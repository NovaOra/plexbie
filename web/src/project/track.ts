// plexbie.com counts its own visits: pages, where people came from, clicks, TV channels,
// how much of the teaser and the tour is watched, which parts of the home page are reached, and time on page with scroll depth. Each event is one
// small POST to /e on this site (cloudflare/worker.js), stored in Cloudflare D1 for the
// maintainer's dashboard. No cookies and nothing kept on the device. Browsers that send
// Global Privacy Control or Do Not Track aren't counted at all, and nothing is sent while
// developing locally. The public demo (demo.plexbie.com, npm run build:demo) is counted
// the same way, into the same place; household installs never include this file.

type Event = "pageview" | "engage" | "click" | "outbound" | "channel" | "video" | "seen";

const OFF = typeof navigator === "undefined" || import.meta.env.DEV
  || (navigator as Navigator & { globalPrivacyControl?: boolean }).globalPrivacyControl === true
  || navigator.doNotTrack === "1";

/** plexbie.com's collector: on the site itself, or across from the demo. */
const ENDPOINT = import.meta.env.VITE_DEMO === "1" ? "https://plexbie.com/e" : "/e";

export function track(e: Event, fields: { l?: string; v?: number; r?: string; p?: string } = {}) {
  if (OFF) return;
  const body = JSON.stringify({ e, p: location.pathname, ...fields });
  // text/plain keeps the beacon a simple request; it's still JSON inside.
  const sent = navigator.sendBeacon?.(ENDPOINT, new Blob([body], { type: "text/plain" }));
  if (!sent) void fetch(ENDPOINT, { method: "POST", body, keepalive: true, mode: "no-cors", credentials: "include", headers: { "Content-Type": "text/plain" } }).catch(() => undefined);
}

/** Where this visit came from: a campaign tag (?utm_source=…, ?ref=…) or another site. */
function source(): string | undefined {
  const q = new URLSearchParams(location.search);
  const tag = q.get("utm_source") || q.get("ref");
  if (tag) return `tag:${tag.slice(0, 60)}`;
  try {
    const host = new URL(document.referrer).hostname.replace(/^www\./, "");
    return host && host !== location.hostname.replace(/^www\./, "") ? host : undefined;
  } catch {
    return undefined;
  }
}

let first = true;
/** The page being counted: within the site the address has already changed by the time
 *  the time spent on the last page is sent, so it's remembered here. */
let current = "";
let shownSince = 0;
let shownTotal = 0;
let deepest = 0;

function scrolled() {
  const doc = document.documentElement;
  const seen = (window.scrollY + window.innerHeight) / Math.max(doc.scrollHeight, 1);
  deepest = Math.max(deepest, Math.min(100, Math.round(seen * 100)));
}

/** Time on the page just left (only while it was on screen) and how far down it was read. */
function flush() {
  if (shownSince) shownTotal += Date.now() - shownSince;
  shownSince = document.visibilityState === "visible" ? Date.now() : 0;
  const seconds = Math.round(shownTotal / 1000);
  if (seconds >= 1) track("engage", { v: seconds, l: `scroll:${Math.round(deepest / 25) * 25}`, p: current });
  shownTotal = 0;
}

/** A page was opened (the first load or a move within the site). */
export function pageview() {
  if (!first) flush();
  current = location.pathname;
  deepest = 0;
  shownTotal = 0;
  shownSince = document.visibilityState === "visible" ? Date.now() : 0;
  scrolled();
  track("pageview", first ? { r: source() } : {});
  first = false;
}

let started = false;
/** Clicks anywhere (links to other sites as "outbound"), scroll depth, and time on page. */
export function startTracking() {
  if (started || OFF) return;
  started = true;
  document.addEventListener("click", (ev) => {
    const el = (ev.target as Element | null)?.closest?.("a, button") as HTMLAnchorElement | HTMLButtonElement | null;
    // The TV's own controls are counted as channel changes (track("channel")), not clicks.
    if (!el || el.closest(".set__guide, .set__dial, .set__rocker, .set__power")) return;
    const named = el.dataset.track || el.getAttribute("aria-label") || el.textContent?.replace(/\s+/g, " ").trim() || "";
    if (el instanceof HTMLAnchorElement && el.href) {
      const to = new URL(el.href, location.href);
      if (to.protocol === "mailto:") { track("outbound", { l: "email" }); return; }
      if (to.host !== location.host) { track("outbound", { l: `${to.hostname.replace(/^www\./, "")}${to.pathname}`.slice(0, 120) }); return; }
    }
    track("click", { l: named.slice(0, 120) });
  }, { capture: true, passive: true });
  window.addEventListener("scroll", scrolled, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
    else shownSince = Date.now();
  });
  window.addEventListener("pagehide", flush);
}

/** Counts each home-page section marked data-seen once per visit to the page, when its top
 *  passes 60% of the way down the screen. Returns the clean-up. */
export function watchSections(): () => void {
  if (OFF || typeof IntersectionObserver === "undefined") return () => undefined;
  const counted = new Set<string>();
  const io = new IntersectionObserver((entries) => {
    for (const en of entries) {
      const name = (en.target as HTMLElement).dataset.seen;
      if (!en.isIntersecting || !name || counted.has(name)) continue;
      counted.add(name);
      io.unobserve(en.target);
      track("seen", { l: name });
    }
  }, { rootMargin: "0px 0px -40% 0px" });
  document.querySelectorAll<HTMLElement>("[data-seen]").forEach((el) => io.observe(el));
  return () => io.disconnect();
}
