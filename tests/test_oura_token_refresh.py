"""Oura token refresh: only a refusal disconnects, and one refresh runs at a time.

Any exception from the refresh (a timeout, a 5xx) used to set the integration to
`error`, after which the scheduler and webhooks skipped it until the person went
through OAuth again. Refreshes also ran in parallel, so two callers could spend
the same single-use refresh token.
"""

import asyncio
import functools

import httpx
import pytest

from app.services.encryption import decrypt, encrypt


async def _expired_integration(tmp_path, monkeypatch, name: str = "oura.db"):
    import app.user_db as user_db

    monkeypatch.setattr(user_db, "USERS_DB_DIR", str(tmp_path))
    await user_db.create_user_db(name)
    db = await user_db.open_user_db(name)
    await db.execute(
        "INSERT INTO integrations (provider, client_id, client_secret_enc, access_token_enc, refresh_token_enc, "
        "token_expires_at, status) VALUES ('oura', 'client', ?, ?, ?, '2000-01-01T00:00:00+00:00', 'connected')",
        (encrypt("secret"), encrypt("old-access"), encrypt("old-refresh")),
    )
    await db.commit()
    return db


def _oura_answers(monkeypatch, handler):
    """Route the token endpoint through `handler` instead of the network."""
    import app.services.oura_api as oura_api

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        oura_api.httpx, "AsyncClient", functools.partial(real_client, transport=httpx.MockTransport(handler))
    )


async def _status(db) -> tuple[str, str]:
    row = (await db.execute_fetchall("SELECT status, refresh_token_enc FROM integrations"))[0]
    return row[0], decrypt(row[1])


def _raise_timeout(request):
    raise httpx.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize(
    ("handler", "status"),
    [
        (lambda request: httpx.Response(400, json={"error": "invalid_grant"}), "error"),
        (lambda request: httpx.Response(401, json={"error": "invalid_client"}), "error"),
        (lambda request: httpx.Response(400, json={"error": "invalid_request"}), "connected"),
        (lambda request: httpx.Response(503, text="maintenance"), "connected"),
        (lambda request: httpx.Response(429, json={"error": "rate_limited"}), "connected"),
        (_raise_timeout, "connected"),
        (lambda request: httpx.Response(200, json={"token_type": "bearer"}), "connected"),
    ],
    ids=["invalid-grant", "invalid-client", "other-400", "503", "429", "timeout", "no-token-in-200"],
)
def test_only_a_refused_refresh_disconnects_the_integration(tmp_path, monkeypatch, handler, status):
    from app.services.oura_api import ensure_valid_token

    _oura_answers(monkeypatch, handler)

    async def scenario():
        db = await _expired_integration(tmp_path, monkeypatch)
        try:
            return await ensure_valid_token(db), await _status(db)
        finally:
            await db.close()

    token, stored = asyncio.run(scenario())

    assert token is None
    # A transient failure keeps the refresh token for the next attempt.
    assert stored == (status, "old-refresh")


def test_concurrent_callers_share_one_refresh(tmp_path, monkeypatch):
    from app.services.oura_api import ensure_valid_token

    refreshes = []

    async def handler(request):
        refreshes.append(request.content.decode())
        await asyncio.sleep(0.1)
        return httpx.Response(
            200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 86400}
        )

    _oura_answers(monkeypatch, handler)

    async def scenario():
        import app.user_db as user_db

        first = await _expired_integration(tmp_path, monkeypatch)
        others = [await user_db.open_user_db("oura.db") for _ in range(2)]
        try:
            tokens = await asyncio.gather(*(ensure_valid_token(db) for db in [first, *others]))
            return tokens, await _status(first)
        finally:
            for db in [first, *others]:
                await db.close()

    tokens, stored = asyncio.run(scenario())

    assert len(refreshes) == 1
    assert "refresh_token=old-refresh" in refreshes[0]
    assert tokens == ["new-access"] * 3
    assert stored == ("connected", "new-refresh")
