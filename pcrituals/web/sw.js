/* Service worker: offline shell + static cache for the PWA. */
const CACHE = 'pc-rituals-v14';  // bump to invalidate stale CSS/JS on update
const CORE = ['/', '/index.html', '/tokens.css', '/styles.css', '/liquid-glass.css',
              '/app.js', '/manifest.json', '/vendor/jsqr.js'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(CORE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
  ).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Never cache API calls or the event stream.
  if (url.pathname.startsWith('/api/')) return;
  if (e.request.method !== 'GET') return;

  e.respondWith(
    fetch(e.request).then((res) => {
      // Only cache good responses. Caching a 404/500 once pinned that broken
      // response into the offline cache and served it forever after.
      if (res && res.ok) {
        const clone = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, clone)).catch(() => {});
      }
      return res;
    }).catch(() => caches.match(e.request).then((m) => {
      if (m) return m;
      // Only fall back to the app shell for a navigation. Returning index.html
      // for a missing .css/.js/image hands the wrong content type to the
      // request (an HTML body parsed as JS is a SyntaxError). Let those fail.
      if (e.request.mode === 'navigate') {
        return caches.match('/index.html').then((shell) => shell || Response.error());
      }
      return Response.error();
    }))
  );
});
