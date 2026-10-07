// Plexbie's service worker: only alerts. It caches no pages, so the site is
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
    data: { url: data.url || "/" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || "/", self.location.origin).href;
  event.waitUntil((async () => {
    // Only a tab this worker controls can be sent elsewhere (a hard-reloaded one isn't);
    // otherwise, or if that fails, the page opens in a new window.
    const open = await self.clients.matchAll({ type: "window" });
    const same = open.find((c) => c.url.startsWith(self.location.origin));
    if (same) {
      try {
        const there = await (await same.focus()).navigate(url);
        if (there) return there;
      } catch { /* falls through to a new window */ }
    }
    return self.clients.openWindow(url);
  })());
});

const keyBytes = (base64url) => {
  const raw = atob((base64url + "=".repeat((4 - (base64url.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
};
const send = (path, body) => fetch(`/api${path}`, {
  method: "POST", credentials: "same-origin", body: JSON.stringify(body),
  headers: { "Content-Type": "application/json", "X-Plexbie": "1" },
});

// Who turned alerts on here, as the page notes it (src/api/alerts.ts), checked against
// whoever is signed in now: on a shared computer that may be someone else.
const forOwner = async () => {
  const note = await (await caches.open("plexbie-alerts")).match("/alerts-owner");
  const owner = note ? await note.text() : "";
  const r = await fetch("/api/session", { credentials: "same-origin" });
  const me = r.ok ? await r.json() : null;
  return !!owner && me?.user?.id === owner;
};

// The browser renewed or dropped this browser's alerts (its push service does that now
// and then): subscribe again with the same key and tell the server the new address, or
// alerts would stop while the page still says they're on. Only the member who turned
// them on gets them; if they aren't signed in here now, the page sends the new address
// when they next visit.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil((async () => {
    const old = event.oldSubscription;
    let sub = event.newSubscription || await self.registration.pushManager.getSubscription();
    if (!sub) {
      let key = old?.options?.applicationServerKey;
      if (!key) {
        const r = await fetch("/api/push/key", { credentials: "same-origin" });
        const { key: fresh } = r.ok ? await r.json() : {};
        if (typeof fresh !== "string" || !fresh) return;
        key = keyBytes(fresh);
      }
      sub = await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    }
    if (!await forOwner()) return;
    await send("/push/subscribe", { subscription: sub.toJSON() });
    if (old && old.endpoint !== sub.endpoint) await send("/push/unsubscribe", { endpoint: old.endpoint });
  })().catch(() => undefined));
});
