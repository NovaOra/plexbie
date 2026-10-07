import { describe, expect, it, vi } from "vitest";
import worker from "../../public/sw.js?raw";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const ORIGIN = "https://plexbie.example";
const OLD = "https://fcm.googleapis.com/fcm/send/old";
const NEW = "https://fcm.googleapis.com/fcm/send/new";

type Handler = (event: { waitUntil(p: Promise<unknown>): void } & Record<string, unknown>) => void;

/** The service worker with a stand-in browser around it: `owner` is who the page noted
 *  turned alerts on here, `signedIn` who the bot says is signed in now. */
function serviceWorker({ owner, signedIn, tabs = [] }: { owner: string | null; signedIn: string | null; tabs?: unknown[] }) {
  const on: Record<string, Handler> = {};
  const subscribe = vi.fn(async () => ({ endpoint: NEW, toJSON: () => ({ endpoint: NEW, keys: { p256dh: "p", auth: "a" } }) }));
  const self = {
    addEventListener: (name: string, fn: Handler) => { on[name] = fn; },
    location: { origin: ORIGIN },
    registration: { pushManager: { getSubscription: vi.fn(async () => null), subscribe } },
    clients: { matchAll: vi.fn(async () => tabs), openWindow: vi.fn(async () => null) },
  };
  const caches = {
    open: async () => ({ match: async () => (owner === null ? undefined : new Response(owner)) }),
  };
  const fetch = vi.fn(async (url: string, _init?: RequestInit) => {
    if (url === "/api/session") return json(signedIn ? { user: { id: signedIn } } : null);
    if (url === "/api/push/key") return json({ key: "BPk3" });
    return json({ ok: true });
  });
  new Function("self", "caches", "fetch", worker)(self, caches, fetch);
  const fire = async (name: string, fields: Record<string, unknown>) => {
    let done: Promise<unknown> = Promise.resolve();
    on[name]({ ...fields, waitUntil: (p) => { done = p; } });
    await done;
  };
  const posts = () => fetch.mock.calls.filter(([, init]) => init?.method === "POST")
    .map(([url, init]) => ({ url, headers: init?.headers, body: JSON.parse(init?.body as string) }));
  return { self, fire, posts, subscribe };
}

const renewal = { oldSubscription: { endpoint: OLD, options: { applicationServerKey: new ArrayBuffer(65) } } };

describe("when the browser renews this browser's alerts", () => {
  it("tells the server the new address, for the member who turned them on", async () => {
    const sw = serviceWorker({ owner: "ana", signedIn: "ana" });
    await sw.fire("pushsubscriptionchange", renewal);
    expect(sw.subscribe).toHaveBeenCalled();
    expect(sw.posts()).toEqual([
      { url: "/api/push/subscribe", headers: expect.objectContaining({ "X-Plexbie": "1" }),
        body: { subscription: { endpoint: NEW, keys: { p256dh: "p", auth: "a" } } } },
      { url: "/api/push/unsubscribe", headers: expect.objectContaining({ "X-Plexbie": "1" }), body: { endpoint: OLD } },
    ]);
  });

  it("doesn't hand them to someone else signed in on the same browser", async () => {
    const sw = serviceWorker({ owner: "ana", signedIn: "ben" });
    await sw.fire("pushsubscriptionchange", renewal);
    expect(sw.subscribe).toHaveBeenCalled();
    expect(sw.posts()).toEqual([]);
  });

  it("sends nothing when nobody is signed in, or nobody here turned them on", async () => {
    for (const who of [{ owner: "ana", signedIn: null }, { owner: null, signedIn: "ana" }]) {
      const sw = serviceWorker(who);
      await sw.fire("pushsubscriptionchange", renewal);
      expect(sw.posts()).toEqual([]);
    }
  });
});

describe("clicking an alert", () => {
  it("opens the page in a new window when the open tab can't be sent there", async () => {
    const tab = { url: `${ORIGIN}/`, focus: vi.fn(async () => tab), navigate: vi.fn(async () => { throw new TypeError("not controlled"); }) };
    const sw = serviceWorker({ owner: null, signedIn: null, tabs: [tab] });
    await sw.fire("notificationclick", { notification: { close() {}, data: { url: "/requests" } } });
    expect(sw.self.clients.matchAll).toHaveBeenCalledWith({ type: "window" });
    expect(sw.self.clients.openWindow).toHaveBeenCalledWith(`${ORIGIN}/requests`);
  });

  it("uses the open tab when it can", async () => {
    const tab = { url: `${ORIGIN}/`, focus: vi.fn(async () => tab), navigate: vi.fn(async () => tab) };
    const sw = serviceWorker({ owner: null, signedIn: null, tabs: [tab] });
    await sw.fire("notificationclick", { notification: { close() {}, data: {} } });
    expect(tab.navigate).toHaveBeenCalledWith(`${ORIGIN}/`);
    expect(sw.self.clients.openWindow).not.toHaveBeenCalled();
  });
});
