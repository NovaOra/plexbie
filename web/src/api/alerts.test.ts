import { afterEach, describe, expect, it, vi } from "vitest";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc";

/** A browser that can do Web Push, holding `sub` (or no subscription), and a stand-in for the bot's /api. */
function browser(sub: { endpoint: string; unsubscribe(): Promise<boolean>; toJSON(): unknown } | null,
                 reply: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  const reg = {
    pushManager: {
      getSubscription: vi.fn(async () => sub),
      subscribe: vi.fn(async () => ({ endpoint: ENDPOINT, toJSON: () => ({ endpoint: ENDPOINT }) })),
    },
  };
  vi.stubGlobal("navigator", {
    userAgent: "test",
    serviceWorker: { getRegistration: vi.fn(async () => reg), register: vi.fn(async () => reg), ready: Promise.resolve(reg) },
  });
  vi.stubGlobal("window", { PushManager: class {}, Notification: class {} });
  vi.stubGlobal("Notification", { permission: "granted", requestPermission: vi.fn(async () => "granted") });
  vi.stubGlobal("localStorage", storage);
  const fetch = vi.fn(async (url: string, init?: RequestInit) => reply(url, init));
  vi.stubGlobal("fetch", fetch);
  return { fetch, reg };
}

const subscription = () => ({
  endpoint: ENDPOINT,
  unsubscribe: vi.fn(async () => true),
  toJSON: () => ({ endpoint: ENDPOINT, keys: { p256dh: "p", auth: "a" } }),
});

/** The bot's /api when all goes well. */
const server = (url: string) => (url === "/api/push/key" ? json({ key: "BPk3" }) : json({ ok: true }));

/** The browser's own storage, kept between page loads (alerts() reloads the module, not this). */
const kept = new Map<string, string>();
const storage = {
  getItem: (k: string) => kept.get(k) ?? null,
  setItem: (k: string, v: string) => void kept.set(k, v),
  removeItem: (k: string) => void kept.delete(k),
};

/** Each test gets the module afresh: it remembers, per visit, what the server said. */
async function alerts() {
  vi.resetModules();
  return import("./alerts");
}

afterEach(() => {
  kept.clear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("signing out", () => {
  it("turns this browser's alerts off, on the server and in the browser", async () => {
    const sub = subscription();
    const { fetch } = browser(sub, () => json({ ok: true }));
    await expect((await alerts()).forgetThisBrowser()).resolves.toBe(true);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/push/unsubscribe");
    expect(init?.method).toBe("POST");
    expect(init?.headers).toMatchObject({ "X-Plexbie": "1" });
    expect(JSON.parse(init?.body as string)).toEqual({ endpoint: ENDPOINT });
    expect(sub.unsubscribe).toHaveBeenCalled();
  });

  it("still lets go of the browser's alerts when the server can't be reached", async () => {
    const sub = subscription();
    browser(sub, () => { throw new TypeError("Failed to fetch"); });
    await expect((await alerts()).forgetThisBrowser()).resolves.toBe(true);
    expect(sub.unsubscribe).toHaveBeenCalled();
  });

  it("doesn't wait forever on a server that doesn't answer", async () => {
    vi.useFakeTimers();
    const sub = subscription();
    browser(sub, () => new Promise<Response>(() => undefined));
    const done = vi.fn();
    (await alerts()).forgetThisBrowser().then(done);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(done).toHaveBeenCalled();
    expect(sub.unsubscribe).toHaveBeenCalled();
  });

  it("asks nothing of the server when alerts were never on here", async () => {
    const { fetch } = browser(null, () => json({ ok: true }));
    await expect((await alerts()).forgetThisBrowser()).resolves.toBe(false);
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("whether alerts are on", () => {
  it("is on for the member who turned them on here, without asking the server", async () => {
    const { fetch } = browser(subscription(), server);
    await (await alerts()).turnOn("ana");
    fetch.mockClear();
    await expect((await alerts()).alertState("ana")).resolves.toBe("on");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("is off for someone else signed in on the same browser, and leaves the alerts alone", async () => {
    const sub = subscription();
    const { fetch } = browser(sub, server);
    await (await alerts()).turnOn("ana");
    fetch.mockClear();
    await expect((await alerts()).alertState("ben")).resolves.toBe("off");
    expect(fetch).not.toHaveBeenCalled();
    expect(sub.unsubscribe).not.toHaveBeenCalled();
  });

  it("is off when nobody here is known to have turned them on", async () => {
    const { fetch } = browser(subscription(), server);
    await expect((await alerts()).alertState("ana")).resolves.toBe("off");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("moves to whoever turns them on next", async () => {
    const { fetch } = browser(subscription(), server);
    await (await alerts()).turnOn("ana");
    const { alertState, turnOn } = await alerts();
    await expect(turnOn("ben")).resolves.toBe("on");
    expect(fetch.mock.calls.at(-1)?.[0]).toBe("/api/push/subscribe");
    await expect(alertState("ben")).resolves.toBe("on");
    await expect(alertState("ana")).resolves.toBe("off");
  });

  it("is off for everyone once the member logs out", async () => {
    browser(subscription(), server);
    await (await alerts()).turnOn("ana");
    const { alertState, forgetThisBrowser } = await alerts();
    await forgetThisBrowser();
    await expect(alertState("ana")).resolves.toBe("off");
  });

  it("still knows within the visit when the browser won't store anything", async () => {
    browser(subscription(), server);
    vi.stubGlobal("localStorage", { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } });
    const { alertState, turnOn } = await alerts();
    await turnOn("ana");
    await expect(alertState("ana")).resolves.toBe("on");
    await expect(alertState("ben")).resolves.toBe("off");
  });
});

describe("keeping the server's copy", () => {
  /** What the page sent in the background has reached the stubbed server. */
  const settle = () => new Promise((done) => setTimeout(done, 0));
  const sent = (fetch: ReturnType<typeof vi.fn>) => fetch.mock.calls.filter(([url]) => url === "/api/push/subscribe").length;
  const day = (d: number) => vi.setSystemTime(new Date(2026, 9, d, 12));

  it("sends it again once on a later day", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    day(7);
    const { fetch } = browser(subscription(), server);
    await (await alerts()).turnOn("ana");
    fetch.mockClear();
    day(8);
    const { alertState } = await alerts();
    await expect(alertState("ana")).resolves.toBe("on");
    await settle();
    expect(sent(fetch)).toBe(1);
    const [, init] = fetch.mock.calls[0];
    expect(init?.headers).toMatchObject({ "X-Plexbie": "1" });
    expect(JSON.parse(init?.body as string).subscription.endpoint).toBe(ENDPOINT);
    await alertState("ana");
    await (await alerts()).alertState("ana");
    await settle();
    expect(sent(fetch)).toBe(1);
  });

  it("sends a renewed address at once", async () => {
    const { fetch, reg } = browser(subscription(), server);
    await (await alerts()).turnOn("ana");
    fetch.mockClear();
    const renewed = "https://fcm.googleapis.com/fcm/send/new";
    reg.pushManager.getSubscription.mockResolvedValue({
      ...subscription(), endpoint: renewed, toJSON: () => ({ endpoint: renewed, keys: { p256dh: "p", auth: "a" } }),
    });
    await expect((await alerts()).alertState("ana")).resolves.toBe("on");
    await settle();
    expect(sent(fetch)).toBe(1);
    expect(JSON.parse(fetch.mock.calls[0][1]?.body as string).subscription.endpoint).toBe(renewed);
  });

  it("sends nothing for someone else signed in on the same browser", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    day(7);
    const { fetch } = browser(subscription(), server);
    await (await alerts()).turnOn("ana");
    fetch.mockClear();
    day(8);
    await expect((await alerts()).alertState("ben")).resolves.toBe("off");
    await settle();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("tries again on the next visit when the server didn't take it", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    day(7);
    let down = false;
    const { fetch } = browser(subscription(), (url) => (down && url === "/api/push/subscribe" ? json({ error: "Too many." }, 429) : server(url)));
    await (await alerts()).turnOn("ana");
    fetch.mockClear();
    day(8);
    down = true;
    await (await alerts()).alertState("ana");
    await settle();
    expect(sent(fetch)).toBe(1);
    down = false;
    await (await alerts()).alertState("ana");
    await settle();
    expect(sent(fetch)).toBe(2);
    await (await alerts()).alertState("ana");
    await settle();
    expect(sent(fetch)).toBe(2);
  });

  it("sends it once a visit when the browser won't store anything", async () => {
    const { fetch } = browser(subscription(), server);
    vi.stubGlobal("localStorage", { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } });
    const { alertState, turnOn } = await alerts();
    await turnOn("ana");
    fetch.mockClear();
    await alertState("ana");
    await alertState("ana");
    await settle();
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("before alerts can be turned on", () => {
  /** Safari without Push: not added to the Home Screen yet. */
  const safari = (userAgent: string, maxTouchPoints: number) => {
    vi.stubGlobal("navigator", { userAgent, maxTouchPoints });
    vi.stubGlobal("window", {});
  };

  it("asks an iPad to add the site to its Home Screen first, though it says it's a Mac", async () => {
    safari("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15", 5);
    await expect((await alerts()).alertState("ana")).resolves.toBe("install-first");
  });

  it("asks an iPhone the same", async () => {
    safari("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1", 5);
    await expect((await alerts()).alertState("ana")).resolves.toBe("install-first");
  });

  it("doesn't ask a Mac, which has no touch screen", async () => {
    safari("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/16.0 Safari/605.1.15", 0);
    await expect((await alerts()).alertState("ana")).resolves.toBe("unsupported");
  });
});

describe("turning alerts on", () => {
  it("says what the server said when it won't hand over its key", async () => {
    browser(null, () => json({ error: "Log in first." }, 401));
    await expect((await alerts()).turnOn("ana")).rejects.toThrow("Log in first.");
  });

  it("doesn't subscribe without a key", async () => {
    const { reg } = browser(null, () => new Response("<html>Bad Gateway</html>", { status: 502 }));
    await expect((await alerts()).turnOn("ana")).rejects.toThrow("That didn’t work.");
    expect(reg.pushManager.subscribe).not.toHaveBeenCalled();
  });
});
