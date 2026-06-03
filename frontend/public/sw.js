/**
 * Wactorz Service Worker
 *
 * Strategy:
 *   - index.html / entry points → network-first, cache fallback (ensures fresh JS hashes)
 *   - Hashed assets (/assets/*, *.js, *.css) → cache-first (content-hash busts automatically)
 *   - API calls (/api/*, /ws/*, /mqtt/*) → network-only (never cache)
 *   - Everything else → network-first, fall back to cache
 */

const CACHE = "wactorz-v3";

const NEVER_CACHE = ["/api/", "/ws", "/mqtt"];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then((c) =>
      c.addAll([
        "./site.webmanifest",
        "./favicon.svg",
      ]).catch(() => {}),
    ),
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))),
    ).then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Never intercept API / WebSocket upgrade requests
  if (NEVER_CACHE.some((p) => url.pathname.startsWith(p))) return;
  if (e.request.method !== "GET") return;

  // HTML entry points: network-first so the browser always gets the latest
  // index.html (which references the current content-hashed JS/CSS filenames).
  if (url.pathname === "/" || url.pathname === "/index.html") {
    e.respondWith(
      fetch(e.request)
        .then((res) => {
          if (res.ok) {
            caches.open(CACHE).then((c) => c.put(e.request, res.clone()));
          }
          return res;
        })
        .catch(() => caches.match(e.request)),
    );
    return;
  }

  // Hashed assets: cache-first (Vite content-hash ensures stale files are never reused)
  if (
    url.pathname.startsWith("/assets/") ||
    url.pathname.endsWith(".js") ||
    url.pathname.endsWith(".css") ||
    url.pathname.endsWith(".svg") ||
    url.pathname.endsWith(".ico") ||
    url.pathname.endsWith(".webmanifest")
  ) {
    e.respondWith(
      caches.match(e.request).then((cached) => {
        const fresh = fetch(e.request).then((res) => {
          if (res.ok) {
            caches.open(CACHE).then((c) => c.put(e.request, res.clone()));
          }
          return res;
        });
        return cached ?? fresh;
      }),
    );
    return;
  }

  // Everything else: network-first, cache fallback
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok) {
          caches.open(CACHE).then((c) => c.put(e.request, res.clone()));
        }
        return res;
      })
      .catch(() => caches.match(e.request)),
  );
});
