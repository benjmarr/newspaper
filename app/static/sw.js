// Network-first service worker: always try the server, fall back to the last copy we saved.
const CACHE = "paper-v6";
const LAST = "/__last-edition";
const SHELL = ["/static/style.css?v=6", "/static/app.js?v=6", "/static/icon-192.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function isEditionPage(url) {
  return url.pathname === "/" || url.pathname.startsWith("/edition/") || url.pathname === "/archive" || url.pathname.startsWith("/read/") || url.pathname.startsWith("/img/");
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const cacheable = isEditionPage(url) || url.pathname.startsWith("/static/");
  if (!cacheable) return;

  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const res = await fetch(req);
      // Don't save login redirects or errors.
      if (res.ok && !res.redirected) {
        cache.put(req, res.clone());
        if (url.pathname === "/" || url.pathname.startsWith("/edition/")) cache.put(LAST, res.clone());
      }
      return res;
    } catch (err) {
      const hit = (await cache.match(req)) || (req.mode === "navigate" ? await cache.match(LAST) : undefined);
      if (hit) return hit;
      return new Response(
        "<!doctype html><meta name=viewport content='width=device-width'><body style='font-family:Georgia,serif;padding:2em'>" +
        "<h1>Offline</h1><p>The paper server can't be reached and no saved edition is available on this device yet.</p>",
        { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
    }
  })());
});
