var CACHE_NAME = 'virgil-v4';

var PRECACHE_URLS = [
    '/static/css/app.css',
    '/static/js/app.js',
    '/static/js/charts.js',
    '/static/icons/icon-192x192.png',
    '/static/icons/icon-512x512.png',
    '/static/icons/favicon-32x32.png',
    '/static/icons/favicon-64x64.png',
    '/static/icons/screenshot-wide.png',
    '/static/icons/screenshot-narrow.png',
    '/static/manifest.json',
    '/offline'
];

var CDN_URLS = [
    'https://unpkg.com/htmx.org@2.0.8/dist/htmx.min.js',
    'https://cdn.jsdelivr.net/npm/alpinejs@3.15.8/dist/cdn.min.js',
    'https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js',
    'https://unpkg.com/lucide@0.575.0/dist/umd/lucide.min.js',
    'https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.css',
    'https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.js'
];

// Install: precache static assets + CDN URLs
self.addEventListener('install', function(event) {
    event.waitUntil(
        caches.open(CACHE_NAME).then(function(cache) {
            return cache.addAll(PRECACHE_URLS.concat(CDN_URLS));
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

    // CDN — stale-while-revalidate
    if (url.origin !== self.location.origin) {
        event.respondWith(
            caches.match(event.request).then(function(cached) {
                var fetchPromise = fetch(event.request).then(function(response) {
                    var clone = response.clone();
                    caches.open(CACHE_NAME).then(function(cache) {
                        cache.put(event.request, clone);
                    });
                    return response;
                }).catch(function() {
                    return cached;
                });
                return cached || fetchPromise;
            })
        );
        return;
    }

    // Pages — network-first with offline fallback
    if (event.request.headers.get('Accept') && event.request.headers.get('Accept').includes('text/html')) {
        event.respondWith(
            fetch(event.request).then(function(response) {
                var clone = response.clone();
                caches.open(CACHE_NAME).then(function(cache) {
                    cache.put(event.request, clone);
                });
                return response;
            }).catch(function() {
                return caches.match(event.request).then(function(cached) {
                    return cached || caches.match('/offline');
                });
            })
        );
        return;
    }
});
