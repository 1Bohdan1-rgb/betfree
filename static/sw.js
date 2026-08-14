const CACHE_NAME = "betfree-cache-v1";
const PRECACHE_URLS = [
  "/login",
  "/staff/login",
  "/static/manifest.json",
  "/static/manifest-staff.json",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  // офлайн-фолбек: staff-розділ (/staff/...) не повинен падати на звичайний
  // /login — інакше та сама проблема з ярликом "На екран Дому" повернеться,
  // тільки вже через service worker, а не через маніфест
  const isStaffRequest = new URL(request.url).pathname.startsWith("/staff/");
  const fallbackUrl = isStaffRequest ? "/staff/login" : "/login";

  event.respondWith(
    fetch(request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        return response;
      })
      .catch(() => caches.match(request).then((cached) => cached || caches.match(fallbackUrl)))
  );
});

self.addEventListener("push", (event) => {
  let data = {};
  if (event.data) {
    try {
      data = event.data.json();
    } catch (e) {
      data = { title: "BetFree", body: event.data.text() };
    }
  }

  const title = data.title || "BetFree";
  const options = {
    body: data.body || "",
    icon: data.icon || "/static/icons/icon-192.png",
    badge: "/static/icons/icon-192.png",
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ("focus" in client) return client.focus();
      }
      if (clients.openWindow) return clients.openWindow("/");
    })
  );
});
