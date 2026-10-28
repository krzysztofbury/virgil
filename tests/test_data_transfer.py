"""A JSON export restores the account it came from, and a bad file changes nothing (#48).

Restore replaces the account's data with the export's, keeping row ids. The
checks run on a staged copy first, so the live database sees either the whole
export or none of it.
"""

import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

import aiosqlite
import pytest

from app.migrations.runner import latest_version, run_migrations
from app.services import data_transfer
from app.services.data_transfer import (
    DATA_TABLES,
    EXCLUDED_TABLES,
    ImportRefused,
    apply_staged,
    export_document,
    new_upload,
    stage_upload,
)
from tests.conftest import csrf_token, user_db_path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from seed_demo import _seed  # noqa: E402

STEM = "transfer-test"


@pytest.fixture
def import_dir(tmp_path, monkeypatch):
    directory = tmp_path / "imports"
    monkeypatch.setattr(data_transfer, "IMPORT_DIR", directory)
    return directory


async def _database(path: Path, *, up_to: int | None = None) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await run_migrations(db, up_to=up_to)
    return db


async def _stage(document: dict) -> str:
    token, upload = new_upload(STEM)
    upload.write_text(json.dumps(document))
    await stage_upload(STEM, token)
    return token


def _comparable(document: dict) -> dict:
    return {"settings": document["settings"], "tables": document["tables"]}


def test_every_table_is_either_restored_or_excluded(tmp_path):
    async def tables():
        db = await _database(tmp_path / "fresh.db")
        try:
            rows = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
            return {row[0] for row in rows}
        finally:
            await db.close()

    schema = asyncio.run(tables())
    assert not set(DATA_TABLES) & EXCLUDED_TABLES
    assert schema == set(DATA_TABLES) | EXCLUDED_TABLES, "a new table must be added to DATA_TABLES or EXCLUDED_TABLES"


def test_export_then_import_reproduces_the_account(tmp_path, import_dir):
    async def run():
        source = await _database(tmp_path / "source.db")
        target = await _database(tmp_path / "target.db")
        try:
            await _seed(source)
            # A goal whose parent comes later in the same table.
            child, parent = (
                row[0] for row in await source.execute_fetchall("SELECT id FROM goals ORDER BY id LIMIT 2")
            )
            await source.execute("UPDATE goals SET parent_goal_id = ? WHERE id = ?", (parent, child))
            await source.commit()
            exported = await export_document(source)

            await target.execute(
                "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES ('x', 'enc', 'm', 1)"
            )
            await target.executemany(
                "INSERT INTO app_settings (key, value) VALUES (?, ?)",
                [("oura_webhook_id", "this-server"), ("theme", "light")],
            )
            # The account being replaced has data of its own, referencing each other.
            await _seed(target)
            await target.execute("INSERT INTO daily_logs (date) VALUES ('2001-01-01')")
            await target.commit()

            await apply_staged(target, STEM, await _stage(exported))
            restored = await export_document(target)
            providers = await target.execute_fetchall("SELECT provider FROM llm_providers")
            webhook = await target.execute_fetchall("SELECT value FROM app_settings WHERE key = 'oura_webhook_id'")
            return exported, restored, [row[0] for row in providers], webhook[0][0]
        finally:
            await source.close()
            await target.close()

    exported, restored, providers, webhook = asyncio.run(run())
    assert any(goal["parent_goal_id"] for goal in exported["tables"]["goals"])
    assert exported["tables"]["training_entries"] and exported["tables"]["experiment_entries"]
    assert _comparable(restored) == _comparable(exported)
    assert providers == ["x"], "credentials are not part of the data and must survive a restore"
    assert webhook == "this-server", "this server's Oura routing must not be replaced"
    assert not list(import_dir.iterdir()), "the staged copy is removed once applied"


def test_a_broken_reference_refuses_the_whole_file(tmp_path, import_dir):
    async def run():
        source = await _database(tmp_path / "source.db")
        try:
            await _seed(source)
            await source.commit()
            exported = await export_document(source)
        finally:
            await source.close()
        # Deleting an exercise that entries use is refused, so this cannot be an old leftover.
        exported["tables"]["training_entries"][0]["exercise_id"] = 999_999
        with pytest.raises(ImportRefused, match="training_entries"):
            await _stage(exported)

    asyncio.run(run())
    assert not list(import_dir.iterdir()), "a refused file leaves nothing staged"


def test_leftovers_of_deleted_rows_are_settled_as_the_delete_would_have(tmp_path, import_dir):
    """A database that once ran without foreign keys keeps children of deleted
    rows. Its owner's export must still restore."""

    async def run():
        source = await _database(tmp_path / "source.db")
        try:
            await _seed(source)
            await source.commit()
            exported = await export_document(source)
        finally:
            await source.close()
        tables = exported["tables"]
        experiment = tables["experiments"].pop(0)["id"]
        children = sum(
            1
            for table in ("experiment_activity_types", "experiment_weeks", "experiment_entries", "experiment_summaries")
            for row in tables[table]
            if row["experiment_id"] == experiment
        )
        goal = tables["goals"][0]["id"]
        tables["experiments"][0]["goal_id"] = goal
        tables["goals"] = [row for row in tables["goals"] if row["id"] != goal]
        tables["goal_reps"] = [row for row in tables["goal_reps"] if row["goal_id"] != goal]
        token = await _stage(exported)
        report = data_transfer.staged_report(STEM, token)
        staged = await aiosqlite.connect(import_dir / f"{STEM}-{token}.db")
        try:
            left = await staged.execute_fetchall(
                "SELECT COUNT(*) FROM experiment_entries WHERE experiment_id = ?", (experiment,)
            )
            cleared = await staged.execute_fetchall(
                "SELECT goal_id FROM experiments WHERE id = ?", (tables["experiments"][0]["id"],)
            )
        finally:
            await staged.close()
        return children, report["dropped_orphans"], left[0][0], cleared[0][0]

    children, dropped, left, cleared = asyncio.run(run())
    assert children > 0
    assert dropped == children
    assert left == 0
    assert cleared is None


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda doc: doc.update(format="something-else"), "not a Virgil JSON export"),
        (lambda doc: doc.update(schema_version=latest_version() + 1), "newer version"),
        (lambda doc: doc["tables"].update(llm_providers=[]), "llm_providers"),
        (lambda doc: doc["tables"].pop("goals"), "incomplete"),
        (lambda doc: doc["tables"]["daily_logs"].append({"date": "2026-01-01", "secret": 1}), "secret"),
    ],
)
def test_files_that_are_not_a_valid_export_are_refused(tmp_path, import_dir, change, message):
    async def run():
        source = await _database(tmp_path / "source.db")
        try:
            await source.execute("INSERT INTO daily_logs (date) VALUES ('2026-02-01')")
            await source.commit()
            exported = await export_document(source)
        finally:
            await source.close()
        change(exported)
        with pytest.raises(ImportRefused, match=message):
            await _stage(exported)

    asyncio.run(run())


def test_an_export_from_an_older_schema_is_upgraded(tmp_path, import_dir):
    older = latest_version() - 1

    async def run():
        source = await _database(tmp_path / "source.db", up_to=older)
        target = await _database(tmp_path / "target.db")
        try:
            await source.execute("INSERT INTO daily_logs (date, energy) VALUES ('2026-03-01', 7)")
            # The older database is also the one likely to hold a leftover of a
            # row deleted without foreign keys; the newer migrations run on it.
            await source.commit()
            await source.execute("PRAGMA foreign_keys=OFF")
            await source.execute("INSERT INTO feniks_daily (habit_id, date) VALUES (999999, '2026-03-01')")
            await source.commit()
            exported = await export_document(source)
            token = await _stage(exported)
            dropped = data_transfer.staged_report(STEM, token)["dropped_orphans"]
            await apply_staged(target, STEM, token)
            rows = await target.execute_fetchall("SELECT date, energy FROM daily_logs")
            return exported["schema_version"], [tuple(row) for row in rows], dropped
        finally:
            await source.close()
            await target.close()

    version, rows, dropped = asyncio.run(run())
    assert version == older
    assert rows == [("2026-03-01", 7)]
    assert dropped == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("backup_interval_hours", "0.001"),
        ("backup_max_copies", "100000"),
        ("oura_sync_interval_hours", "nan"),
        ("feature_freedom", "yes"),
        ("llm_reasoning_effort", "maximum"),
        ("training_days", "every day"),
        ("theme", 7),
    ],
)
def test_a_setting_outside_what_settings_allows_is_not_restored(tmp_path, import_dir, key, value):
    async def run():
        source = await _database(tmp_path / "source.db")
        try:
            exported = await export_document(source)
        finally:
            await source.close()
        exported["settings"][key] = value
        exported["settings"]["backup_enabled"] = "1"
        report = data_transfer.staged_report(STEM, await _stage(exported))
        return report

    report = asyncio.run(run())
    assert report["invalid_settings"] == [key]
    assert key not in report["settings"]
    assert "backup_enabled" in report["settings"]


def _counts():
    conn = sqlite3.connect(user_db_path())
    try:
        return {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in DATA_TABLES}  # noqa: S608
    finally:
        conn.close()


def _upload(client, content: bytes):
    token = csrf_token(client, "/settings?tab=data")
    return client.post(
        "/settings/import",
        data={"_csrf_token": token},
        files={"file": ("virgil-export.json", content, "application/json")},
        follow_redirects=False,
    )


def test_the_web_flow_checks_first_and_restores_on_confirm(auth_client):
    conn = sqlite3.connect(user_db_path())
    conn.execute("INSERT INTO daily_logs (date, energy) VALUES ('1999-12-31', 3)")
    conn.commit()
    conn.close()

    export = auth_client.get("/settings/export/json")
    assert export.status_code == 200
    assert export.json()["format"] == "virgil-user-data"

    conn = sqlite3.connect(user_db_path())
    conn.execute("DELETE FROM daily_logs WHERE date = '1999-12-31'")
    conn.commit()
    conn.close()
    before = _counts()

    checked = _upload(auth_client, export.content)
    assert checked.status_code == 303
    review_path = checked.headers["location"].split("?")[0]
    assert re.fullmatch(r"/settings/import/[A-Za-z0-9_-]{22}", review_path)
    review = auth_client.get(review_path)
    assert "Replace my data" in review.text
    assert _counts() == before, "checking a file must not change the account"

    token = csrf_token(auth_client, review_path)
    applied = auth_client.post(f"{review_path}/apply", data={"_csrf_token": token}, follow_redirects=False)
    assert applied.status_code == 303
    assert "err=" not in applied.headers["location"]
    conn = sqlite3.connect(user_db_path())
    restored = conn.execute("SELECT energy FROM daily_logs WHERE date = '1999-12-31'").fetchall()
    conn.execute("DELETE FROM daily_logs WHERE date = '1999-12-31'")
    conn.commit()
    conn.close()
    assert restored == [(3,)]

    from app.services.backup import BACKUP_DIR

    assert list((BACKUP_DIR / "pre-import").glob(f"{user_db_path().stem}-pre-import-*.db"))

    again = auth_client.post(f"{review_path}/apply", data={"_csrf_token": token}, follow_redirects=False)
    assert "expired" in again.headers["location"], "a checked file restores once"


def test_the_web_flow_refuses_a_file_that_is_not_an_export(auth_client):
    before = _counts()
    response = _upload(auth_client, b'{"hello": "world"}')
    assert response.status_code == 303
    assert (
        "not+a+Virgil+JSON+export" in response.headers["location"] or "not%20a%20Virgil" in response.headers["location"]
    )
    assert _counts() == before


@pytest.mark.parametrize(
    ("stem", "token"),
    [
        ("account", "../../../../etc/passwd"),
        ("account", "a" * 21),
        ("../account", "a" * 22),
        ("acc/ount", "a" * 22),
        ("", "a" * 22),
    ],
)
def test_an_import_path_never_leaves_the_import_directory(stem, token):
    from app.services.data_transfer import ImportRefused, staged_report

    with pytest.raises(ImportRefused):
        staged_report(stem, token)
