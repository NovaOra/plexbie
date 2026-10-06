// plexbie.com: the Plexbie project's own site (the static build in ../dist-project),
// served by Cloudflare so it stays up whatever any Plexbie server is doing.
//
// The project's maintainer used to run their own household's Plexbie at plexbie.com
// too, before it moved to HOME. So that nothing pointing here breaks:
//   - pages people open (/app/..., /invite/..., a sign-in) are sent there, address and all;
//   - the phone app's traffic (/api, /img, the app's sign-in, downloads, iPhone sources)
//     is passed through to it, because older app versions still talk
//     to plexbie.com, and a redirect would drop their sign-in header.
// Self-hosters don't need any of this: their Plexbie is on their own address.

const FORWARD = [/^\/api\//, /^\/img\//, /^\/auth\/mobile\//, /^\/download\//, /^\/app-source\//];
const REDIRECT = [/^\/app(\/|$)/, /^\/invite(\/|$)/, /^\/auth\//, /^\/setup(\/|$)/];

// ---- stats -------------------------------------------------------------------------
// The project site counts its own visits (POST /e from src/project/track.ts) into the D1
// database STATS, read by the maintainer's dashboard (stats/). No cookies, nothing stored
// on the visitor's device, and no IP address kept: the IP is used for a moment, with
// the browser string, the day and a secret salt, to make that day's anonymous visitor
// number, so one person counts once per day and can't be followed from day to day.
//
// The maintainer's own visits aren't counted: any browser that has opened the dashboard
// (it leaves a plexbie_me cookie across plexbie.com; visitors never get one), and the
// addresses in the EXCLUDE_IPS secret (comma-separated IPs, or IPv4 ranges like
// 203.0.113.0/24), for devices at home that never open it.

/** Where an event may come from: this site, or the public demo (demo.plexbie.com), which
 *  sends its own visits here. Each is stored with its site, so the dashboard can tell them apart. */
const SITES = { "https://plexbie.com": "plexbie.com", "https://www.plexbie.com": "plexbie.com", "https://demo.plexbie.com": "demo" };
const EVENTS = new Set(["pageview", "engage", "click", "outbound", "channel", "video", "seen"]);
const BOTS = /bot|crawl|spider|slurp|preview|headless|lighthouse|pingdom|monitor|curl|wget|python|go-http|java\//i;
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

function matches(ip, rule) {
  const [base, bits] = rule.split("/");
  if (bits === undefined) return ip.toLowerCase() === base.toLowerCase();
  const a = v4(ip), b = v4(base), size = Number(bits);
  if (a === null || b === null || !Number.isInteger(size) || size < 0 || size > 32) return false;
  const block = 2 ** (32 - size);
  return Math.floor(a / block) === Math.floor(b / block);
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

/** One event, its body already read (the reply goes out before this runs). */
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

async function record(request, text, env) {
  const origin = request.headers.get("Origin") || "";
  const ua = request.headers.get("User-Agent") || "";
  // Not counted: no salt (the visitor number could then be turned back into an address),
  // other sites, bots and blank browsers, oversized bodies, anyone asking not to be, and
  // the maintainer.
  const site = Object.hasOwn(SITES, origin) ? SITES[origin] : null;
  if (!env.STATS || !env.STATS_SALT || !site || !ua || BOTS.test(ua)
      || text.length > 2048 || request.headers.get("Sec-GPC") === "1" || request.headers.get("DNT") === "1"
      || isMaintainer(request, env)) return;
  let e;
  try { e = JSON.parse(text); } catch { return; }
  if (!e || !EVENTS.has(e.e)) return;
  const value = Number(e.v);
  const now = Date.now();
  const day = new Date(now).toISOString().slice(0, 10);
  const ip = request.headers.get("CF-Connecting-IP") || "";
  const { browser, os, device } = agent(ua);
  await env.STATS.prepare(`INSERT INTO events (ts, day, event, path, label, value, referrer, country, region, browser, os, device, visitor, site)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).bind(
    now, day, e.e, clip(e.p, 200), clip(e.l, 120), Number.isFinite(value) && value >= 0 && value <= 86400 ? value : null,
    clip(e.r, 120), request.cf?.country || null, clip(request.cf?.region, 60), browser, os, device,
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
    const home = env.HOME.replace(/\/$/, "");
    if (FORWARD.some((r) => r.test(path))) {
      const target = home + path + url.search;
      const headers = new Headers(request.headers);
      // Nothing a visitor sends about where they are reaches the bot: Cloudflare says that.
      for (const h of ["host", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip", "cf-connecting-ip", "cf-visitor", "forwarded"]) headers.delete(h);
      const answer = await fetch(target, {
        method: request.method, headers, redirect: "manual",
        body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
      });
      // plexbie.com sets no cookies, even passing on an answer from HOME.
      const out = new Response(answer.body, answer);
      out.headers.delete("set-cookie");
      return out;
    }
    if (path === "/demo" || path === "/demo/") return Response.redirect("https://demo.plexbie.com/", 302);
    if (REDIRECT.some((r) => r.test(path))) {
      // 308 keeps the method; the household site drops the old /app prefix itself.
      return Response.redirect(home + path + url.search, request.method === "GET" || request.method === "HEAD" ? 301 : 308);
    }
    if (path === "/sw.js" || path === "/manifest.webmanifest") {
      // Browsers that installed the old site's alerts keep their worker (it still shows
      // alerts, and their links lead here, then HOME); an update check just finds nothing.
      return new Response("Not here: this is the Plexbie project's site.", { status: 404 });
    }
    return env.ASSETS.fetch(request);
  },
};
