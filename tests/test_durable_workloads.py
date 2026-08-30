"""Production backup, export, and Oura jobs from enqueue through execution."""

import asyncio
import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiosqlite
import pytest

import app.services.backup as backup_module
import app.services.markdown_export as export_module
from app.services.job_handlers import handle_backup, handle_markdown_export
from app.services.job_producers import (
    BACKUP_JOB_KIND,
    MARKDOWN_EXPORT_JOB_KIND,
    OURA_SYNC_JOB_KIND,
    ActiveWorkloadConflictError,
    enqueue_backup_job,
    enqueue_markdown_export_job,
    enqueue_oura_sync_job,
    manual_job_key,
)
from app.services.job_worker import JobContext, run_jobs_for_user
from app.services.jobs import claim_next_job, fail_job, get_job, recover_stale_jobs
from app.services.oura_api import OuraSyncResult
from tests.conftest import csrf_token, user_db_path


def _connect(path: Path | None = None) -> sqlite3.Connection:
    return sqlite3.connect(path or user_db_path())


def _clear_workloads() -> None:
    db = _connect()
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM integrations WHERE provider = 'oura'")
        db.execute("DELETE FROM app_settings WHERE key IN ('backup_last_run', 'export_last_run', 'oura_sync_last_run')")
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean_workloads(auth_client):
    _clear_workloads()
    yield
    _clear_workloads()


def _seed_connected_oura() -> None:
    db = _connect()
    try:
        db.execute(
            "INSERT INTO integrations (provider, client_id, client_secret_enc, status) "
            "VALUES ('oura', 'client', 'secret', 'connected')"
        )
        db.commit()
    finally:
        db.close()


def _job_nonce(html: str) -> str:
    match = re.search(r'name="job_nonce" value="([0-9a-f]{32})"', html)
    assert match
    return match.group(1)


def _redirect_job_id(response, header: str = "location") -> int:
    target = urlsplit(response.headers[header])
    values = parse_qs(target.query)
    assert values["job_id"]
    return int(values["job_id"][0])


def _user_id() -> str:
    db = sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])
    try:
        return db.execute("SELECT id FROM users ORDER BY created_at, id LIMIT 1").fetchone()[0]
    finally:
        db.close()


def test_manual_routes_only_enqueue_and_duplicate_submission_reuses_job(auth_client, monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("request route must not execute workload I/O")

    monkeypatch.setattr(backup_module, "run_backup", forbidden)
    monkeypatch.setattr(export_module, "write_export", forbidden)
    monkeypatch.setattr("app.services.oura_api.sync_oura_from_api", forbidden)
    _seed_connected_oura()

    automation = auth_client.get("/settings?tab=automation")
    backup_data = {
        "_csrf_token": csrf_token(auth_client, "/settings?tab=automation"),
        "job_nonce": _job_nonce(automation.text),
    }
    backup = auth_client.post("/settings/backup/now", data=backup_data, follow_redirects=False)
    backup_duplicate = auth_client.post("/settings/backup/now", data=backup_data, follow_redirects=False)
    assert backup.status_code == backup_duplicate.status_code == 303
    backup_id = _redirect_job_id(backup)
    assert _redirect_job_id(backup_duplicate) == backup_id

    data_page = auth_client.get("/settings?tab=data")
    export = auth_client.post(
        "/settings/export",
        data={
            "_csrf_token": csrf_token(auth_client, "/settings?tab=data"),
            "job_nonce": _job_nonce(data_page.text),
            "scope": "monthly",
            "sections": ["training", "daily_logs", "training"],
        },
        follow_redirects=False,
    )
    assert export.status_code == 303
    export_id = _redirect_job_id(export)

    oura_page = auth_client.get("/oura")
    oura = auth_client.post(
        "/oura/api-sync",
        data={"_csrf_token": csrf_token(auth_client, "/oura"), "job_nonce": _job_nonce(oura_page.text)},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert oura.status_code == 200
    oura_id = _redirect_job_id(oura, "HX-Redirect")

    integrations = auth_client.get("/settings?tab=integrations")
    settings_oura = auth_client.post(
        "/settings/oura/sync",
        data={
            "_csrf_token": csrf_token(auth_client, "/settings?tab=integrations"),
            "job_nonce": _job_nonce(integrations.text),
        },
        follow_redirects=False,
    )
    assert settings_oura.status_code == 303
    assert _redirect_job_id(settings_oura) == oura_id

    db = _connect()
    try:
        rows = db.execute(
            "SELECT id, kind, payload_json, retry_policy, max_attempts, status FROM jobs ORDER BY id"
        ).fetchall()
    finally:
        db.close()
    assert rows == [
        (backup_id, "backup", '{"trigger":"manual"}', "automatic", 3, "queued"),
        (
            export_id,
            "markdown_export",
            '{"scope":"monthly","sections":["daily_logs","training"],"trigger":"manual"}',
            "automatic",
            3,
            "queued",
        ),
        (oura_id, "oura_sync", '{"days_back":30,"trigger":"manual"}', "automatic", 3, "queued"),
    ]

    assert f'data-job-id="{backup_id}"' in auth_client.get(backup.headers["location"]).text
    assert f'data-job-id="{export_id}"' in auth_client.get(export.headers["location"]).text
    assert f'data-job-id="{oura_id}"' in auth_client.get(oura.headers["HX-Redirect"]).text


def test_scheduled_producers_coalesce_and_registered_handlers_execute_once(auth_client, monkeypatch, tmp_path):
    import app.services.scheduler as scheduler

    backup_dir = tmp_path / "backups"
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(export_module, "SECOND_BRAIN_PATH", str(export_dir))
    oura_calls = []

    async def fake_sync(db, days_back):
        assert db.in_transaction is False
        oura_calls.append(days_back)
        return OuraSyncResult(days=2, failed_daily_endpoints=("sleep",), workouts_synced=False)

    monkeypatch.setattr("app.services.oura_api.sync_oura_from_api", fake_sync)
    _seed_connected_oura()
    db = _connect()
    try:
        db.executemany(
            "INSERT OR REPLACE INTO app_settings(key, value) VALUES(?, ?)",
            [
                ("backup_enabled", "1"),
                ("backup_interval_hours", "24"),
                ("export_enabled", "1"),
                ("export_interval_hours", "6"),
                ("oura_sync_enabled", "1"),
                ("oura_sync_interval_hours", "6"),
            ],
        )
        db.commit()
    finally:
        db.close()

    async def scenario():
        control = await aiosqlite.connect(user_db_path())
        control.row_factory = aiosqlite.Row
        try:
            await scheduler._enqueue_due_jobs(control)
            await scheduler._enqueue_due_jobs(control)
            queued = await control.execute_fetchall(
                "SELECT kind, payload_json, retry_policy, status FROM jobs ORDER BY id"
            )
            batch = await run_jobs_for_user(
                control,
                _user_id(),
                user_db_path().name,
                worker_id="workload-test",
                max_jobs=3,
            )
            jobs = await control.execute_fetchall("SELECT id, kind, status, result_json FROM jobs ORDER BY id")
            settings = await control.execute_fetchall(
                "SELECT key, value FROM app_settings "
                "WHERE key IN ('backup_last_run', 'export_last_run', 'oura_sync_last_run') ORDER BY key"
            )
            return queued, batch, jobs, settings
        finally:
            await control.close()

    queued, batch, jobs, settings = asyncio.run(scenario())
    assert [row["kind"] for row in queued] == ["backup", "markdown_export", "oura_sync"]
    assert all(row["retry_policy"] == "automatic" and row["status"] == "queued" for row in queued)
    assert [result.status for result in batch.jobs] == ["succeeded", "succeeded", "succeeded"]
    assert [row["status"] for row in jobs] == ["succeeded", "succeeded", "succeeded"]
    assert len(list(backup_dir.glob("*.db"))) == 1
    assert (export_dir / "virgil.md").exists()
    assert oura_calls == [30]
    assert [row["key"] for row in settings] == ["backup_last_run", "export_last_run", "oura_sync_last_run"]
    assert all(row["value"] for row in settings)

    oura_job_id = next(row["id"] for row in jobs if row["kind"] == "oura_sync")
    status = auth_client.get(f"/api/jobs/{oura_job_id}")
    assert status.status_code == 200
    assert "Partial" in status.text
    assert "partial data" in status.text
    assert "failed_daily_endpoints" not in status.text


def test_backup_handler_replay_reuses_one_validated_artifact(auth_client, monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)

    async def scenario():
        db = await aiosqlite.connect(user_db_path())
        db.row_factory = aiosqlite.Row
        try:
            queued = await enqueue_backup_job(
                db,
                trigger="manual",
                idempotency_key=manual_job_key(BACKUP_JOB_KIND, "a" * 32),
            )
            context = JobContext(db=db, user_id=_user_id(), job_id=queued.job_id, attempt=1)
            first = await handle_backup(context, {"trigger": "manual"})
            second = await handle_backup(context, {"trigger": "manual"})
            return first, second
        finally:
            await db.close()

    first, second = asyncio.run(scenario())
    assert first == second
    artifacts = list(backup_dir.glob("*.db"))
    assert [artifact.name for artifact in artifacts] == [first["filename"]]
    assert sqlite3.connect(artifacts[0]).execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_distinct_backup_jobs_in_same_minute_publish_distinct_snapshots(auth_client, monkeypatch, tmp_path):
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)

    async def scenario():
        db = await aiosqlite.connect(user_db_path())
        db.row_factory = aiosqlite.Row
        try:
            await db.execute("DELETE FROM app_settings WHERE key = 'backup_probe'")
            await db.commit()
            first = await enqueue_backup_job(
                db,
                trigger="manual",
                idempotency_key=manual_job_key(BACKUP_JOB_KIND, "c" * 32),
            )
            first_result = await handle_backup(
                JobContext(db=db, user_id=_user_id(), job_id=first.job_id, attempt=1),
                {"trigger": "manual"},
            )
            await db.execute(
                "UPDATE jobs SET status = 'succeeded', attempts = 1, started_at = datetime('now'), "
                "finished_at = datetime('now'), updated_at = datetime('now') "
                "WHERE id = ?",
                (first.job_id,),
            )
            await db.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('backup_probe', 'new')")
            await db.commit()
            second = await enqueue_backup_job(
                db,
                trigger="manual",
                idempotency_key=manual_job_key(BACKUP_JOB_KIND, "d" * 32),
            )
            second_result = await handle_backup(
                JobContext(db=db, user_id=_user_id(), job_id=second.job_id, attempt=1),
                {"trigger": "manual"},
            )
            await db.execute("DELETE FROM app_settings WHERE key = 'backup_probe'")
            await db.commit()
            return first_result, second_result
        finally:
            await db.close()

    first, second = asyncio.run(scenario())
    assert first["filename"] != second["filename"]
    assert "-j" in first["filename"] and "-j" in second["filename"]
    first_db = sqlite3.connect(backup_dir / first["filename"])
    second_db = sqlite3.connect(backup_dir / second["filename"])
    try:
        assert first_db.execute("SELECT value FROM app_settings WHERE key = 'backup_probe'").fetchone() is None
        assert second_db.execute("SELECT value FROM app_settings WHERE key = 'backup_probe'").fetchone() == ("new",)
    finally:
        first_db.close()
        second_db.close()


def test_markdown_export_handler_replay_atomically_replaces_one_artifact(auth_client, monkeypatch, tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    monkeypatch.setattr(export_module, "SECOND_BRAIN_PATH", str(export_dir))

    async def scenario():
        db = await aiosqlite.connect(user_db_path())
        db.row_factory = aiosqlite.Row
        try:
            queued = await enqueue_markdown_export_job(
                db,
                trigger="manual",
                scope="monthly",
                sections={"daily_logs", "training"},
                idempotency_key=manual_job_key(MARKDOWN_EXPORT_JOB_KIND, "5" * 32),
            )
            context = JobContext(db=db, user_id=_user_id(), job_id=queued.job_id, attempt=1)
            payload = {
                "scope": "monthly",
                "sections": ["daily_logs", "training"],
                "trigger": "manual",
            }
            return await handle_markdown_export(context, payload), await handle_markdown_export(context, payload)
        finally:
            await db.close()

    first, second = asyncio.run(scenario())
    assert first == second
    assert [path.name for path in export_dir.iterdir()] == [first["filename"]]
    assert (export_dir / first["filename"]).read_text(encoding="utf-8").endswith("---\n")


def test_backup_job_recovers_after_dead_claim_and_process_restart(monkeypatch, tmp_path):
    from app.migrations.runner import run_migrations

    path = tmp_path / "restart.db"
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)

    async def open_db(_filename):
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        return db

    async def scenario():
        first = await open_db(path.name)
        await run_migrations(first)
        queued = await enqueue_backup_job(
            first,
            trigger="manual",
            idempotency_key=manual_job_key(BACKUP_JOB_KIND, "b" * 32),
        )
        claimed = await claim_next_job(first, "dead-worker")
        assert claimed and claimed["id"] == queued.job_id
        old_lock = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S.%f")
        await first.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (old_lock, queued.job_id))
        await first.commit()
        await first.close()

        restarted = await open_db(path.name)
        now = datetime.now(UTC)
        recovered = await recover_stale_jobs(
            restarted,
            now - timedelta(minutes=1),
            now=now,
            retry_delay_seconds=1,
        )
        await asyncio.sleep(1.05)
        batch = await run_jobs_for_user(
            restarted,
            "user-restarted",
            path.name,
            worker_id="new-worker",
            handler_db_opener=open_db,
            max_jobs=1,
        )
        job = await get_job(restarted, queued.job_id)
        await restarted.close()
        return recovered, batch, job

    recovered, batch, job = asyncio.run(scenario())
    assert [(item.job_id, item.status) for item in recovered] == [(job["id"], "queued")]
    assert [item.status for item in batch.jobs] == ["succeeded"]
    assert (job["status"], job["attempts"]) == ("succeeded", 2)
    assert len(list(backup_dir.glob("*.db"))) == 1


def test_production_registry_contains_only_migrated_workloads():
    from app.services.job_worker import JOB_HANDLERS
    from app.services.llm_jobs import PAID_LLM_JOB_KINDS

    operational = {BACKUP_JOB_KIND, MARKDOWN_EXPORT_JOB_KIND, OURA_SYNC_JOB_KIND}
    assert set(JOB_HANDLERS) - operational <= set(PAID_LLM_JOB_KINDS), "only known kinds may reach a handler"
    assert (
        json.dumps(sorted(JOB_HANDLERS))
        == '["andy_generation", "backup", "markdown_export", "morning_briefing", "oura_sync", "wod_parse"]'
    )


def test_webhook_jobs_coalesce_queued_work_and_leave_one_successor_while_running(tmp_path):
    from app.migrations.runner import run_migrations

    async def scenario():
        path = tmp_path / "coalesce.db"
        first = await aiosqlite.connect(path)
        second = await aiosqlite.connect(path)
        first.row_factory = second.row_factory = aiosqlite.Row
        await run_migrations(first)
        one, two = await asyncio.gather(
            enqueue_oura_sync_job(
                first,
                trigger="webhook",
                days_back=2,
                idempotency_key="oura_sync:webhook:300:100",
            ),
            enqueue_oura_sync_job(
                second,
                trigger="webhook",
                days_back=2,
                idempotency_key="oura_sync:webhook:300:101",
            ),
        )
        rows = await first.execute_fetchall("SELECT id, status FROM jobs")
        running = await claim_next_job(first, "webhook-worker")
        successor, coalesced_successor = await asyncio.gather(
            enqueue_oura_sync_job(
                first,
                trigger="webhook",
                days_back=2,
                idempotency_key="oura_sync:webhook:delivery:102",
            ),
            enqueue_oura_sync_job(
                second,
                trigger="webhook",
                days_back=2,
                idempotency_key="oura_sync:webhook:delivery:103",
            ),
        )
        failed = await fail_job(first, running["id"], running["claim_token"], "Oura temporarily unavailable")
        claimed_successor = await claim_next_job(first, "webhook-worker")
        final_rows = await first.execute_fetchall("SELECT id, status FROM jobs ORDER BY id")
        await first.close()
        await second.close()
        return one, two, rows, running, successor, coalesced_successor, failed, claimed_successor, final_rows

    one, two, rows, running, successor, coalesced_successor, failed, claimed_successor, final_rows = asyncio.run(
        scenario()
    )
    assert one.job_id == two.job_id == rows[0]["id"]
    assert len(rows) == 1
    assert {one.created, two.created} == {False, True}
    assert running["id"] == one.job_id
    assert successor.job_id == coalesced_successor.job_id
    assert {successor.created, coalesced_successor.created} == {False, True}
    assert failed == "failed"
    assert claimed_successor["id"] == successor.job_id
    assert [(row["id"], row["status"]) for row in final_rows] == [
        (one.job_id, "failed"),
        (successor.job_id, "running"),
    ]


def test_queued_oura_sync_must_cover_requested_range(tmp_path):
    from app.migrations.runner import run_migrations

    async def scenario():
        db = await aiosqlite.connect(tmp_path / "range.db")
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        scheduled = await enqueue_oura_sync_job(
            db,
            trigger="scheduled",
            days_back=30,
            idempotency_key="oura_sync:scheduled:slot",
        )
        covered = await enqueue_oura_sync_job(
            db,
            trigger="webhook",
            days_back=2,
            idempotency_key="oura_sync:webhook:covered",
        )
        await db.execute("DELETE FROM jobs")
        await db.commit()
        webhook = await enqueue_oura_sync_job(
            db,
            trigger="webhook",
            days_back=2,
            idempotency_key="oura_sync:webhook:first",
        )
        with pytest.raises(ActiveWorkloadConflictError):
            await enqueue_oura_sync_job(
                db,
                trigger="scheduled",
                days_back=30,
                idempotency_key="oura_sync:scheduled:later",
            )
        running = await claim_next_job(db, "range-worker")
        successor = await enqueue_oura_sync_job(
            db,
            trigger="scheduled",
            days_back=30,
            idempotency_key="oura_sync:scheduled:later",
        )
        successor_row = await get_job(db, successor.job_id)
        await db.close()
        return scheduled, covered, webhook, running, successor, successor_row

    scheduled, covered, webhook, running, successor, successor_row = asyncio.run(scenario())
    assert covered.job_id == scheduled.job_id
    assert covered.created is False
    assert webhook.created is True
    assert running["id"] == webhook.job_id
    assert successor.created is True
    assert successor_row["payload_json"] == '{"days_back":30,"trigger":"scheduled"}'


def test_stale_running_workload_yields_to_queued_successor(tmp_path):
    from app.migrations.runner import run_migrations

    async def scenario():
        db = await aiosqlite.connect(tmp_path / "stale-successor.db")
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        first = await enqueue_backup_job(
            db,
            trigger="manual",
            idempotency_key=manual_job_key(BACKUP_JOB_KIND, "3" * 32),
        )
        running = await claim_next_job(db, "stale-worker")
        stale_lock = (datetime.now(UTC) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S.%f")
        await db.execute("UPDATE jobs SET locked_at = ? WHERE id = ?", (stale_lock, first.job_id))
        await db.commit()
        successor = await enqueue_backup_job(
            db,
            trigger="manual",
            idempotency_key=manual_job_key(BACKUP_JOB_KIND, "4" * 32),
        )
        now = datetime.now(UTC)
        recovered = await recover_stale_jobs(db, now - timedelta(minutes=1), now=now)
        claimed_successor = await claim_next_job(db, "replacement-worker", now=now)
        rows = await db.execute_fetchall("SELECT id, status FROM jobs ORDER BY id")
        await db.close()
        return first, running, successor, recovered, claimed_successor, rows

    first, running, successor, recovered, claimed_successor, rows = asyncio.run(scenario())
    assert running["id"] == first.job_id
    assert [(item.job_id, item.status) for item in recovered] == [(first.job_id, "failed")]
    assert claimed_successor["id"] == successor.job_id
    assert [(row["id"], row["status"]) for row in rows] == [
        (first.job_id, "failed"),
        (successor.job_id, "running"),
    ]


def test_manual_workload_queue_and_terminal_history_are_bounded(tmp_path):
    from app.migrations.runner import run_migrations

    async def scenario():
        db = await aiosqlite.connect(tmp_path / "bounded.db")
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        await db.executemany(
            "INSERT INTO jobs(kind, payload_json, status, idempotency_key, finished_at) "
            "VALUES('backup', '{\"trigger\":\"manual\"}', 'cancelled', ?, datetime('now'))",
            [(f"old-backup-{index}",) for index in range(100)],
        )
        await db.commit()
        first = await enqueue_backup_job(
            db,
            trigger="manual",
            idempotency_key=manual_job_key(BACKUP_JOB_KIND, "e" * 32),
        )
        coalesced = await enqueue_backup_job(
            db,
            trigger="manual",
            idempotency_key=manual_job_key(BACKUP_JOB_KIND, "f" * 32),
        )
        terminal_after_enqueue = (
            await db.execute_fetchall("SELECT count(*) AS count FROM jobs WHERE status = 'cancelled'")
        )[0]["count"]
        claimed = await claim_next_job(db, "bounded-worker")
        successor = await enqueue_backup_job(
            db,
            trigger="manual",
            idempotency_key=manual_job_key(BACKUP_JOB_KIND, "1" * 32),
        )
        bounded = await enqueue_backup_job(
            db,
            trigger="manual",
            idempotency_key=manual_job_key(BACKUP_JOB_KIND, "2" * 32),
        )
        active = await db.execute_fetchall(
            "SELECT id, status FROM jobs WHERE status IN ('queued', 'running') ORDER BY id"
        )
        await db.close()
        return first, coalesced, terminal_after_enqueue, claimed, successor, bounded, active

    first, coalesced, terminal_count, claimed, successor, bounded, active = asyncio.run(scenario())
    assert coalesced.job_id == first.job_id
    assert terminal_count == 99
    assert claimed["id"] == first.job_id
    assert successor.job_id == bounded.job_id
    assert [(row["id"], row["status"]) for row in active] == [
        (first.job_id, "running"),
        (successor.job_id, "queued"),
    ]
