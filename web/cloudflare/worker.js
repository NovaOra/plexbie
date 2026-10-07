// plexbie.com: the Plexbie project's own site (the static build in ../dist-project),
// served by Cloudflare so it stays up whatever any Plexbie server is doing.
//
// No household's Plexbie is here: each is on its own address. So a household's addresses
// (its pages, the phone app's traffic, downloads, iPhone sources) are answered 404, never
// redirected or passed on anywhere: the app's in plain text, pages with the site's own
// not-found page.

const APP_PATHS = [/^\/api\//, /^\/img\//, /^\/auth\/mobile\//, /^\/download(\/|$)/, /^\/app-source(\/|$)/];
const HOUSEHOLD_PAGES = [/^\/app(\/|$)/, /^\/invite(\/|$)/, /^\/auth\//, /^\/setup(\/|$)/];

// The site's own pages say what they may load: only this site, plus the films and posters
// on media.plexbie.com. Styles allow inline too: the animation library holds the hero's
// outgoing word in place with a style element it adds.
const PAGE_HEADERS = {
  "Content-Security-Policy": "default-src 'self'; img-src 'self' data: https://media.plexbie.com; media-src 'self' https://media.plexbie.com; "
    + "style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
};

// ---- stats -------------------------------------------------------------------------
// The project site counts its own visits (POST /e from src/project/track.ts) into the D1
// database STATS, read by the maintainer's dashboard (stats/). No cookies, nothing stored
// on the visitor's device, and no IP address kept: the IP is used for a moment, with
// the browser string, the day and a secret salt, to make that day's anonymous visitor
// number, so one person counts once per day and can't be followed from day to day.
//
// The maintainer's own visits aren't counted: any browser that has opened the dashboard
// (it leaves a plexbie_me cookie across plexbie.com; visitors never get one), and the
// addresses in the EXCLUDE_IPS secret (comma-separated IPs, or ranges like 203.0.113.0/24
// or 2001:db8:1:2::/64), for devices at home that never open it.
//
// Each address (each IPv6 /64, counted as a salted hash) may send 60 events a minute (the
// EVENT_LIMIT binding in wrangler.jsonc); past that they're dropped. Pages and referrers are
// stored only when they look like what the site itself sends, so made-up text can't fill the
// dashboard's lists. The events table itself is in migrations/0001_events.sql.

/** Where an event may come from: this site, or the public demo (demo.plexbie.com), which
 *  sends its own visits here. Each is stored with its site, so the dashboard can tell them apart. */
const SITES = { "https://plexbie.com": "plexbie.com", "https://www.plexbie.com": "plexbie.com", "https://demo.plexbie.com": "demo" };
const EVENTS = new Set(["pageview", "engage", "click", "outbound", "channel", "video", "seen"]);
const BOTS = /bot|crawl|spider|slurp|preview|headless|lighthouse|pingdom|monitor|curl|wget|python|go-http|java\//i;
/** The pages there are: this site's, and the demo's (a household's site with sample data).
 *  Any other address is a page that doesn't exist, stored as "(not found)". */
const PAGES = /^\/(?:privacy|terms|library|search|schedule|channel|manage|alerts|invite|title\/[a-z]+\/[\w%.-]{1,60})?\/?$/;
/** Where a visit came from, as track.ts sends it: a campaign tag, or another site's name. */
const SOURCE = /^(?:tag:[\w./-]{1,60}|[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})+)$/i;
/** A campaign tag with anything else in it (?ref=hacker news) tidied to fit, so it still counts. */
const tidy = (r) => (r.startsWith("tag:") ? `tag:${r.slice(4).replace(/[^\w./-]+/g, "-").slice(0, 60)}` : r);
const clip = (v, n) => (typeof v === "string" ? v.slice(0, n) : null);

function agent(ua) {
  const browser = /Edg\//.test(ua) ? "Edge" : /OPR\/|Opera/.test(ua) ? "Opera" : /SamsungBrowser/.test(ua) ? "Samsung Internet"
    : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : "Other";
  const os = /iPhone|iPad|iPod/.test(ua) ? "iOS" : /Android/.test(ua) ? "Android" : /Windows/.test(ua) ? "Windows"
    : /CrOS/.test(ua) ? "ChromeOS" : /Mac OS X|Macintosh/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "Other";
  const device = /iPad|Tablet/.test(ua) || (/Android/.test(ua) && !/Mobile/.test(ua)) ? "Tablet" : /Mobi|iPhone/.test(ua) ? "Phone" : "Desktop";
  return { browser, os, device };
}

const ME = /(?:^|;\s*)plexbie_me=1(?:;|$)/;

/** An IPv4 address as a number, or null. */
function v4(ip) {
  const parts = ip.split(".");
  if (parts.length !== 4 || !parts.every((x) => /^\d{1,3}$/.test(x) && Number(x) <= 255)) return null;
  return parts.reduce((n, x) => n * 256 + Number(x), 0);
}

/** An IPv6 address as a 128-bit BigInt (:: and a trailing IPv4 part allowed), or null. */
function v6(ip) {
  let text = ip;
  const tail = /:(\d+\.\d+\.\d+\.\d+)$/.exec(text);
  if (tail) {
    const n = v4(tail[1]);
    if (n === null) return null;
    text = `${text.slice(0, -tail[1].length)}${(n >>> 16).toString(16)}:${(n & 0xffff).toString(16)}`;
  }
  const halves = text.split("::");
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(":") : [];
  const rest = halves.length === 2 && halves[1] ? halves[1].split(":") : [];
  const gap = 8 - head.length - rest.length;
  if (halves.length === 2 ? gap < 1 : gap !== 0) return null;
  const groups = [...head, ...Array(halves.length === 2 ? gap : 0).fill("0"), ...rest];
  if (!groups.every((g) => /^[0-9a-f]{1,4}$/i.test(g))) return null;
  return groups.reduce((n, g) => (n << 16n) | BigInt(parseInt(g, 16)), 0n);
}

function matches(ip, rule) {
  const [base, bits] = rule.split("/");
  if (bits === undefined) {
    const a = v6(ip);
    return ip.toLowerCase() === base.toLowerCase() || (a !== null && a === v6(base));
  }
  if (!/^\d{1,3}$/.test(bits)) return false;
  const size = Number(bits);
  const a = v4(ip), b = v4(base);
  if (a !== null && b !== null) {
    if (!Number.isInteger(size) || size < 0 || size > 32) return false;
    const block = 2 ** (32 - size);
    return Math.floor(a / block) === Math.floor(b / block);
  }
  const x = v6(ip), y = v6(base);
  if (x === null || y === null || !Number.isInteger(size) || size < 0 || size > 128) return false;
  const shift = BigInt(128 - size);
  return x >> shift === y >> shift;
}

/** Who the rate limit counts: the address, or for IPv6 its /64 (one household's network),
 *  hashed with the salt like the visitor number, so the limiter never holds an address. */
function limitKey(ip, salt) {
  const a = v6(ip);
  return visitorOf("limit", salt, a === null ? ip : `${(a >> 64n).toString(16)}/64`, "");
}

function isMaintainer(request, env) {
  if (ME.test(request.headers.get("Cookie") || "")) return true;
  const ip = request.headers.get("CF-Connecting-IP") || "";
  return !!ip && (env.EXCLUDE_IPS || "").split(",").map((r) => r.trim()).filter(Boolean).some((r) => matches(ip, r));
}

async function visitorOf(day, salt, ip, ua) {
  const data = new TextEncoder().encode(`${day}|${salt}|${ip}|${ua}`);
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", data));
  return [...hash.slice(0, 8)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The request body as text, read no further than `limit` bytes ("" past it). */
async function readCapped(request, limit) {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const parts = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) { await reader.cancel().catch(() => undefined); return ""; }
    parts.push(value);
  }
  const all = new Uint8Array(size);
  let at = 0;
  for (const p of parts) { all.set(p, at); at += p.byteLength; }
  return new TextDecoder().decode(all);
}

/** One event, its body already read (the reply goes out before this runs). */
async function record(request, text, env) {
  const origin = request.headers.get("Origin") || "";
  const ua = request.headers.get("User-Agent") || "";
  // Not counted: no salt (the visitor number could then be turned back into an address),
  // other sites, bots and blank browsers, oversized bodies, anyone asking not to be, the
  // maintainer, and anyone sending more than the rate limit allows.
  const site = Object.hasOwn(SITES, origin) ? SITES[origin] : null;
  if (!env.STATS || !env.STATS_SALT || !site || !ua || BOTS.test(ua)
      || text.length > 2048 || request.headers.get("Sec-GPC") === "1" || request.headers.get("DNT") === "1"
      || isMaintainer(request, env)) return;
  let e;
  try { e = JSON.parse(text); } catch { return; }
  if (!e || !EVENTS.has(e.e)) return;
  const ip = request.headers.get("CF-Connecting-IP") || "";
  if (env.EVENT_LIMIT && !(await env.EVENT_LIMIT.limit({ key: await limitKey(ip, env.STATS_SALT) })).success) return;
  const value = Number(e.v);
  const now = Date.now();
  const day = new Date(now).toISOString().slice(0, 10);
  const path = typeof e.p === "string" ? (PAGES.test(e.p) ? e.p : "(not found)") : null;
  const r = typeof e.r === "string" ? tidy(e.r) : "";
  const referrer = SOURCE.test(r) ? r : null;
  const { browser, os, device } = agent(ua);
  await env.STATS.prepare(`INSERT INTO events (ts, day, event, path, label, value, referrer, country, region, browser, os, device, visitor, site)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).bind(
    now, day, e.e, path, clip(e.l, 120), Number.isFinite(value) && value >= 0 && value <= 86400 ? value : null,
    referrer, request.cf?.country || null, clip(request.cf?.region, 60), browser, os, device,
    await visitorOf(day, env.STATS_SALT, ip, ua), site,
  ).run();
}

export default {
  async scheduled(_event, env) {
    // Raw events are kept about 13 months; the dashboard's longest view is a year.
    if (env.STATS) await env.STATS.prepare("DELETE FROM events WHERE ts < ?").bind(Date.now() - 400 * 86400000).run();
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/e" && request.method === "POST") {
      const text = Number(request.headers.get("Content-Length") || 0) > 2048 ? "" : await readCapped(request, 2048).catch(() => "");
      ctx.waitUntil(record(request, text, env).catch((err) => console.log(`stats: ${err}`)));
      return new Response(null, { status: 204, headers: { "Cache-Control": "no-store" } });
    }
    if (url.hostname.startsWith("www.")) {
      url.hostname = url.hostname.slice(4);
      return Response.redirect(url.toString(), 301);
    }
    const path = url.pathname;
    if (path === "/demo" || path === "/demo/") return Response.redirect("https://demo.plexbie.com/", 302);
    if (APP_PATHS.some((r) => r.test(path)) || path === "/sw.js" || path === "/manifest.webmanifest") {
      // Browsers that installed alerts here before plexbie.com was only the project's site
      // keep their worker; an update check just finds nothing.
      return new Response("Not here: this is the Plexbie project's site.", { status: 404 });
    }
    const gone = HOUSEHOLD_PAGES.some((r) => r.test(path));
    let asked = request;
    if (gone) {
      // Always the whole not-found page: a 304 here would leave a person an empty 404.
      asked = new Request(request);
      for (const h of ["if-none-match", "if-modified-since"]) asked.headers.delete(h);
    }
    const page = await env.ASSETS.fetch(asked);
    const out = new Response(page.body, gone ? { status: 404, headers: page.headers } : page);
    for (const [k, v] of Object.entries(PAGE_HEADERS)) out.headers.set(k, v);
    return out;
  },
};
