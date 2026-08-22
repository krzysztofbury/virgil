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


def test_static_links_carry_build_version(auth_client):
    """The SW caches /static/ cache-first, so an updated stylesheet under the
    same URL can be served stale on the first load after a deploy (and forever
    in dev, where the cache name only changes per commit). Versioned URLs make
    every build's first paint fetch fresh assets."""
    html = auth_client.get("/").text
    assert "/static/css/app.css?v=" in html
    assert "/static/js/app.js?v=" in html
    login = auth_client.get("/login", follow_redirects=False)
    if login.status_code == 200:  # logged-in sessions get redirected; standalone pages checked via signup
        assert "/static/css/app.css?v=" in login.text
