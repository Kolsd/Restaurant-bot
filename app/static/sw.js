/**
 * Mesio Service Worker — Offline-First Shell Cache
 * Served at /sw.js (root scope) via a dedicated route in dashboard.py.
 *
 * Strategy:
 *   /api/*          → Network-only   (never cache live data)
 *   /static/*       → Cache-first    (assets rarely change; version via CACHE_NAME)
 *   HTML pages      → Network-first, fallback to cache (stale shell beats blank screen)
 *
 * Cache busting: increment CACHE_VERSION on every deploy that changes static assets.
 */

const CACHE_VERSION  = 'v7';  // ← incrementar para limpiar el caché viejo
const CACHE_NAME     = `mesio-shell-${CACHE_VERSION}`;

const SHELL_ASSETS = [
  '/dashboard',
  '/login',
  '/settings',
  '/static/dashboard.css',
  '/static/dashboard-core.js',
  '/static/dashboard-features.js',
  '/static/dashboard-nps-inventory.js',
  '/static/offline-sync.js',
  '/static/logo.png',
];

// ── Install ──────────────────────────────────────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL_ASSETS))
  );
  // Take control immediately without waiting for old SW to expire.
  self.skipWaiting();
});

// ── Activate — prune old caches ──────────────────────────────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys
          .filter(k => k.startsWith('mesio-shell-') && k !== CACHE_NAME)
          .map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch ────────────────────────────────────────────────────────────────────
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  if (url.origin !== self.location.origin) return;

  // API calls: siempre network-only.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(request));
    return;
  }

  // roles.js: siempre network-only, nunca cachear.
  if (url.pathname === '/static/roles.js') {
    event.respondWith(fetch(request));
    return;
  }

  // Páginas de staff: siempre network-first, sin fallback a caché.
  const staffPages = ['/mesero', '/caja', '/bar', '/cocina', '/domiciliario'];
  if (staffPages.includes(url.pathname)) {
    event.respondWith(fetch(request));
    return;
  }

  // Static assets: cache-first.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then(cached => cached || fetch(request).then(res => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(request, clone));
        }
        return res;
      }))
    );
    return;
  }

  // HTML pages: network-first, fallback a caché.
  event.respondWith(
    fetch(request)
      .then(res => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(request, clone));
        }
        return res;
      })
      .catch(() => caches.match(request))
  );
});

// ── Background Sync ──────────────────────────────────────────────────────────
// The 'mesio-sync-queue' tag is registered by offline-sync.js when operations
// are enqueued while offline. The SW replays them once connectivity returns.
self.addEventListener('sync', event => {
  if (event.tag === 'mesio-sync-queue') {
    event.waitUntil(
      // Notify all open clients to run the flush.
      self.clients.matchAll({ type: 'window' }).then(clients => {
        clients.forEach(c => c.postMessage({ type: 'SW_SYNC_NOW' }));
      })
    );
  }
});
