// PWA basics: caches the app shell for offline/repeat-visit loading and
// lets an install prompt work. Deliberately narrow in scope - map tiles
// (openfreemap) and the maplibre-gl library (unpkg CDN) are cross-origin
// and unbounded/large, so they're left to the browser's normal HTTP
// cache rather than intercepted here; a fully offline map is a separate,
// bigger project than "PWA basics".
const CACHE_NAME = "kosher-map-v2";
const APP_SHELL = [
  "index.html",
  "map-common.js",
  "manifest.json",
  "icons/icon-192.png",
  "icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // businesses.json is regenerated whenever an admin correction goes
  // live (see scripts/export_map_data.py) - always try the network first
  // so visitors see the current data, and only fall back to the last
  // cached copy if they're offline.
  if (url.origin === self.location.origin && url.pathname.endsWith("/businesses.json")) {
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return resp;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // Cross-origin requests (map tiles, maplibre-gl from its CDN) are left
  // untouched - not cached, not intercepted.
  if (url.origin !== self.location.origin) return;

  // Everything else in the app shell: serve from cache first, since it
  // only changes on deploy, not per-visit.
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});
