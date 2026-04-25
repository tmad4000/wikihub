// WikiHub service worker — Path B PWA
// Strategy:
//   - HTML / navigations:  network-first with cache fallback (so users see fresh content but offline still loads)
//   - /static/* assets:    cache-first, stale-while-revalidate (instant + auto-refreshing)
//   - /api/*:              never cache (auth + freshness matter; let the network handle it)
//   - Offline fallback:    /offline.html shown when both network and cache fail
//
// Bump CACHE_VERSION whenever the offline page or shell changes; old caches are cleaned on activate.

const CACHE_VERSION = 'v3-2026-04-25';
const SHELL_CACHE = `wikihub-shell-${CACHE_VERSION}`;
const PAGES_CACHE = `wikihub-pages-${CACHE_VERSION}`;
const STATIC_CACHE = `wikihub-static-${CACHE_VERSION}`;

const PRECACHE_URLS = [
  '/offline.html',
  '/static/favicon.svg',
  '/static/manifest.json',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/apple-touch-icon-180.png',
];

const PAGES_CACHE_MAX = 200;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
      .catch((err) => console.warn('[sw] precache failed', err))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((k) => !k.endsWith(CACHE_VERSION))
          .map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// Trim a cache to a max number of entries (LRU-ish: keep newest by inserting trimmed first)
async function trimCache(cacheName, maxEntries) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  if (keys.length <= maxEntries) return;
  const toDelete = keys.slice(0, keys.length - maxEntries);
  await Promise.all(toDelete.map((k) => cache.delete(k)));
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Same-origin only — let cross-origin pass through (Cloudflare, PostHog, etc.)
  if (url.origin !== self.location.origin) return;

  // Never cache APIs (auth, mutations, fresh data)
  if (url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/auth/')) return;

  // Static assets: cache-first with background revalidate (stale-while-revalidate)
  if (url.pathname.startsWith('/static/')) {
    event.respondWith((async () => {
      const cache = await caches.open(STATIC_CACHE);
      const cached = await cache.match(req);
      const networkPromise = fetch(req).then((resp) => {
        if (resp && resp.ok) cache.put(req, resp.clone());
        return resp;
      }).catch(() => cached);
      return cached || networkPromise;
    })());
    return;
  }

  // HTML / navigations: network-first, fall back to cache, then offline page
  const isNavigation = req.mode === 'navigate' ||
    (req.headers.get('accept') || '').includes('text/html');

  if (isNavigation) {
    event.respondWith((async () => {
      try {
        const fresh = await fetch(req);
        if (fresh && fresh.ok) {
          const cache = await caches.open(PAGES_CACHE);
          cache.put(req, fresh.clone());
          // Async trim — don't block the response
          trimCache(PAGES_CACHE, PAGES_CACHE_MAX);
        }
        return fresh;
      } catch (_) {
        const cache = await caches.open(PAGES_CACHE);
        const cached = await cache.match(req);
        if (cached) return cached;
        const shell = await caches.open(SHELL_CACHE);
        const offline = await shell.match('/offline.html');
        return offline || new Response('Offline', { status: 503, statusText: 'Offline' });
      }
    })());
    return;
  }

  // Everything else: network with cache fallback (no put)
  event.respondWith(
    fetch(req).catch(() => caches.match(req))
  );
});

// Allow page to ask for an immediate update
self.addEventListener('message', (event) => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});
