// Plexbie's service worker: only alerts. It caches nothing, so the site is
// always the live one; it exists so the household can get notifications.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "Plexbie", {
    body: data.body || "",
    icon: "/brand/plexbie-192.png",
    // The small icon in the status bar: Android uses its outline only, so it's a
    // white TV on transparent (scripts/app-icons.mjs), not the logo, which shows as a box.
    badge: "/brand/badge-96.png",
    tag: data.tag || undefined,
    data: { url: data.url || "/app" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/app", self.location.origin).href;
  event.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const same = open.find((c) => c.url.startsWith(self.location.origin));
    if (same) { await same.focus(); return same.navigate(url); }
    return self.clients.openWindow(url);
  })());
});
