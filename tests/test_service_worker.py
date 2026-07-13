"""Service worker: versioned cache name + no caching of authenticated pages."""


def test_sw_cache_name_is_versioned(client):
    resp = client.get("/service-worker.js")
    assert resp.status_code == 200
    assert "{{SW_VERSION}}" not in resp.text, "version placeholder must be substituted"
    assert "virgil-" in resp.text
    assert resp.headers["cache-control"] == "no-cache"


def test_sw_never_caches_html_pages(client):
    """Privacy regression: cached dashboards/journals stayed readable offline
    after logout. Only /static/ assets and CDN resources may be cache.put —
    HTML falls back to the precached public /offline page."""
    resp = client.get("/service-worker.js")
    source = resp.text
    assert source.count("cache.put") == 2, "Only the static and CDN handlers may write to Cache Storage"
    assert "caches.match('/offline')" in source
