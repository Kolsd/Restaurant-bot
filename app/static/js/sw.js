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
 *
 * ⚠ DORMANT as of 2026-09-24 — this worker never runs in production.
 * The only call to navigator.serviceWorker.register() lives in
 * offline-sync.js, and NO html page loads offline-sync.js (checked across
 * app/static/html: zero references, and zero occurrences of
 * "serviceWorker"). So nothing installs this worker, nothing reads
 * CACHE_NAME, and bumping CACHE_VERSION has no effect on any browser.
 *
 * The rule to bump it in the same commit as a static change is kept on
 * purpose: it costs one line and it is what makes this file safe to switch
 * on later. But it is NOT what makes a deploy reach a browser today —
 * that is the Cache-Control on /static (app/main.py, _CachedStaticFiles).
 * If a tablet is running old code, look there first, not here.
 *
 * Turning this on is a product decision, not a cleanup: an offline shell
 * cache changes what staff see when the wifi drops, and a cache-first
 * strategy over /static/* would reintroduce exactly the staleness the
 * Cache-Control fix just removed unless the version bump is disciplined.
 */

const CACHE_VERSION  = 'v70';
const CACHE_NAME     = `mesio-shell-${CACHE_VERSION}`;

const SHELL_ASSETS = [
  '/dashboard',
  '/login',
  '/settings',
  '/static/css/tokens.css',
  '/static/css/shared.css',
  '/static/js/mesio-utils.js',
  '/static/js/mesio-realtime.js',
  '/static/js/offline-sync.js',
  '/static/js/pages/sidebar.js',
  '/static/js/pages/dashboard.js',
  '/static/js/pages/menu-admin.js',
  '/static/js/pages/team.js',
  '/static/js/pages/reservations.js',
  '/static/js/pages/nps.js',
  '/static/js/pages/locations.js',
  '/static/js/pages/settings.js',
  '/static/js/pages/billing.js',
  '/static/img/logo.png',
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

  // API calls: always network-only.
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(fetch(request));
    return;
  }

  // Diner chat pages: never cache — always serve fresh HTML so JS updates take effect.
  if (url.pathname.startsWith('/chat/')) {
    event.respondWith(fetch(request));
    return;
  }

  // Staff App: always network-first, no cache fallback (operational data —
  // POS/KDS/clock — must never be served stale). Covers the unified /staff
  // shell and its lazy-loaded section scripts.
  if (url.pathname === '/staff' || url.pathname.startsWith('/static/js/staff/')) {
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

  // HTML pages: network-first, fallback to cache.
  // caches.match() returns undefined (not null) on miss — must guard to avoid
  // "Failed to convert value to 'Response'" crash in event.respondWith().
  event.respondWith(
    fetch(request)
      .then(res => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(request, clone));
        }
        return res;
      })
      .catch(() => caches.match(request).then(r => r || new Response('Sin conexión', { status: 503, headers: { 'Content-Type': 'text/plain' } })))
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
