const CACHE_NAME = "qr-full-pwa-v1";
const ASSETS = [
  "/",
  "/static/index.html",
  "/static/style.css",
  "/static/app.js",
  "/manifest.webmanifest",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png"
];
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE_NAME).then((c) => c.addAll(ASSETS)));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
    ))
  );
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/save_text") || url.pathname.startsWith("/scan") || url.pathname.startsWith("/list") || url.pathname.startsWith("/download")) {
    return;
  }
  e.respondWith(caches.match(e.request).then(resp => resp || fetch(e.request)));
});
