// {{SW_VERSION}} is injected by the /service-worker.js endpoint from the build
// version (VIRGIL_GIT_SHA) — every deploy busts the static cache automatically.
var CACHE_NAME = 'virgil-{{SW_VERSION}}';

var PRECACHE_URLS = [
    '/static/css/app.css',
    '/static/js/app.js',
    '/static/js/charts.js',
    '/static/js/components.js',
    '/static/js/theme.js',
    '/static/vendor/htmx-2.0.8.min.js',
    '/static/vendor/alpinejs-csp-3.15.8.min.js',
    '/static/vendor/chart-4.5.1.umd.min.js',
    '/static/vendor/lucide-0.575.0.min.js',
    '/static/vendor/flatpickr-4.6.13.min.css',
    '/static/vendor/flatpickr-4.6.13.min.js',
    '/static/vendor/fonts/fonts.css',
    '/static/icons/icon-192x192.png',
    '/static/icons/icon-512x512.png',
    '/static/icons/favicon-32x32.png',
    '/static/icons/favicon-64x64.png',
    '/static/manifest.json',
    '/offline'
];

// Install: precache static assets
self.addEventListener('install', function(event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function(cache) {
            return cache.addAll(PRECACHE_URLS);
        }).then(function() {
            return self.skipWaiting();
        })
    );
});

// Activate: purge old caches
self.addEventListener('activate', function(event) {
    event.waitUntil(
        caches.keys().then(function(names) {
            return Promise.all(
                names.filter(function(name) { return name !== CACHE_NAME; })
                    .map(function(name) { return caches.delete(name); })
            );
        }).then(function() {
            return self.clients.claim();
        })
    );
});

// Fetch strategies
self.addEventListener('fetch', function(event) {
    var url = new URL(event.request.url);

    // Only handle http/https requests — skip chrome-extension://, etc.
    if (!url.protocol.startsWith('http')) return;

    // POST requests: network-only passthrough
    if (event.request.method !== 'GET') return;

    // /static/ — cache-first
    if (url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.match(event.request).then(function(cached) {
                return cached || fetch(event.request).then(function(response) {
                    var clone = response.clone();
                    caches.open(CACHE_NAME).then(function(cache) {
                        cache.put(event.request, clone);
                    });
                    return response;
                });
            })
        );
        return;
    }

    // Every asset is served from here; anything from elsewhere goes to the network.
    if (url.origin !== self.location.origin) return;

    // Pages — network-only with offline fallback. Authenticated HTML is NEVER
    // cached: Cache Storage outlives the session cookie, so cached dashboards,
    // medical data and journals would stay readable offline after logout or on
    // a shared device. Only the public precached /offline page is served.
    if (event.request.headers.get('Accept') && event.request.headers.get('Accept').includes('text/html')) {
        event.respondWith(
            fetch(event.request).catch(function() {
                return caches.match('/offline');
            })
        );
        return;
    }
});
