import { SAMPLE } from "./client";

/** Phone and browser alerts (Web Push) for members, mostly those without Discord. */

export type AlertState = "unsupported" | "install-first" | "blocked" | "off" | "on";

// iPadOS Safari says it's a Mac; only the touch screen gives it away.
const isIos = () => /iphone|ipad|ipod/i.test(navigator.userAgent)
  || (/macintosh/i.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
const installed = () => window.matchMedia?.("(display-mode: standalone)").matches || (navigator as { standalone?: boolean }).standalone === true;

const headers = { "Content-Type": "application/json", "X-Plexbie": "1" };
async function post(path: string, body: unknown) {
  const r = await fetch(`/api${path}`, { method: "POST", headers, credentials: "same-origin", body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "That didn’t work.");
  return data as { ok: boolean; message?: string };
}

function keyBytes(base64url: string) {
  const pad = "=".repeat((4 - (base64url.length % 4)) % 4);
  const raw = atob((base64url + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

/** A step that may hang (a slow server, a push service) is given up after `ms`. */
function within<T>(ms: number, step: Promise<T>) {
  return Promise.race([step, new Promise<never>((_, fail) => setTimeout(() => fail(new Error("Timed out.")), ms))]);
}

async function registration() {
  return (await navigator.serviceWorker.getRegistration("/")) ?? navigator.serviceWorker.register("/sw.js", { scope: "/" });
}

/** Who turned alerts on in this browser. On a shared computer it may still hold the
 *  last member's alerts; to anyone else they show as off until they turn on their own. */
const OWNER = "plexbie.alerts.for";
let owner: string | null = null;  // when the browser won't store it (private mode)
function ownerIs(who: string | null) {
  owner = who;
  try { if (who) localStorage.setItem(OWNER, who); else localStorage.removeItem(OWNER); } catch { /* private mode */ }
  noteOwner(who);
}
/** The same, where the service worker can read it: when the browser renews the alerts
 *  by itself, the worker only passes them on for this member (public/sw.js). */
const OWNER_NOTE = "/alerts-owner";
function noteOwner(who: string | null) {
  if (typeof caches === "undefined") return;
  caches.open("plexbie-alerts")
    .then(async (c) => { if (who) await c.put(OWNER_NOTE, new Response(who)); else await c.delete(OWNER_NOTE); })
    .catch(() => undefined);
}
function ownedBy(who: string) {
  try { return (localStorage.getItem(OWNER) ?? owner) === who; } catch { return owner === who; }
}

/** The server's copy of this browser's alerts, sent again once a day: the browser may
 *  have renewed them under a new address without the worker hearing, or the server
 *  dropped them. A new address goes at once, and a failed send on the next visit.
 *  Best effort; nothing waits for it. */
const SYNCED = "plexbie.alerts.synced";
let synced: string | null = null;   // when the browser won't store it (private mode)
let sending: string | null = null;  // already on its way this visit
const today = (sub: PushSubscription) => `${new Date().toDateString()} ${sub.endpoint}`;
function markSynced(sub: PushSubscription) {
  synced = today(sub);
  try { localStorage.setItem(SYNCED, synced); } catch { /* private mode */ }
}
function resync(sub: PushSubscription, who: string) {
  const mark = today(sub);
  try { synced = localStorage.getItem(SYNCED) ?? synced; } catch { /* private mode */ }
  if (synced === mark || sending === mark) return;
  sending = mark;
  // Only marked once the server has it: a failed send is tried again on the next visit.
  post("/push/subscribe", { subscription: sub.toJSON() })
    .then(() => { markSynced(sub); noteOwner(who); }, () => undefined)
    .finally(() => { if (sending === mark) sending = null; });
}

export async function alertState(who: string): Promise<AlertState> {
  if (SAMPLE) return (sessionStorage.getItem("sample-alerts") as AlertState) || "off";
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    return isIos() && !installed() ? "install-first" : "unsupported";
  }
  if (Notification.permission === "denied") return "blocked";
  const reg = await navigator.serviceWorker.getRegistration("/");
  const sub = await reg?.pushManager.getSubscription();
  if (!sub || !ownedBy(who)) return "off";
  resync(sub, who);
  return "on";
}

export async function turnOn(who: string): Promise<AlertState> {
  if (SAMPLE) { sessionStorage.setItem("sample-alerts", "on"); return "on"; }
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return permission === "denied" ? "blocked" : "off";
  const r = await fetch("/api/push/key", { credentials: "same-origin" });
  const { key, error } = await r.json().catch(() => ({}));
  if (!r.ok || typeof key !== "string" || !key) throw new Error(error || "That didn’t work.");
  const reg = await registration();
  await navigator.serviceWorker.ready;
  const sub = (await reg.pushManager.getSubscription())
    ?? (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) }));
  // Same browser, same keys: the server moves the alerts to this member if they were someone else's.
  await post("/push/subscribe", { subscription: sub.toJSON() });
  markSynced(sub);
  ownerIs(who);
  return "on";
}

export async function turnOff(): Promise<AlertState> {
  if (SAMPLE) { sessionStorage.setItem("sample-alerts", "off"); return "off"; }
  const reg = await navigator.serviceWorker.getRegistration("/");
  const sub = await reg?.pushManager.getSubscription();
  if (sub) {
    await post("/push/unsubscribe", { endpoint: sub.endpoint }).catch(() => undefined);
    await sub.unsubscribe();
  }
  ownerIs(null);
  return "off";
}

/** Signing out: this browser's alerts stop, so whoever uses it next doesn't get the
 *  last member's. Best effort and quick; signing out goes ahead whatever happens here.
 *  Says whether there were alerts here to turn off. */
export async function forgetThisBrowser(): Promise<boolean> {
  if (SAMPLE) {
    const was = sessionStorage.getItem("sample-alerts") === "on";
    sessionStorage.removeItem("sample-alerts");
    return was;
  }
  ownerIs(null);
  const reg = await navigator.serviceWorker?.getRegistration("/").catch(() => undefined);
  const sub = await reg?.pushManager?.getSubscription().catch(() => null);
  if (!sub) return false;
  await within(3000, post("/push/unsubscribe", { endpoint: sub.endpoint })).catch(() => undefined);
  await within(3000, sub.unsubscribe()).catch(() => undefined);
  return true;
}

/** "Sign out every other session" (Manage → Health) turns every browser's alerts off on
 *  the server, since someone else signed in as this member may have turned some on: the
 *  browser it was pressed in is still signed in, so it sends its own again at once. */
export async function sendAgain(who: string): Promise<void> {
  if (SAMPLE || !("serviceWorker" in navigator)) return;
  const reg = await navigator.serviceWorker.getRegistration("/");
  const sub = await reg?.pushManager?.getSubscription().catch(() => null);
  if (!sub || !ownedBy(who)) return;
  await post("/push/subscribe", { subscription: sub.toJSON() });
  markSynced(sub);
}

export async function sendTest(): Promise<string> {
  if (SAMPLE) return "Sent. It should pop up in a moment.";
  const out = await post("/push/test", {});
  return out.message ?? "Sent.";
}
