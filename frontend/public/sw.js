/**
 * SPDX-License-Identifier: Apache-2.0
 */
/**
 * Wactorz Service Worker
 *
 * Registered only when the app is served directly (see index.html): behind
 * Home Assistant's ingress proxy the app lives under a rotating /api/… path where
 * caching is useless, so the SW is never installed there in the first place.
 *
 * It keeps only the app's shell: the page, its content-hashed assets and the
 * icons. Everything else goes to the network untouched and is never stored:
 * the API answers with chats, agents and attachments, and so do the routes it
 * also answers under bare names (`/chats`, `/config`), the sign-in page and the
 * docs. A cache is kept per browser rather than per session, and nothing clears
 * it on sign-out, so what is never stored cannot be read back by the next person.
 *
 * Strategy, by path under the worker's scope:
 *   - the page ("" or index.html) → network-first, cache fallback (the newest
 *     page names the newest hashed assets)
 *   - assets/*, and the icons and manifest → stale-while-revalidate
 *   - anything else → not intercepted
 */

/** Renamed when what may be stored changes: activation deletes every other cache. */
const CACHE = "wactorz-v6";

/** The page itself, as a path under the scope. */
const PAGE = new Set(["", "index.html"]);

/** The shell's own files beside the page, as paths under the scope. */
const SHELL_FILES = new Set(["favicon.svg", "icon.ico", "icon.png", "site.webmanifest"]);

/** Where the worker's scope begins: the dashboard may be served under a prefix. */
function scopePath() {
    const scope = self.registration && self.registration.scope;
    return scope ? new URL(scope).pathname : "/";
}

/** Most entries to keep. The cache had no bound at all: every same-origin GET
 *  was stored and nothing ever evicted, so a long-lived install grew until the
 *  browser evicted the whole origin — losing the offline shell this exists for.
 *  Content-hashed assets make that worse, since each deploy adds a new set of
 *  filenames rather than replacing the old ones. */
const MAX_ENTRIES = 60;

/** Store a response, then trim the cache back to MAX_ENTRIES.
 *  `cache.keys()` yields insertion order, so the head is the oldest. */
async function putBounded(request, response) {
    const cache = await caches.open(CACHE);
    await cache.put(request, response);
    const keys = await cache.keys();
    for (const stale of keys.slice(0, Math.max(0, keys.length - MAX_ENTRIES))) {
        await cache.delete(stale);
    }
}

self.addEventListener("install", e => {
    self.skipWaiting();
    e.waitUntil(
        caches.open(CACHE).then(c => c.addAll(["./site.webmanifest", "./favicon.svg"]).catch(() => {})),
    );
});

self.addEventListener("activate", e => {
    e.waitUntil(
        caches
            .keys()
            .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
            .then(() => self.clients.claim()),
    );
});

/** What the worker does with a request for `path` under its scope: "page", "shell" or null. */
function kindOf(path) {
    if (PAGE.has(path)) return "page";
    if (path.startsWith("assets/") || SHELL_FILES.has(path)) return "shell";
    return null;
}

/** Fetch `request`, storing a good answer. */
function fetchAndKeep(request) {
    return fetch(request).then(res => {
        // Not one that was redirected: for the page that is the sign-in form, and
        // a browser refuses a redirected answer to a navigation served from the cache.
        if (res.ok && !res.redirected) {
            // Cloned synchronously, before `res` is returned and its body read:
            // cloning later, inside the async write, throws "body already used".
            const copy = res.clone();
            void putBounded(request, copy);
        }
        return res;
    });
}

self.addEventListener("fetch", e => {
    if (e.request.method !== "GET") return;
    const url = new URL(e.request.url);
    // Same-origin only: a stored third-party answer would later be served as this origin's.
    if (url.origin !== self.location.origin) return;
    const base = scopePath();
    if (!url.pathname.startsWith(base)) return;
    const kind = kindOf(url.pathname.slice(base.length));
    // Not intercepted: the browser fetches it as if there were no worker, and nothing is kept.
    if (kind === null) return;

    if (kind === "page") {
        e.respondWith(
            fetchAndKeep(e.request).catch(() => caches.match(e.request).then(c => c ?? Response.error())),
        );
        return;
    }
    // Stale-while-revalidate: served from the cache at once and refreshed behind it. The
    // catch is on the fetch itself, so a refresh that fails offline is not an unhandled rejection.
    e.respondWith(
        caches.match(e.request).then(cached => {
            const fresh = fetchAndKeep(e.request).catch(() => Response.error());
            return cached ?? fresh;
        }),
    );
});
