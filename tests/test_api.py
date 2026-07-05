"""REST API: auth paths and endpoint contracts."""

KEY = {"X-API-Key": "test-key-123"}


def test_no_key_401(client):
    assert client.get("/api/summary").status_code == 401


def test_wrong_key_401(client):
    assert client.get("/api/summary", headers={"X-API-Key": "wrong"}).status_code == 401


def test_disabled_api_403(client, monkeypatch):
    import app.routers.api as api_module

    monkeypatch.setattr(api_module, "API_KEY", "")
    assert client.get("/api/summary", headers=KEY).status_code == 403


def test_unknown_api_user_503(client, monkeypatch):
    import app.routers.api as api_module

    monkeypatch.setattr(api_module, "API_USER_EMAIL", "ghost@example.com")
    assert client.get("/api/summary", headers=KEY).status_code == 503


def test_summary_shape(auth_client):
    resp = auth_client.get("/api/summary", headers=KEY)
    assert resp.status_code == 200
    body = resp.json()
    for key in ("date", "daily", "feniks", "oura_latest", "training_week", "measurements_latest"):
        assert key in body
    assert "streak_days" in body["feniks"]


def test_habits_range(auth_client):
    resp = auth_client.get("/api/habits?range=7", headers=KEY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["range_days"] == 7
    assert isinstance(body["logs"], list)


def test_habits_range_out_of_bounds_422(auth_client):
    assert auth_client.get("/api/habits?range=999", headers=KEY).status_code == 422


def test_training_empty(auth_client):
    resp = auth_client.get("/api/training", headers=KEY)
    assert resp.status_code == 200
    assert resp.json()["sessions"] == []


def test_experiments_active(auth_client):
    resp = auth_client.get("/api/experiments/active", headers=KEY)
    assert resp.status_code == 200
    assert "experiments" in resp.json()


def test_oura_today_404_when_no_data(auth_client):
    assert auth_client.get("/api/oura/today", headers=KEY).status_code == 404
