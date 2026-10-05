import { SAMPLE } from "./client";

/** Phone and browser alerts (Web Push) for members, mostly those without Discord. */

export type AlertState = "unsupported" | "install-first" | "blocked" | "off" | "on";

const isIos = () => /iphone|ipad|ipod/i.test(navigator.userAgent);
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

async function registration() {
  return (await navigator.serviceWorker.getRegistration("/")) ?? navigator.serviceWorker.register("/sw.js", { scope: "/" });
}

export async function alertState(): Promise<AlertState> {
  if (SAMPLE) return (sessionStorage.getItem("sample-alerts") as AlertState) || "off";
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    return isIos() && !installed() ? "install-first" : "unsupported";
  }
  if (Notification.permission === "denied") return "blocked";
  const reg = await navigator.serviceWorker.getRegistration("/");
  const sub = await reg?.pushManager.getSubscription();
  return sub ? "on" : "off";
}

export async function turnOn(): Promise<AlertState> {
  if (SAMPLE) { sessionStorage.setItem("sample-alerts", "on"); return "on"; }
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return permission === "denied" ? "blocked" : "off";
  const { key } = await (await fetch("/api/push/key", { credentials: "same-origin" })).json();
  const reg = await registration();
  await navigator.serviceWorker.ready;
  const sub = (await reg.pushManager.getSubscription())
    ?? (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) }));
  await post("/push/subscribe", { subscription: sub.toJSON() });
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
  return "off";
}

export async function sendTest(): Promise<string> {
  if (SAMPLE) return "Sent. It should pop up in a moment.";
  const out = await post("/push/test", {});
  return out.message ?? "Sent.";
}
