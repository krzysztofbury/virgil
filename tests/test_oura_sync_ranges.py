"""The Oura sync asks for the right range in the parameters each endpoint reads.

Two gaps from production: resting_hr stayed empty because /heartrate takes
start_datetime/end_datetime and silently ignored start_date/end_date, and
today's activity never arrived because Oura leaves the end_date day out.

The fake below answers the way Oura does for those cases. Daily endpoints leave
the end_date day out, /heartrate ignores date parameters, refuses ranges longer
than a week and pages its samples, and the sync runs against it end to end.
"""

import asyncio
import time
from datetime import date, datetime, timedelta

import httpx
import pytest


class FakeOura:
    def __init__(self, today: date):
        self.today = today
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, headers=None, params=None):
        endpoint = url.rsplit("/", 1)[-1]
        params = dict(params or {})
        self.calls.append((endpoint, params))
        if endpoint == "heartrate":
            return self._heartrate(params)
        if endpoint == "daily_activity" and "start_date" in params:
            start, end = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
            days = [start + timedelta(days=n) for n in range((end - start).days)]  # end_date excluded
            data = [{"day": d.isoformat(), "score": 80, "steps": 9000 + n} for n, d in enumerate(days)]
            return httpx.Response(200, json={"data": data, "next_token": None})
        return httpx.Response(200, json={"data": [], "next_token": None})

    def _heartrate(self, params):
        if "start_datetime" not in params:
            # What Oura does with start_date/end_date here: ignores them.
            return httpx.Response(200, json={"data": [], "next_token": None})
        start = datetime.fromisoformat(params["start_datetime"])
        end = datetime.fromisoformat(params["end_datetime"])
        if end - start > timedelta(days=7):
            return httpx.Response(400, json={"detail": "range too long"})
        # Two samples a night, at 23:30 and 23:45 UTC: 01:30 and 01:45 the next
        # day in Warsaw.
        samples = []
        day = start.date()
        while day < end.date():
            for minute, bpm in ((30, 48), (45, 52)):
                stamp = datetime(day.year, day.month, day.day, 23, minute).isoformat() + "+00:00"
                samples.append({"timestamp": stamp, "bpm": bpm, "source": "rest"})
            day += timedelta(days=1)
        page = int(params.get("next_token") or 0)
        chunk = samples[page * 3 : page * 3 + 3]
        more = (page + 1) * 3 < len(samples)
        return httpx.Response(200, json={"data": chunk, "next_token": str(page + 1) if more else None})


@pytest.fixture
def warsaw(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _sync(tmp_path, monkeypatch, days_back):
    async def scenario():
        import aiosqlite

        from app.migrations.runner import run_migrations
        from app.services import oura_api

        fake = FakeOura(date.today())
        monkeypatch.setattr(oura_api.httpx, "AsyncClient", lambda **_kwargs: fake)

        async def token(_db):
            return "token"

        monkeypatch.setattr(oura_api, "ensure_valid_token", token)
        db = await aiosqlite.connect(tmp_path / "oura.db")
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        await oura_api.sync_oura_from_api(db, days_back=days_back)
        rows = {r["date"]: dict(r) for r in await db.execute_fetchall("SELECT * FROM oura_daily")}
        await db.close()
        return fake, rows

    return asyncio.run(scenario())


def test_todays_activity_is_fetched_and_stored(tmp_path, monkeypatch, warsaw):
    fake, rows = _sync(tmp_path, monkeypatch, days_back=3)

    today = date.today().isoformat()
    assert rows[today]["steps"] is not None, "today's activity must arrive"
    (activity,) = [params for endpoint, params in fake.calls if endpoint == "daily_activity"]
    assert activity["end_date"] == (date.today() + timedelta(days=1)).isoformat()
    assert activity["start_date"] == (date.today() - timedelta(days=3)).isoformat()


def test_resting_hr_comes_from_heartrate_samples_on_their_local_day(tmp_path, monkeypatch, warsaw):
    fake, rows = _sync(tmp_path, monkeypatch, days_back=20)

    heartrate = [params for endpoint, params in fake.calls if endpoint == "heartrate"]
    assert heartrate and all("start_datetime" in p and "start_date" not in p for p in heartrate)
    assert any("next_token" in p for p in heartrate), "pages beyond the first must be fetched"
    # 21 days in weeks of at most seven: no request was refused.
    windows = {(p["start_datetime"], p["end_datetime"]) for p in heartrate}
    assert len(windows) == 3

    # Samples stamped 23:30 UTC on day N belong to day N+1 in Warsaw.
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert rows[yesterday]["resting_hr"] == 48
    first_day = (date.today() - timedelta(days=20)).isoformat()
    assert rows.get(first_day, {}).get("resting_hr") is None, "nothing was stamped late on the day before the range"


def test_endless_pages_end_the_fetch_with_an_error(monkeypatch):
    from app.services import oura_api

    class Endless:
        async def get(self, url, headers=None, params=None):
            return httpx.Response(200, json={"data": [{"day": "2026-01-01"}], "next_token": "again"})

    with pytest.raises(oura_api.OuraFetchError, match="pages"):
        asyncio.run(oura_api._fetch_endpoint(Endless(), "daily_sleep", "token", "2026-01-01", "2026-01-02"))
