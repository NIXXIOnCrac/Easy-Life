/* Push service worker for Easy Life.
 *
 * Registered SEPARATELY from sw.js (the offline-shell worker) so the two
 * concerns don't fight over the same registration. This file only handles
 * 'push' and 'notificationclick' events; it does not touch the cache.
 *
 * The push payload is a JSON object:
 *   { "title": "...", "body": "...", "url": "/" }
 * 'url' is optional and defaults to the app root.
 */
self.addEventListener('push', (event) => {
  let data = { title: 'Easy Life', body: '', url: '/' };
  try {
    if (event.data) {
      const parsed = event.data.json();
      if (parsed && typeof parsed === 'object') {
        data = { title: parsed.title || data.title,
                 body: parsed.body || '',
                 url: parsed.url || '/' };
      }
    }
  } catch (e) {
    // Not JSON (or empty): fall back to the raw text as the body.
    if (event.data) data.body = event.data.text();
  }

  const options = {
    body: data.body,
    icon: '/icons/icon-192.png',
    badge: '/icons/icon-192.png',
    data: { url: data.url },
  };

  event.waitUntil(self.registration.showNotification(data.title, options));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          if ('focus' in client) {
            client.navigate(url);
            return client.focus();
          }
        }
        return self.clients.openWindow(url);
      })
  );
});
