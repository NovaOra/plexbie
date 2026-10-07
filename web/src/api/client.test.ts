import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, SAMPLE, api, artUrl, loginUrl, mayHaveWorked, plexSignIn, signInReturn } from "./client";

/** A stand-in for the bot's /api: answers every call with `reply` and records what was asked. */
function serve(reply: () => Response) {
  const fetch = vi.fn(async (_url: string, _init?: RequestInit) => reply());
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the client", () => {
  it("talks to the bot, not sample data", () => {
    expect(SAMPLE).toBe(false);
  });

  describe("reads", () => {
    let fetch: ReturnType<typeof serve>;
    beforeEach(() => {
      fetch = serve(() => json({ ok: true }));
    });

    it("go to /api with the session cookie and the site's own header", async () => {
      await api.status();
      expect(fetch).toHaveBeenCalledTimes(1);
      const [url, init] = fetch.mock.calls[0];
      expect(url).toBe("/api/status");
      expect(init?.credentials).toBe("same-origin");
      expect(init?.headers).toMatchObject({ "X-Plexbie": "1", "Content-Type": "application/json" });
      expect(init?.method).toBeUndefined();
    });

    it("encode what goes in the path and the query", async () => {
      await api.title("movie", "a/b?c");
      await api.search("love & war", "tv");
      expect(fetch.mock.calls[0][0]).toBe("/api/titles/movie/a%2Fb%3Fc");
      expect(fetch.mock.calls[1][0]).toBe("/api/search?q=love+%26+war&kind=tv");
    });

    it("return the parsed answer", async () => {
      await expect(api.status()).resolves.toEqual({ ok: true });
    });
  });

  describe("writes", () => {
    it("are POSTs with a JSON body and the site's own header", async () => {
      const fetch = serve(() => json({ ok: true, message: "Approved." }));
      await expect(api.decide("requests", "r/1", true)).resolves.toEqual({ ok: true, message: "Approved." });
      const [url, init] = fetch.mock.calls[0];
      expect(url).toBe("/api/admin/requests/r%2F1/approve");
      expect(init?.method).toBe("POST");
      expect(init?.body).toBe("{}");
      expect(init?.headers).toMatchObject({ "X-Plexbie": "1" });
    });

    it("send what they're given", async () => {
      const fetch = serve(() => json({ ok: true, message: "" }));
      await api.say("123", "hello", false);
      expect(JSON.parse(fetch.mock.calls[0][1]?.body as string)).toEqual({ channelId: "123", message: "hello", allowMassPings: false });
    });

    it("resolve to nothing on 204", async () => {
      serve(() => new Response(null, { status: 204 }));
      await expect(api.logout()).resolves.toBeUndefined();
    });
  });

  describe("errors", () => {
    it("carry the status and the bot's own message", async () => {
      serve(() => json({ error: "Only admins can do that." }, 403));
      const err = await api.cleanupScan().catch((e: unknown) => e);
      expect(err).toBeInstanceOf(ApiError);
      expect(err).toMatchObject({ status: 403, message: "Only admins can do that." });
    });

    it("carry the line of a refused admin action", async () => {
      serve(() => json({ ok: false, message: "Already approved." }, 409));
      await expect(api.decide("requests", "r1", true)).rejects.toMatchObject({ status: 409, message: "Already approved." });
    });

    it("fall back to the status text when the answer isn't JSON", async () => {
      serve(() => new Response("<html>Bad Gateway</html>", { status: 502, statusText: "Bad Gateway" }));
      await expect(api.status()).rejects.toMatchObject({ status: 502, message: "Bad Gateway" });
    });
  });

  describe("an outcome", () => {
    it("is unknown when no answer came back or a server in the way failed", async () => {
      serve(() => { throw new TypeError("Failed to fetch"); });
      expect(mayHaveWorked(await api.cleanupScan().catch((e: unknown) => e))).toBe(true);
      serve(() => new Response("<html>Gateway Timeout</html>", { status: 504, statusText: "Gateway Timeout" }));
      expect(mayHaveWorked(await api.cleanupScan().catch((e: unknown) => e))).toBe(true);
      expect(mayHaveWorked(new DOMException("signal timed out", "TimeoutError"))).toBe(true);
    });

    it("is a no when the bot refused", async () => {
      serve(() => json({ ok: false, message: "Those episodes aren't in that show." }, 409));
      expect(mayHaveWorked(await api.blockedImport("sonarr", "abc").catch((e: unknown) => e))).toBe(false);
    });
  });

  describe("a blocked import", () => {
    it("comes back as a plain no when Sonarr refused it", async () => {
      serve(() => json({ ok: false, message: "Sonarr refused it: no matching episode." }));
      await expect(api.blockedImport("sonarr", "abc")).resolves.toEqual({ ok: false, message: "Sonarr refused it: no matching episode." });
    });

    it("stops waiting once the bot's own wait for Sonarr is long over", async () => {
      const fetch = serve(() => json({ ok: true, message: "Imported 1 file." }));
      const timeout = vi.spyOn(AbortSignal, "timeout");
      await api.blockedImport("sonarr", "abc");
      // The bot waits up to two minutes; giving up first would call a slow import a failure.
      expect(timeout).toHaveBeenCalledOnce();
      expect(timeout.mock.calls[0][0]).toBeGreaterThan(120_000);
      expect(timeout.mock.calls[0][0]).toBeLessThanOrEqual(180_000);
      expect(fetch.mock.calls[0][1]?.signal).toBe(timeout.mock.results[0].value);
    });
  });

  describe("the session", () => {
    it("is null when signed out", async () => {
      serve(() => json({ error: "Sign in first." }, 401));
      await expect(api.session()).resolves.toBeNull();
    });

    it("still fails loudly on any other error", async () => {
      serve(() => json({ error: "Plexbie is starting." }, 503));
      await expect(api.session()).rejects.toMatchObject({ status: 503 });
    });
  });
});

describe("loginUrl", () => {
  it("goes to the chosen sign-in with where to come back to", () => {
    expect(loginUrl()).toBe("/auth/discord/login?next=%2F");
    expect(loginUrl("/requests?x=1", "plex")).toBe("/auth/plex/login?next=%2Frequests%3Fx%3D1");
  });
});

describe("signInReturn", () => {
  it("comes back to the page that asked for a sign-in", () => {
    expect(signInReturn("/title/tv/123", "")).toBe("/title/tv/123");
    expect(signInReturn("/search", "?q=moon&type=tv")).toBe("/search?q=moon&type=tv");
  });

  it("leaves out the last sign-in's notice", () => {
    expect(signInReturn("/", "?login=failed")).toBe("/");
    expect(signInReturn("/library", "?login=expired&page=2")).toBe("/library?page=2");
    expect(signInReturn("/", "?invite=failed")).toBe("/");
  });
});

describe("plexSignIn", () => {
  /** A browser with a popup that opens (or is blocked) and a page that can move on. */
  function browser(popup: { close(): void } | null) {
    const open = vi.fn((_url: string, _name?: string, _features?: string) => popup);
    const assign = vi.fn();
    vi.stubGlobal("window", { open, location: { assign } });
    return { open, assign };
  }
  const click = () => ({ preventDefault: vi.fn() });

  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("opens our own page, waits for Plex and goes where the bot says", async () => {
    const popup = { close: vi.fn() };
    const { open, assign } = browser(popup);
    let checks = 0;
    const fetch = serve(() => (++checks < 3 ? json({ done: false }) : json({ done: true, next: "/requests" })));
    const e = click();
    const status = vi.fn();
    plexSignIn(e, "/home", true, status);
    expect(open.mock.calls[0][0]).toBe("/auth/plex/go?next=%2Fhome&invite=1");
    expect(e.preventDefault).toHaveBeenCalled();
    expect(status).toHaveBeenCalledWith("Waiting for Plex…");
    await vi.advanceTimersByTimeAsync(1500 * 3);
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(fetch.mock.calls[0][0]).toBe("/auth/plex/check");
    expect(popup.close).toHaveBeenCalled();
    expect(assign).toHaveBeenCalledWith("/requests");
  });

  it("leaves the link alone when the popup is blocked", async () => {
    browser(null);
    const fetch = serve(() => json({ done: true }));
    const e = click();
    plexSignIn(e);
    await vi.advanceTimersByTimeAsync(1500 * 3);
    expect(e.preventDefault).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("says why when the bot keeps refusing", async () => {
    const { assign } = browser({ close: vi.fn() });
    const fetch = serve(() => json({ error: "That PIN expired." }, 400));
    const status = vi.fn();
    plexSignIn(click(), "/", false, status);
    await vi.advanceTimersByTimeAsync(1500 * 4);
    expect(status).toHaveBeenLastCalledWith("Waiting for Plex…");
    await vi.advanceTimersByTimeAsync(1500);
    expect(fetch).toHaveBeenCalledTimes(5);
    expect(status).toHaveBeenLastCalledWith("That PIN expired.");
    expect(assign).not.toHaveBeenCalled();
  });

  it("stops a few seconds after the window is closed, checking once more first", async () => {
    const popup = { close: vi.fn(), closed: false };
    const { assign } = browser(popup);
    const fetch = serve(() => json({ done: false }));
    const status = vi.fn();
    plexSignIn(click(), "/", false, status);
    await vi.advanceTimersByTimeAsync(1500 * 2);
    popup.closed = true;
    await vi.advanceTimersByTimeAsync(1500 * 4);
    expect(fetch).toHaveBeenCalledTimes(6);
    expect(status).toHaveBeenLastCalledWith("Sign-in window closed. Press Sign in with Plex again.");
    await vi.advanceTimersByTimeAsync(1500 * 10);
    expect(fetch).toHaveBeenCalledTimes(6);
    expect(assign).not.toHaveBeenCalled();
  });

  it("still signs in when Plex was approved just as the window closed", async () => {
    const popup = { close: vi.fn(), closed: true };
    const { assign } = browser(popup);
    let checks = 0;
    serve(() => (++checks < 3 ? json({ done: false }) : json({ done: true, next: "/title/tv/123" })));
    plexSignIn(click(), "/title/tv/123");
    await vi.advanceTimersByTimeAsync(1500 * 3);
    expect(assign).toHaveBeenCalledWith("/title/tv/123");
  });

  it("goes back to its own page when the window finished the sign-in first", async () => {
    const { assign } = browser({ close: vi.fn() });
    serve(() => json({ done: true }));
    plexSignIn(click(), "/title/tv/123");
    await vi.advanceTimersByTimeAsync(1500);
    expect(assign).toHaveBeenCalledWith("/title/tv/123");
  });

  it("watches one sign-in at a time: pressing again takes over", async () => {
    browser({ close: vi.fn() });
    const fetch = serve(() => json({ done: false }));
    const first = vi.fn();
    const second = vi.fn();
    plexSignIn(click(), "/", false, first);
    await vi.advanceTimersByTimeAsync(1500 * 2);
    expect(fetch).toHaveBeenCalledTimes(2);
    plexSignIn(click(), "/", false, second);
    expect(first).toHaveBeenLastCalledWith("");
    expect(second).toHaveBeenLastCalledWith("Waiting for Plex…");
    await vi.advanceTimersByTimeAsync(1500 * 4);
    expect(fetch).toHaveBeenCalledTimes(6);
    expect(first).toHaveBeenLastCalledWith("");
  });
});

describe("artUrl", () => {
  it("maps each kind of artwork path to an address", () => {
    expect(artUrl(null)).toBeNull();
    expect(artUrl("")).toBeNull();
    expect(artUrl("/img/demo/one.svg")).toBe("/img/demo/one.svg");
    expect(artUrl("ol:123", "w185")).toBe("https://covers.openlibrary.org/b/id/123-M.jpg");
    expect(artUrl("ol:123")).toBe("https://covers.openlibrary.org/b/id/123-L.jpg");
    expect(artUrl("https://example.org/a.jpg")).toBe("https://example.org/a.jpg");
    expect(artUrl("/abc.jpg", "w780")).toBe("https://image.tmdb.org/t/p/w780/abc.jpg");
  });
});
