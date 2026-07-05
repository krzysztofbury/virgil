"""Service worker must ship a versioned cache name — stale-cache regression."""


def test_sw_cache_name_is_versioned(client):
    resp = client.get("/service-worker.js")
    assert resp.status_code == 200
    assert "{{SW_VERSION}}" not in resp.text, "version placeholder must be substituted"
    assert "virgil-" in resp.text
    assert resp.headers["cache-control"] == "no-cache"
