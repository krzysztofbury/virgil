"""One account's data as a versioned JSON document, and restoring from one.

The export names the schema version it was made with. An import rebuilds that
schema in a scratch database, loads the rows there, runs the newer migrations
and checks every reference before anything touches the account: an export made
before an upgrade still restores, and a broken file changes nothing. The live
database then swaps its data for the staged copy in one transaction.

Credentials (LLM keys, Oura tokens), the job queue and this server's own
bookkeeping are neither exported nor replaced.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

import aiosqlite

from app.config import CENTRAL_DB_PATH
from app.i18n import N_, LocalizedError, _
from app.migrations.runner import _current_version, latest_version, run_migrations

logger = logging.getLogger(__name__)

FORMAT = "virgil-user-data"
FORMAT_VERSION = 1

# Every table of the account's own data. Restore deletes and refills exactly these.
DATA_TABLES = (
    "user_profiles",
    "daily_logs",
    "daily_briefings",
    "body_measurements",
    "training_exercises",
    "training_sessions",
    "training_entries",
    "exercise_families",
    "exercise_library",
    "exercise_library_tags",
    "exercise_aliases",
    "habits",
    "feniks_journal",
    "feniks_pleasures",
    "feniks_daily",
    "feniks_bricks",
    "pmo_events",
    "oura_daily",
    "oura_monthly",
    "oura_workouts",
    "blood_markers",
    "blood_results",
    "life_scores",
    "goal_areas",
    "goals",
    "goal_reps",
    "goal_changes",
    "deleted_goals",
    "experiments",
    "experiment_activity_types",
    "experiment_weeks",
    "experiment_entries",
    "experiment_summaries",
    "finance_layers",
    "finance_score_rules",
    "finance_accounts",
    "finance_balances",
    "finance_assumptions",
    "finance_income_sources",
    "finance_policies",
    "sync_log",
)

# Never exported, never written by an import: credentials encrypted with this
# server's key, the job queue, and schema bookkeeping. app_settings is handled
# key by key below.
EXCLUDED_TABLES = frozenset(
    {
        "llm_providers",
        "integrations",
        "auth_users",
        "jobs",
        "llm_publications",
        "schema_migrations",
        "app_settings",
    }
)

# Preferences travel with the data. Everything else in app_settings is this
# server's state: an Oura webhook id from another server would misroute
# notifications, and copied last-run times would skip a due backup.
PORTABLE_SETTINGS = frozenset(
    {
        "theme",
        "onboarding_completed",
        "freedom_on_dashboard",
        "training_days",
        "training_swim_per_week",
        "llm_reasoning_effort",
        "briefing_enabled",
        "backup_enabled",
        "backup_interval_hours",
        "backup_max_copies",
        "oura_sync_enabled",
        "oura_sync_interval_hours",
        "finance_base_currency",
    }
)

_LABELS = {
    "user_profiles": N_("Profile"),
    "training_exercises": N_("Training plan exercises"),
    "training_entries": N_("Training entries"),
    "exercise_library_tags": N_("Exercise tags"),
    "habits": N_("Freedom habits"),
    "feniks_journal": N_("Freedom journal"),
    "feniks_pleasures": N_("Freedom pleasures"),
    "feniks_daily": N_("Freedom days"),
    "feniks_bricks": N_("Freedom bricks"),
    "pmo_events": N_("Freedom events"),
    "oura_daily": N_("Oura days"),
    "oura_monthly": N_("Oura months"),
    "oura_workouts": N_("Oura workouts"),
    "experiment_activity_types": N_("Experiment metrics"),
    "sync_log": N_("Sync log"),
    "finance_score_rules": N_("Finance score rules"),
    "finance_income_sources": N_("Finance income sources"),
    "daily_logs": N_("Daily logs"),
    "daily_briefings": N_("Daily briefings"),
    "body_measurements": N_("Body measurements"),
    "training_sessions": N_("Training sessions"),
    "exercise_families": N_("Exercise families"),
    "exercise_library": N_("Exercise library"),
    "exercise_aliases": N_("Exercise aliases"),
    "blood_markers": N_("Blood markers"),
    "blood_results": N_("Blood results"),
    "life_scores": N_("Life scores"),
    "goal_areas": N_("Goal areas"),
    "goals": N_("Goals"),
    "goal_reps": N_("Goal reps"),
    "goal_changes": N_("Goal changes"),
    "deleted_goals": N_("Deleted goals"),
    "experiments": N_("Experiments"),
    "experiment_weeks": N_("Experiment weeks"),
    "experiment_entries": N_("Experiment entries"),
    "experiment_summaries": N_("Experiment summaries"),
    "finance_layers": N_("Finance layers"),
    "finance_accounts": N_("Finance accounts"),
    "finance_balances": N_("Finance balances"),
    "finance_assumptions": N_("Finance assumptions"),
    "finance_policies": N_("Finance policies"),
}


def table_label(table: str) -> str:
    """A table's name on the import check page, in the reader's language."""
    return _(_LABELS[table]) if table in _LABELS else table.replace("_", " ").capitalize()


IMPORT_MAX_BYTES = 20 * 1024 * 1024
IMPORT_TTL_SECONDS = 3600
IMPORT_DIR = Path(CENTRAL_DB_PATH).parent / "imports"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{22}")


class ImportRefused(LocalizedError):
    """The file cannot be restored. The message is shown to the user as is."""


def is_portable_setting(key: str) -> bool:
    return key in PORTABLE_SETTINGS or key.startswith("feature_")


def _number(value: str, kind: type, low: float, high: float) -> bool:
    try:
        number = kind(value)
    except TypeError, ValueError, OverflowError:
        return False
    return isfinite(number) and low <= number <= high


def setting_is_valid(key: str, value: object) -> bool:
    """The rules the settings forms apply, for a value arriving in a file.

    A crafted export must not schedule a backup every minute or keep thousands
    of copies, which the forms refuse.
    """
    from app.services.llm import REASONING_EFFORTS
    from app.services.training_schedule import SWIM_PER_WEEK_MAX, normalize_days

    if not isinstance(value, str) or len(value) > 200:
        return False
    if (
        key.startswith("feature_")
        or key in {"onboarding_completed", "freedom_on_dashboard"}
        or key.endswith("_enabled")
    ):
        return value in ("0", "1")
    if key == "theme":
        return value in ("dark", "light")
    if key in ("backup_interval_hours", "oura_sync_interval_hours"):
        return _number(value, float, 1, 168)
    if key == "backup_max_copies":
        return _number(value, int, 1, 100)
    if key == "training_swim_per_week":
        return _number(value, int, 0, SWIM_PER_WEEK_MAX)
    if key == "training_days":
        return value == ",".join(normalize_days(value))
    if key == "llm_reasoning_effort":
        return value in REASONING_EFFORTS
    if key == "finance_base_currency":
        return len(value) == 3 and value.isalpha() and value.isupper()
    return False


async def _columns(db: aiosqlite.Connection, table: str, schema: str = "main") -> list[str]:
    rows = await db.execute_fetchall(f'PRAGMA "{schema}".table_info("{table}")')
    return [row[1] for row in rows]


async def _tables(db: aiosqlite.Connection) -> set[str]:
    rows = await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    return {row[0] for row in rows}


async def export_document(db: aiosqlite.Connection) -> dict:
    """The account's data as a JSON-ready dict."""
    present = await _tables(db)
    tables = {}
    for table in DATA_TABLES:
        if table not in present:
            continue
        rows = await db.execute_fetchall(f'SELECT * FROM "{table}" ORDER BY rowid')  # noqa: S608 - fixed table list
        tables[table] = [dict(row) for row in rows]
    settings = await db.execute_fetchall("SELECT key, value FROM app_settings ORDER BY key")
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "schema_version": await _current_version(db),
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "settings": {key: value for key, value in settings if is_portable_setting(key)},
        "tables": tables,
    }


def _load(path: Path) -> object:
    try:
        with path.open("rb") as handle:
            return json.load(handle)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImportRefused(N_("The file is not valid JSON.")) from error


def _envelope(document: object) -> tuple[int, dict, dict, str]:
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ImportRefused(N_("This is not a Virgil JSON export. Download one from Settings > Data."))
    if document.get("format_version") != FORMAT_VERSION:
        raise ImportRefused(
            N_("This export uses format version {value!r}, not {FORMAT_VERSION}."),
            value=document.get("format_version"),
            FORMAT_VERSION=FORMAT_VERSION,
        )
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version < 1:
        raise ImportRefused(N_("The export does not say which schema version it was made with."))
    if schema_version > latest_version():
        raise ImportRefused(N_("This export was made by a newer version of Virgil. Update this server first."))
    tables, settings = document.get("tables"), document.get("settings", {})
    if not isinstance(tables, dict) or not isinstance(settings, dict):
        raise ImportRefused(N_("The export's tables or settings are not objects."))
    unknown = sorted(set(tables) - set(DATA_TABLES))
    if unknown:
        raise ImportRefused(
            N_("The export contains tables an import does not restore: {choices}."), choices=", ".join(unknown)
        )
    return schema_version, tables, settings, str(document.get("exported_at", ""))[:40]


def _row_values(table: str, row: object, columns: list[str]) -> tuple[tuple[str, ...], list]:
    if not isinstance(row, dict):
        raise ImportRefused(N_("A row in {table} is not an object."), table=table)
    unknown = [key for key in row if key not in columns]
    if unknown:
        raise ImportRefused(
            N_("{table} has columns this schema version does not: {choices}."),
            table=table,
            choices=", ".join(sorted(unknown)),
        )
    for value in row.values():
        if value is not None and not isinstance(value, str | int | float):
            raise ImportRefused(N_("A value in {table} is a list or object, not text or a number."), table=table)
    return tuple(row), list(row.values())


async def _stage(source: Path, staged: Path) -> dict:
    document = await asyncio.to_thread(_load, source)
    schema_version, tables, settings, exported_at = _envelope(document)
    del document

    db = await aiosqlite.connect(staged)
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA foreign_keys=ON")
        await run_migrations(db, up_to=schema_version)
        present = await _tables(db)
        expected = {table for table in DATA_TABLES if table in present}
        missing = sorted(expected - set(tables))
        if missing:
            raise ImportRefused(N_("The export is incomplete; it has no {choices}."), choices=", ".join(missing))
        absent = sorted(set(tables) - present)
        if absent:
            raise ImportRefused(
                N_("{choices} did not exist at schema version {schema_version}."),
                choices=", ".join(absent),
                schema_version=schema_version,
            )

        # Off while loading: the order of tables in the file must not matter.
        # foreign_key_check below reports every broken reference instead.
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute("BEGIN IMMEDIATE")
        for table in sorted(expected):
            await db.execute(f'DELETE FROM "{table}"')  # noqa: S608 - validated table name
        for table in sorted(expected):
            columns = await _columns(db, table)
            rows = tables.pop(table)
            if not isinstance(rows, list):
                raise ImportRefused(N_("{table} is not a list of rows."), table=table)
            # One statement per run of rows with the same columns: a thread hop per row is slow.
            batches: list[tuple[tuple[str, ...], list[list]]] = []
            for row in rows:
                names, values = _row_values(table, row, columns)
                if batches and batches[-1][0] == names:
                    batches[-1][1].append(values)
                else:
                    batches.append((names, [values]))
            for names, batch in batches:
                if not names:
                    raise ImportRefused(N_("A row in {table} is empty."), table=table)
                quoted = ", ".join(f'"{name}"' for name in names)
                marks = ", ".join("?" for _ in names)
                try:
                    await db.executemany(f'INSERT INTO "{table}" ({quoted}) VALUES ({marks})', batch)  # noqa: S608
                except aiosqlite.IntegrityError as error:
                    raise ImportRefused(
                        N_("A row in {table} breaks a rule of the schema: {error}."), table=table, error=error
                    ) from error
        skipped = sorted(key for key in settings if not is_portable_setting(key))
        invalid = sorted(
            key for key in settings if is_portable_setting(key) and not setting_is_valid(key, settings[key])
        )
        await db.execute("DELETE FROM app_settings")
        for key, value in settings.items():
            if key not in skipped and key not in invalid:
                await db.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

        await db.execute("PRAGMA foreign_keys=ON")
        # Settled at the export's own schema first: the newer migrations run
        # with foreign keys on, as they do at startup, and may rebuild tables.
        dropped = await _settle_orphans(db)
        await run_migrations(db)
        dropped += await _settle_orphans(db)
        integrity = await db.execute_fetchall("PRAGMA integrity_check")
        if [tuple(row) for row in integrity] != [("ok",)]:
            raise ImportRefused(N_("The staged copy failed SQLite's integrity check. Nothing was changed."))

        counts = await current_counts(db)
        portable = await db.execute_fetchall("SELECT key FROM app_settings")
        return {
            "schema_version": schema_version,
            "exported_at": exported_at,
            "counts": counts,
            "settings": sorted(key for (key,) in portable if is_portable_setting(key)),
            "skipped_settings": skipped,
            "invalid_settings": invalid,
            "dropped_orphans": dropped,
        }
    except BaseException:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


async def _settle_orphans(db: aiosqlite.Connection) -> int:
    """Do to rows whose parent is gone what SQLite would have done when it went.

    A database that once ran without foreign keys can keep children of deleted
    rows, and its owner's export must still restore. A CASCADE child is left
    out and a SET NULL reference is cleared. Any other broken reference refuses
    the file. Returns how many rows were left out, cascades included.
    """
    before = sum((await current_counts(db)).values())
    keys: dict[str, dict[int, tuple[str, str]]] = {}
    # A dropped row can orphan its own children, so check again until settled.
    for _attempt in range(8):
        broken = await db.execute_fetchall("PRAGMA foreign_key_check")
        if not broken:
            return before - sum((await current_counts(db)).values())
        fixes = []
        for table, rowid, _parent, key_id in broken:
            if table not in keys:
                rows = await db.execute_fetchall(f'PRAGMA foreign_key_list("{table}")')
                keys[table] = {row[0]: (row[3], row[6].upper()) for row in rows}
            column, action = keys[table][key_id]
            fixes.append((table, rowid, column, action))
        refused = sorted({table for table, _, _, action in fixes if action not in ("CASCADE", "SET NULL")})
        if refused:
            raise ImportRefused(
                N_("Rows in {choices} point at rows the export does not have. Nothing was changed."),
                choices=", ".join(refused),
            )
        await db.execute("BEGIN IMMEDIATE")
        for table, rowid, column, action in fixes:
            if action == "CASCADE":
                await db.execute(f'DELETE FROM "{table}" WHERE rowid = ?', (rowid,))  # noqa: S608
            else:
                await db.execute(f'UPDATE "{table}" SET "{column}" = NULL WHERE rowid = ?', (rowid,))  # noqa: S608
        await db.commit()
    raise ImportRefused(N_("The export's references could not be settled. Nothing was changed."))


def _paths(stem: str, token: str) -> tuple[Path, Path, Path]:
    if not _TOKEN.fullmatch(token) or not stem or "/" in stem or ".." in stem:
        raise ImportRefused(N_("That import was not found. Upload the file again."))
    base = IMPORT_DIR / f"{stem}-{token}"
    return base.with_suffix(".upload"), base.with_suffix(".db"), base.with_suffix(".json")


def new_upload(stem: str) -> tuple[str, Path]:
    """A token and the path its uploaded file is written to."""
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(16)
    return token, _paths(stem, token)[0]


async def stage_upload(stem: str, token: str) -> dict:
    """Validate the uploaded file into a staged database and keep its report.

    Raises ImportRefused, after removing everything staged, when the file cannot be restored.
    """
    upload, staged, report_path = _paths(stem, token)
    try:
        report = await _stage(upload, staged)
        report["created_at"] = time.time()
        await asyncio.to_thread(report_path.write_text, json.dumps(report))
        return report
    except BaseException:
        discard(stem, token)
        raise
    finally:
        upload.unlink(missing_ok=True)


def staged_report(stem: str, token: str) -> dict | None:
    """The dry-run report of a staged import, or None when it is gone or expired."""
    _upload, staged, report_path = _paths(stem, token)
    try:
        report = json.loads(report_path.read_text())
    except FileNotFoundError, json.JSONDecodeError:
        return None
    if time.time() - report.get("created_at", 0) > IMPORT_TTL_SECONDS or not staged.exists():
        discard(stem, token)
        return None
    return report


def discard(stem: str, token: str) -> None:
    for path in _paths(stem, token):
        path.unlink(missing_ok=True)


def discard_account_imports(stem: str) -> None:
    """Remove every staged import of one database (account delete or reset)."""
    if IMPORT_DIR.exists():
        for path in IMPORT_DIR.glob(f"{stem}-*"):
            path.unlink(missing_ok=True)


def remove_stale_imports(now: float | None = None) -> int:
    """Delete staged imports nobody confirmed within the time limit."""
    if not IMPORT_DIR.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - IMPORT_TTL_SECONDS
    removed = 0
    for path in IMPORT_DIR.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except FileNotFoundError:
            continue
    return removed


async def current_counts(db: aiosqlite.Connection) -> dict[str, int]:
    present = await _tables(db)
    counts = {}
    for table in DATA_TABLES:
        if table in present:
            rows = await db.execute_fetchall(f'SELECT COUNT(*) FROM "{table}"')  # noqa: S608
            counts[table] = rows[0][0]
    return counts


async def apply_staged(db: aiosqlite.Connection, stem: str, token: str) -> None:
    """Replace the account's data with the staged copy, all or nothing."""
    _upload, staged, _report = _paths(stem, token)
    if not staged.exists():
        raise ImportRefused(N_("That import has expired. Upload the file again."))
    if db.in_transaction:
        raise RuntimeError("Import needs a connection with no open transaction")
    await db.execute("ATTACH DATABASE ? AS staged", (os.fspath(staged),))
    try:
        rows = await db.execute_fetchall("SELECT name FROM staged.sqlite_master WHERE type = 'table'")
        incoming = {row[0] for row in rows}
        tables = [table for table in DATA_TABLES if table in await _tables(db)]
        for table in tables:
            if table not in incoming or await _columns(db, table) != await _columns(db, table, "staged"):
                raise ImportRefused(N_("The server was updated after this file was checked. Upload it again."))

        await db.execute("BEGIN IMMEDIATE")
        try:
            # Every reference is checked at COMMIT, against the finished tables.
            # Deleting all before inserting any keeps ON DELETE actions from
            # reaching rows that were just inserted.
            await db.execute("PRAGMA defer_foreign_keys=ON")
            for table in tables:
                await db.execute(f'DELETE FROM main."{table}"')  # noqa: S608 - fixed table list
            for table in tables:
                columns = ", ".join(f'"{name}"' for name in await _columns(db, table))
                await db.execute(f'INSERT INTO main."{table}" ({columns}) SELECT {columns} FROM staged."{table}"')  # noqa: S608
            keys = await db.execute_fetchall("SELECT key FROM main.app_settings")
            for (key,) in keys:
                if is_portable_setting(key):
                    await db.execute("DELETE FROM main.app_settings WHERE key = ?", (key,))
            settings = await db.execute_fetchall("SELECT key, value FROM staged.app_settings")
            for key, value in settings:
                if is_portable_setting(key):
                    await db.execute("INSERT INTO main.app_settings (key, value) VALUES (?, ?)", (key, value))
            # Queued model work names rows by id, and those ids now hold other rows.
            now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
            await db.execute(
                "UPDATE main.jobs SET status = 'cancelled', finished_at = ?, updated_at = ? "
                "WHERE status = 'queued' AND kind NOT IN ('backup', 'oura_sync')",
                (now, now),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    finally:
        await db.execute("DETACH DATABASE staged")
    discard(stem, token)
