import { afterEach, describe, expect, it, vi } from "vitest";

// plexbie.com's Worker (cloudflare/worker.js): its visit counter (POST /e) and the headers
// on the site's own pages. D1, the rate limiter and the static files are stand-ins here.
// The Worker is plain JavaScript, so its shape is written out.
type Worker = { fetch(request: Request, env: object, ctx: { waitUntil(p: Promise<unknown>): void }): Promise<Response> };
const worker: Worker = (await import("../../cloudflare/worker.js" as string)).default;

const CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36";
const MIGRATIONS = import.meta.glob<string>("../../cloudflare/migrations/*.sql", { query: "?raw", import: "default", eager: true });

type Row = Record<string, unknown>;

/** The Worker's surroundings: `rows` are the events it stored, by column. */
function site(env: Record<string, unknown> = {}) {
  const rows: Row[] = [];
  const statements: string[] = [];
  const STATS = {
    prepare: (sql: string) => ({
      bind: (...args: unknown[]) => ({
        run: async () => {
          statements.push(sql);
          const columns = /\(([^)]*)\)\s*VALUES/.exec(sql)?.[1].split(",").map((c) => c.trim()) ?? [];
          rows.push(Object.fromEntries(columns.map((c, i) => [c, args[i]])));
        },
      }),
    }),
  };
  const ASSETS = { fetch: async () => new Response("<!doctype html><title>Plexbie</title>", { headers: { "Content-Type": "text/html" } }) };
  return { env: { HOME: "https://home.example", STATS, STATS_SALT: "salt", ASSETS, ...env }, rows, statements };
}

async function send(env: object, body: unknown, { ip = "198.51.100.7", origin = "https://plexbie.com" } = {}) {
  const waits: Promise<unknown>[] = [];
  const request = new Request("https://plexbie.com/e", {
    method: "POST", body: JSON.stringify(body),
    headers: { Origin: origin, "User-Agent": CHROME, "CF-Connecting-IP": ip, "Content-Type": "text/plain" },
  });
  const res = await worker.fetch(request, env, { waitUntil: (p: Promise<unknown>) => waits.push(p) });
  await Promise.all(waits);
  return res;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the visit counter", () => {
  it("stores a page view", async () => {
    const { env, rows } = site();
    expect((await send(env, { e: "pageview", p: "/privacy", r: "news.ycombinator.com" })).status).toBe(204);
    expect(rows).toEqual([expect.objectContaining({ event: "pageview", path: "/privacy", referrer: "news.ycombinator.com", site: "plexbie.com" })]);
  });

  it("keeps the demo's own pages, and campaign tags", async () => {
    const { env, rows } = site();
    await send(env, { e: "pageview", p: "/title/movie/42", r: "tag:reddit" }, { origin: "https://demo.plexbie.com" });
    expect(rows).toEqual([expect.objectContaining({ path: "/title/movie/42", referrer: "tag:reddit", site: "demo" })]);
  });

  it("keeps campaign tags with other characters in them, tidied", async () => {
    const { env, rows } = site();
    await send(env, { e: "pageview", p: "/", r: "tag:r/selfhosted" });
    await send(env, { e: "pageview", p: "/", r: "tag:hacker news+" });
    expect(rows.map((r) => r.referrer)).toEqual(["tag:r/selfhosted", "tag:hacker-news-"]);
  });

  it("doesn't store made-up pages or referrers as written", async () => {
    const { env, rows } = site();
    await send(env, { e: "pageview", p: "/Buy cheap watches at example.net", r: "Buy cheap watches at example.net" });
    expect(rows).toHaveLength(1);
    expect(rows[0].path).toBe("(not found)");
    expect(rows[0].referrer).toBeNull();
  });

  it("drops events past the rate limit", async () => {
    const limit = vi.fn(async () => ({ success: false }));
    const { env, rows } = site({ EVENT_LIMIT: { limit } });
    await send(env, { e: "pageview", p: "/" });
    expect(rows).toHaveLength(0);
    expect(limit).toHaveBeenCalledTimes(1);
  });

  it("limits an IPv6 visitor by their /64, and never hands the limiter an address", async () => {
    const keys: string[] = [];
    const limit = vi.fn(async ({ key }: { key: string }) => { keys.push(key); return { success: true }; });
    const { env, rows } = site({ EVENT_LIMIT: { limit } });
    await send(env, { e: "pageview", p: "/" }, { ip: "2001:db8:1:2:abcd:1234:5678:9abc" });
    await send(env, { e: "pageview", p: "/" }, { ip: "2001:db8:1:2::77" });
    await send(env, { e: "pageview", p: "/" }, { ip: "2001:db8:1:3::77" });
    await send(env, { e: "pageview", p: "/" }, { ip: "198.51.100.7" });
    expect(rows).toHaveLength(4);
    expect(keys[0]).toBe(keys[1]);
    expect(new Set(keys).size).toBe(3);
    for (const k of keys) expect(k).not.toMatch(/[:.]/);
  });

  it("doesn't count addresses in an excluded IPv6 range", async () => {
    const { env, rows } = site({ EXCLUDE_IPS: "203.0.113.0/24, 2001:db8:1:2::/64" });
    await send(env, { e: "pageview", p: "/" }, { ip: "2001:db8:1:2:abcd:1234:5678:9abc" });
    await send(env, { e: "pageview", p: "/" }, { ip: "203.0.113.9" });
    expect(rows).toHaveLength(0);
    await send(env, { e: "pageview", p: "/" }, { ip: "2001:db8:1:3::1" });
    expect(rows).toHaveLength(1);
  });

  it("matches an excluded IPv6 address however it's written", async () => {
    const { env, rows } = site({ EXCLUDE_IPS: "2001:DB8:0:0:0:0:0:1, ::ffff:192.0.2.0/120" });
    await send(env, { e: "pageview", p: "/" }, { ip: "2001:db8::1" });
    await send(env, { e: "pageview", p: "/" }, { ip: "::ffff:192.0.2.77" });
    expect(rows).toHaveLength(0);
  });

  it("ignores an excluded range with no prefix length", async () => {
    const { env, rows } = site({ EXCLUDE_IPS: "2001:db8::/, 203.0.113.0/" });
    await send(env, { e: "pageview", p: "/" }, { ip: "2001:db8:5::1" });
    await send(env, { e: "pageview", p: "/" }, { ip: "198.51.100.7" });
    expect(rows).toHaveLength(2);
  });

  it("has a migration that creates every column it stores", async () => {
    const { env, statements } = site();
    await send(env, { e: "pageview", p: "/" });
    const sql = Object.values(MIGRATIONS).join("\n");
    const table = /CREATE TABLE IF NOT EXISTS events \(([^;]*)\);/.exec(sql)?.[1] ?? "";
    const created = table.split(",").map((c) => c.trim().split(/\s+/)[0]);
    const stored = /\(([^)]*)\)\s*VALUES/.exec(statements[0])?.[1].split(",").map((c) => c.trim()) ?? [];
    expect(stored.length).toBeGreaterThan(0);
    expect(created).toEqual(expect.arrayContaining(stored));
    expect(sql).toMatch(/CREATE INDEX IF NOT EXISTS \w+ ON events \(day/);
    expect(sql).toMatch(/CREATE INDEX IF NOT EXISTS \w+ ON events \(ts/);
  });
});

describe("the site's pages", () => {
  it("carry a content security policy and the other safety headers", async () => {
    const { env } = site();
    const res = await worker.fetch(new Request("https://plexbie.com/privacy"), env, { waitUntil: () => undefined });
    const csp = res.headers.get("Content-Security-Policy") ?? "";
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("media-src 'self' https://media.plexbie.com");
    expect(csp).not.toMatch(/script-src[^;]*unsafe/);
    expect(res.headers.get("X-Content-Type-Options")).toBe("nosniff");
    expect(res.headers.get("Referrer-Policy")).toBe("strict-origin-when-cross-origin");
  });

  it("aren't added to the answers passed through from the household's Plexbie", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { headers: { "Content-Type": "application/json" } })));
    const { env } = site();
    const res = await worker.fetch(new Request("https://plexbie.com/api/session"), env, { waitUntil: () => undefined });
    expect(res.headers.get("Content-Security-Policy")).toBeNull();
  });
});
