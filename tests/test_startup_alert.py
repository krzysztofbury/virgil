"""A start that leaves a database unmigrated tells the operator once, through
the alert webhook, instead of being restarted over and over."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


@pytest.fixture
def webhook():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(500 if "refuse" in self.path else 204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook", received
    finally:
        server.shutdown()
        server.server_close()


def _start(tmp_path, monkeypatch, url: str, users: list[dict]):
    from fastapi import FastAPI

    import app.central_db as central_db
    import app.config as config
    import app.services.scheduler as scheduler
    import app.user_db as user_db
    from app.main import _start_application

    monkeypatch.setattr(user_db, "USERS_DB_DIR", str(tmp_path))
    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", url)

    async def no_op(*_args, **_kwargs):
        return None

    async def all_users():
        return users

    monkeypatch.setattr(central_db, "init_central_db", no_op)
    monkeypatch.setattr(central_db, "promote_admin_emails", no_op)
    monkeypatch.setattr(central_db, "get_all_users", all_users)
    monkeypatch.setattr(scheduler, "scheduler_loop", no_op)

    async def scenario():
        await user_db.create_user_db("future.db")
        db = await user_db.open_user_db("future.db")
        try:
            # Migrated by a newer image: this one refuses it, as after a rollback.
            await db.execute("INSERT INTO schema_migrations (version, name) VALUES (999, '999_from_the_future.py')")
            await db.commit()
        finally:
            await db.close()
        probe = FastAPI()
        await (await _start_application(probe))
        alert = getattr(probe.state, "startup_alert", None)
        return await alert if alert else None

    return asyncio.run(scenario())


def test_a_degraded_start_posts_one_alert_without_naming_the_account(tmp_path, monkeypatch, webhook):
    url, received = webhook

    assert _start(tmp_path, monkeypatch, url, [{"db_filename": "future.db"}]) is True

    assert len(received) == 1
    text = received[0]["text"]
    assert "started degraded" in text
    assert "1 account database(s) failed their migrations" in text
    assert "future.db" not in text


def test_a_healthy_start_sends_nothing(tmp_path, monkeypatch, webhook):
    url, received = webhook

    assert _start(tmp_path, monkeypatch, url, []) is None

    assert received == []


def test_a_refused_alert_is_logged_without_the_webhook_url(caplog, webhook):
    from app.alerts import send_alert

    url, _received = webhook
    url = url.replace("/hook", "/refuse-SECRET-TOKEN")

    assert asyncio.run(send_alert(url, "test")) is False
    assert "did not accept the alert" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text
