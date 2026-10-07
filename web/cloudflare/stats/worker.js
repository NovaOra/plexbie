// home.plexbie.com/stats: what plexbie.com's visitors do, from the events its Worker
// stores in D1 (../worker.js, the site's src/project/track.ts). Server-rendered HTML,
// a few hundred rows at most per query. Only for the maintainer: Cloudflare Access sits
// in front, and every request's Access token is checked here as well.
//
// It also keeps the project's GitHub numbers (stars, forks, followers, repo traffic,
// release downloads), collected every three hours with the GITHUB_TOKEN secret, because
// GitHub itself only shows the last 14 days of traffic.

const TZ = "America/Chicago";
const RANGES = { 1: "Today", 7: "7 days", 30: "30 days", 90: "90 days", 365: "A year" };

// ---- Access ---------------------------------------------------------------------------
let certs = null;
let certsAt = 0;
/** Access's signing keys, fetched again at most once a minute (a key rotation, or a token
 *  naming a key we haven't seen), so a stream of bad tokens can't make it fetch each time. */
async function keyFor(env, kid) {
  let jwk = certs?.find((k) => k.kid === kid);
  if (!jwk && Date.now() - certsAt > 60000) {
    certsAt = Date.now();
    certs = (await (await fetch(`${env.TEAM}/cdn-cgi/access/certs`)).json()).keys;
    jwk = certs.find((k) => k.kid === kid);
  }
  return jwk;
}
const b64 = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=")), (c) => c.charCodeAt(0));

async function allowed(request, env) {
  const token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token || !env.AUD) return false;
  const [h, p, sig] = token.split(".");
  if (!h || !p || !sig) return false;
  const head = JSON.parse(new TextDecoder().decode(b64(h)));
  const claims = JSON.parse(new TextDecoder().decode(b64(p)));
  const auds = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (head.alg !== "RS256" || claims.iss !== env.TEAM || !auds.includes(env.AUD) || !(claims.exp * 1000 > Date.now())) return false;
  const jwk = await keyFor(env, head.kid);
  if (!jwk) return false;
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  return crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64(sig), new TextEncoder().encode(`${h}.${p}`));
}

// ---- data ----------------------------------------------------------------------------
const all = async (env, sql, ...args) => (await env.STATS.prepare(sql).bind(...args).all()).results;
const one = async (env, sql, ...args) => (await env.STATS.prepare(sql).bind(...args).first()) || {};

async function load(env, days, site) {
  const since = new Date(Date.now() - (days - 1) * 86400000).toISOString().slice(0, 10);
  // ?1 is the first day, ?2 the site (rows from before sites were recorded are plexbie.com's).
  const W = "day >= ?1 AND COALESCE(site, 'plexbie.com') = ?2";
  const [totals, engage, video, daily, sources, countries, devices, browsers, oses, pages, clicks, outbound, channels, scroll, recent, times, seen] =
    await Promise.all([
      one(env, `SELECT SUM(CASE WHEN event='pageview' THEN 1 ELSE 0 END) AS views,
                       COUNT(DISTINCT CASE WHEN event='pageview' THEN day || visitor END) AS visitors,
                       SUM(CASE WHEN event='outbound' AND label LIKE 'github.com%' AND label NOT LIKE 'github.com/sponsors%' THEN 1 ELSE 0 END) AS github,
                       SUM(CASE WHEN event='outbound' AND (label LIKE 'github.com/sponsors%' OR label LIKE 'ko-fi.com%' OR label LIKE 'buymeacoffee.com%') THEN 1 ELSE 0 END) AS support,
                       SUM(CASE WHEN event='outbound' AND label LIKE 'demo.plexbie.com%' THEN 1 ELSE 0 END) AS demo,
                       COUNT(DISTINCT CASE WHEN event='pageview' AND path='/' THEN day || visitor END) AS home
                FROM events WHERE ${W}`, since, site),
      one(env, `SELECT AVG(value) AS seconds, COUNT(*) AS n FROM events WHERE ${W} AND event='engage'`, since, site),
      // Plays before the films carried a label were all the tour.
      all(env, `SELECT COALESCE(label, 'Full tour') AS film, CAST(value AS INTEGER) AS q, COUNT(*) AS n FROM events WHERE ${W} AND event='video' GROUP BY film, q ORDER BY film, q`, since, site),
      all(env, `SELECT day, SUM(event='pageview') AS views, COUNT(DISTINCT CASE WHEN event='pageview' THEN visitor END) AS visitors
                FROM events WHERE ${W} GROUP BY day ORDER BY day`, since, site),
      all(env, `SELECT COALESCE(referrer, 'Direct or unknown') AS k, COUNT(*) AS n FROM events WHERE ${W} AND event='pageview' AND ts IN
                (SELECT MIN(ts) FROM events WHERE ${W} AND event='pageview' GROUP BY day, visitor) GROUP BY k ORDER BY n DESC LIMIT 15`, since, site),
      all(env, `SELECT COALESCE(country, '??') AS k, COUNT(DISTINCT day || visitor) AS n FROM events WHERE ${W} AND event='pageview' GROUP BY k ORDER BY n DESC LIMIT 15`, since, site),
      all(env, `SELECT device AS k, COUNT(DISTINCT day || visitor) AS n FROM events WHERE ${W} AND event='pageview' GROUP BY k ORDER BY n DESC`, since, site),
      all(env, `SELECT browser AS k, COUNT(DISTINCT day || visitor) AS n FROM events WHERE ${W} AND event='pageview' GROUP BY k ORDER BY n DESC`, since, site),
      all(env, `SELECT os AS k, COUNT(DISTINCT day || visitor) AS n FROM events WHERE ${W} AND event='pageview' GROUP BY k ORDER BY n DESC`, since, site),
      all(env, `SELECT path AS k, COUNT(*) AS n FROM events WHERE ${W} AND event='pageview' GROUP BY k ORDER BY n DESC LIMIT 15`, since, site),
      all(env, `SELECT label AS k, COUNT(*) AS n FROM events WHERE ${W} AND event='click' AND label <> '' GROUP BY k ORDER BY n DESC LIMIT 20`, since, site),
      all(env, `SELECT label AS k, COUNT(*) AS n FROM events WHERE ${W} AND event='outbound' GROUP BY k ORDER BY n DESC LIMIT 20`, since, site),
      all(env, `SELECT label AS k, COUNT(*) AS n FROM events WHERE ${W} AND event='channel' GROUP BY k ORDER BY n DESC`, since, site),
      all(env, `SELECT label AS k, COUNT(*) AS n FROM events WHERE ${W} AND event='engage' GROUP BY k ORDER BY k`, since, site),
      all(env, `SELECT ts, event, path, label, value, referrer, country, device, browser FROM events WHERE COALESCE(site, 'plexbie.com') = ?1 ORDER BY ts DESC LIMIT 40`, site),
      all(env, `SELECT ts FROM events WHERE ${W} AND event='pageview' LIMIT 50000`, since, site),
      all(env, `SELECT label AS k, COUNT(DISTINCT day || visitor) AS n FROM events WHERE ${W} AND event='seen' GROUP BY k`, since, site),
    ]);
  const hours = Array(24).fill(0);
  const hourOf = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", hourCycle: "h23" });
  for (const r of times) hours[Number(hourOf.format(r.ts)) % 24]++;
  const github = await loadGithub(env, since);
  return { site, since, totals, engage, video, daily, sources, countries, devices, browsers, oses, pages, clicks, outbound, channels, scroll, recent, hours, github, seen };
}

// ---- GitHub --------------------------------------------------------------------------
// A fine-grained token (`cloudflare/node_modules/.bin/wrangler secret put GITHUB_TOKEN --config cloudflare/stats/wrangler.jsonc`)
// for the repos in REPOS, with "Administration: Read-only": GitHub keeps traffic behind it.

const SCHEMA = [
  "CREATE TABLE IF NOT EXISTS gh_repo (day TEXT, repo TEXT, stars INTEGER, forks INTEGER, watchers INTEGER, issues INTEGER, PRIMARY KEY (day, repo))",
  "CREATE TABLE IF NOT EXISTS gh_traffic (day TEXT, repo TEXT, views INTEGER, view_uniques INTEGER, clones INTEGER, clone_uniques INTEGER, PRIMARY KEY (day, repo))",
  "CREATE TABLE IF NOT EXISTS gh_popular (day TEXT, repo TEXT, kind TEXT, k TEXT, title TEXT, n INTEGER, uniques INTEGER, PRIMARY KEY (day, repo, kind, k))",
  "CREATE TABLE IF NOT EXISTS gh_downloads (day TEXT, repo TEXT, tag TEXT, asset TEXT, n INTEGER, PRIMARY KEY (day, repo, tag, asset))",
  "CREATE TABLE IF NOT EXISTS gh_people (repo TEXT, kind TEXT, login TEXT, at TEXT, PRIMARY KEY (repo, kind, login))",
  "CREATE TABLE IF NOT EXISTS gh_account (day TEXT, login TEXT, followers INTEGER, public_repos INTEGER, PRIMARY KEY (day, login))",
  "CREATE TABLE IF NOT EXISTS gh_runs (ts INTEGER PRIMARY KEY, ok INTEGER, note TEXT)",
  "CREATE TABLE IF NOT EXISTS gh_images (day TEXT, image TEXT, downloads INTEGER, PRIMARY KEY (day, image))",
  // The repo's own GitHub Actions runs, by day: each job checks the repo out, and GitHub
  // counts that as a clone, so they're taken off the clone numbers.
  "CREATE TABLE IF NOT EXISTS gh_ci (repo TEXT, run INTEGER, day TEXT, jobs INTEGER, PRIMARY KEY (repo, run))",
];
let schemaReady = false;
async function ensure(env) {
  if (schemaReady) return;
  await env.STATS.batch(SCHEMA.map((q) => env.STATS.prepare(q)));
  schemaReady = true;
}

const repos = (env) => (env.REPOS || "").split(",").map((r) => r.trim()).filter((r) => /^[\w.-]+\/[\w.-]+$/.test(r));
const short = (repo) => repo.split("/")[1];

async function gh(env, path, accept = "application/vnd.github+json") {
  const res = await fetch(`https://api.github.com${path}`, { headers: {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "plexbie-stats" } });
  if (!res.ok) {
    const why = await res.json().then((b) => b?.message, () => null);
    throw new Error(`${path.split("?")[0]} answered ${res.status}${why ? ` (${String(why).slice(0, 120)})` : ""}`);
  }
  return res.json();
}

/** Every page of a list (stargazers, forks, releases), up to 1,000 rows. */
async function everyPage(env, path, accept) {
  const rows = [];
  for (let page = 1; page <= 10; page++) {
    const got = await gh(env, `${path}${path.includes("?") ? "&" : "?"}per_page=100&page=${page}`, accept);
    rows.push(...got);
    if (got.length < 100) break;
  }
  return rows;
}

/** The repo's own Actions runs since `since` (YYYY-MM-DD) not counted yet, with how
 *  many jobs each had: every job is a checkout, which GitHub counts as a clone. Dependabot's
 *  update runs are Actions runs too. A workflow always has the same jobs, so they're asked
 *  for once per workflow, not per run (a collection has few requests to spare). */
async function actionsCheckouts(env, db, repo, since) {
  const known = new Set((await db.prepare("SELECT run FROM gh_ci WHERE repo = ?").bind(repo).all()).results.map((r) => r.run));
  const runs = [];
  for (let page = 1; page <= 5; page++) {
    const got = await gh(env, `/repos/${repo}/actions/runs?created=%3E%3D${since}&per_page=100&page=${page}`);
    runs.push(...(got.workflow_runs || []));
    if ((got.workflow_runs || []).length < 100) break;
  }
  const jobsOf = new Map();
  const out = [];
  for (const r of runs) {
    if (known.has(r.id) || r.status !== "completed") continue;
    if (!jobsOf.has(r.workflow_id)) {
      const jobs = await gh(env, `/repos/${repo}/actions/runs/${r.id}/jobs?per_page=100`);
      jobsOf.set(r.workflow_id, jobs.total_count || 0);
    }
    out.push({ run: r.id, day: r.created_at.slice(0, 10), jobs: jobsOf.get(r.workflow_id) });
  }
  return out;
}

/** A container image's total downloads (every pull: Unraid, docker run, Compose, updates,
 *  CI). GitHub's API doesn't give it, but the package's public page shows the exact count. */
async function imagePulls(owner, name) {
  for (const kind of ["users", "orgs"]) {
    const res = await fetch(`https://github.com/${kind}/${owner}/packages/container/package/${name}`, {
      headers: { "User-Agent": "Mozilla/5.0 (compatible; plexbie-stats)" } });
    if (res.status === 404) continue;
    if (!res.ok) throw new Error(`${name} package page answered ${res.status}`);
    const found = /Total downloads<\/span>\s*<h3 title="(\d+)"/.exec(await res.text());
    if (!found) throw new Error(`${name} package page has no download count any more`);
    return Number(found[1]);
  }
  throw new Error(`${name} package page not found`);
}

/** One collection: today's snapshot of each repo, and GitHub's last 14 days of traffic. */
async function collectGithub(env) {
  await ensure(env);
  const db = env.STATS;
  if (!env.GITHUB_TOKEN) {
    await db.prepare("INSERT OR REPLACE INTO gh_runs (ts, ok, note) VALUES (?, 0, ?)").bind(Date.now(), "No GITHUB_TOKEN secret yet.").run();
    return;
  }
  const day = new Date().toISOString().slice(0, 10);
  const writes = [];
  const problems = [];
  for (const repo of repos(env)) {
    try {
      const base = `/repos/${repo}`;
      // Each part on its own: one GitHub refuses (the stargazer list is admins-only since
      // July 2026) mustn't cost the rest. A refused part is reported and left out.
      const part = (p, quiet403 = false) => p.catch((err) => {
        if (!(quiet403 && / answered 403/.test(err.message))) problems.push(`${short(repo)}: ${err.message}`);
        return null;
      });
      const [info, views, clones, referrers, paths, releases, stars, forks] = await Promise.all([
        gh(env, base), part(gh(env, `${base}/traffic/views?per=day`)), part(gh(env, `${base}/traffic/clones?per=day`)),
        part(gh(env, `${base}/traffic/popular/referrers`)), part(gh(env, `${base}/traffic/popular/paths`)),
        part(everyPage(env, `${base}/releases`)), part(everyPage(env, `${base}/stargazers`, "application/vnd.github.star+json"), true),
        part(everyPage(env, `${base}/forks?sort=newest`)),
      ]);
      writes.push(db.prepare(`INSERT INTO gh_repo (day, repo, stars, forks, watchers, issues) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (day, repo) DO UPDATE SET stars = excluded.stars, forks = excluded.forks, watchers = excluded.watchers, issues = excluded.issues`)
        .bind(day, repo, info.stargazers_count, info.forks_count, info.subscribers_count, info.open_issues_count));
      for (const v of views?.views || []) {
        writes.push(db.prepare(`INSERT INTO gh_traffic (day, repo, views, view_uniques, clones, clone_uniques) VALUES (?, ?, ?, ?, 0, 0)
          ON CONFLICT (day, repo) DO UPDATE SET views = excluded.views, view_uniques = excluded.view_uniques`).bind(v.timestamp.slice(0, 10), repo, v.count, v.uniques));
      }
      for (const c of clones?.clones || []) {
        writes.push(db.prepare(`INSERT INTO gh_traffic (day, repo, views, view_uniques, clones, clone_uniques) VALUES (?, ?, 0, 0, ?, ?)
          ON CONFLICT (day, repo) DO UPDATE SET clones = excluded.clones, clone_uniques = excluded.clone_uniques`).bind(c.timestamp.slice(0, 10), repo, c.count, c.uniques));
      }
      if (referrers && paths) writes.push(db.prepare("DELETE FROM gh_popular WHERE day = ? AND repo = ?").bind(day, repo));
      for (const r of referrers || []) writes.push(db.prepare("INSERT OR REPLACE INTO gh_popular VALUES (?, ?, 'referrer', ?, NULL, ?, ?)").bind(day, repo, r.referrer, r.count, r.uniques));
      for (const p of paths || []) writes.push(db.prepare("INSERT OR REPLACE INTO gh_popular VALUES (?, ?, 'path', ?, ?, ?, ?)").bind(day, repo, p.path, p.title, p.count, p.uniques));
      for (const rel of releases || []) for (const a of rel.assets || []) {
        writes.push(db.prepare("INSERT OR REPLACE INTO gh_downloads VALUES (?, ?, ?, ?, ?)").bind(day, repo, rel.tag_name, a.name, a.download_count));
      }
      for (const st of stars || []) if (st.user) writes.push(db.prepare("INSERT OR IGNORE INTO gh_people VALUES (?, 'star', ?, ?)").bind(repo, st.user.login, st.starred_at));
      for (const f of forks || []) if (f.owner) writes.push(db.prepare("INSERT OR IGNORE INTO gh_people VALUES (?, 'fork', ?, ?)").bind(repo, f.owner.login, f.created_at));
      // GitHub keeps 14 days of clones; the runs from then on are what's taken off them.
      const ci = await actionsCheckouts(env, db, repo, new Date(Date.now() - 15 * 86400000).toISOString().slice(0, 10)).catch((err) => {
        problems.push(`${short(repo)}: ${/ answered 403/.test(err.message) ? "the token can't read Actions (give it Actions: read-only), so build checkouts count as clones" : err.message}`);
        return [];
      });
      for (const c of ci) writes.push(db.prepare("INSERT OR IGNORE INTO gh_ci VALUES (?, ?, ?, ?)").bind(repo, c.run, c.day, c.jobs));
    } catch (err) {
      problems.push(`${short(repo)}: ${err.message}`);
    }
  }
  for (const image of (env.IMAGES || "").split(",").map((i) => i.trim()).filter((i) => /^[\w.-]+\/[\w.-]+$/.test(i))) {
    try {
      const [o, name] = image.split("/");
      writes.push(db.prepare("INSERT OR REPLACE INTO gh_images VALUES (?, ?, ?)").bind(day, image, await imagePulls(o, name)));
    } catch (err) {
      problems.push(err.message);
    }
  }
  const owner = repos(env)[0]?.split("/")[0];
  if (owner) {
    try {
      const u = await gh(env, `/users/${owner}`);
      writes.push(db.prepare("INSERT OR REPLACE INTO gh_account VALUES (?, ?, ?, ?)").bind(day, owner, u.followers, u.public_repos));
    } catch (err) {
      problems.push(`${owner}: ${err.message}`);
    }
  }
  for (let i = 0; i < writes.length; i += 200) await db.batch(writes.slice(i, i + 200));
  await db.prepare("INSERT OR REPLACE INTO gh_runs (ts, ok, note) VALUES (?, ?, ?)").bind(Date.now(), problems.length ? 0 : 1, problems.join("; ") || null).run();
}

async function loadGithub(env, since) {
  await ensure(env);
  const latestOf = (table) => `day = (SELECT MAX(day) FROM ${table} x WHERE x.repo = t.repo)`;
  const [now, then, traffic, totals, popular, downloads, dlThen, people, run, account, accountThen, pulls, pullsThen, ciDays] = await Promise.all([
    all(env, `SELECT repo, stars, forks, watchers, issues FROM gh_repo t WHERE ${latestOf("gh_repo")}`),
    all(env, `SELECT repo, stars, forks FROM gh_repo t WHERE day = (SELECT MIN(day) FROM gh_repo x WHERE x.repo = t.repo AND day >= ?)`, since),
    all(env, `SELECT day, SUM(views) AS views, SUM(view_uniques) AS uniques, SUM(clones) AS clones FROM gh_traffic WHERE day >= ? GROUP BY day ORDER BY day`, since),
    one(env, `SELECT SUM(views) AS views, SUM(view_uniques) AS uniques, SUM(clones) AS clones, SUM(clone_uniques) AS cloners FROM gh_traffic WHERE day >= ?`, since),
    all(env, `SELECT repo, kind, k, title, n FROM gh_popular t WHERE ${latestOf("gh_popular")} ORDER BY n DESC`),
    all(env, `SELECT repo, tag, asset, n FROM gh_downloads t WHERE ${latestOf("gh_downloads")} ORDER BY n DESC`),
    one(env, `SELECT SUM(n) AS n FROM gh_downloads t WHERE day = (SELECT MIN(day) FROM gh_downloads x WHERE x.repo = t.repo AND day >= ?)`, since),
    all(env, "SELECT repo, kind, login, at FROM gh_people ORDER BY at DESC LIMIT 20"),
    one(env, "SELECT ts, ok, note FROM gh_runs ORDER BY ts DESC LIMIT 1"),
    one(env, "SELECT login, followers, public_repos FROM gh_account ORDER BY day DESC LIMIT 1"),
    one(env, "SELECT followers FROM gh_account WHERE day >= ? ORDER BY day LIMIT 1", since),
    one(env, "SELECT SUM(downloads) AS n FROM gh_images t WHERE day = (SELECT MAX(day) FROM gh_images x WHERE x.image = t.image)"),
    one(env, "SELECT SUM(downloads) AS n FROM gh_images t WHERE day = (SELECT MIN(day) FROM gh_images x WHERE x.image = t.image AND day >= ?)", since),
    all(env, "SELECT day, repo, SUM(jobs) AS jobs FROM gh_ci WHERE day >= ? GROUP BY day, repo", since),
  ]);
  // Clones by everyone else. Each Actions job is one unique cloner to GitHub (a fresh
  // runner) but several clones (a checkout is a few git fetches: about 3), so it's the
  // unique cloners that are taken off, and each day's clones are shared out in proportion.
  // Our own git pulls (one cloner a day) can't be told apart and still count.
  const ciOn = Object.fromEntries(ciDays.map((r) => [`${r.day}|${r.repo}`, r.jobs]));
  const perRepo = await all(env, "SELECT day, repo, clones, clone_uniques FROM gh_traffic WHERE day >= ?", since);
  let builds = 0, outside = 0, cloners = 0;
  for (const r of perRepo) {
    const ci = ciOn[`${r.day}|${r.repo}`] || 0, count = r.clones || 0, uniq = r.clone_uniques || 0;
    const others = Math.max(0, uniq - ci);
    const theirs = uniq ? Math.round(count * others / uniq) : 0;
    cloners += others;
    outside += theirs;
    builds += count - theirs;
  }
  return { now, then, traffic, totals, popular, downloads, dlThen, people, run, account, accountThen, pulls, pullsThen,
           clones: { outside, builds, cloners, counted: ciDays.length > 0 } };
}

// ---- page ----------------------------------------------------------------------------
const e = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const num = (n) => Number(n || 0).toLocaleString("en-US");
const flag = (cc) => (/^[A-Z]{2}$/.test(cc) ? String.fromCodePoint(...[...cc].map((c) => 0x1f1a5 + c.charCodeAt(0))) + " " : "");
const regionName = new Intl.DisplayNames(["en"], { type: "region" });
const country = (cc) => (/^[A-Z]{2}$/.test(cc) ? `${flag(cc)}${regionName.of(cc)}` : "Unknown");

function tile(label, value, note) {
  return `<div class="tile"><div class="tile__label">${e(label)}</div><div class="tile__value">${e(value)}</div>${note ? `<div class="tile__note">${e(note)}</div>` : ""}</div>`;
}

/** A ranked list as bars: labels and numbers in ink, the bar carries the size. */
function ranked(title, rows, fmt = (k) => k, empty = "Nothing yet.") {
  const max = Math.max(1, ...rows.map((r) => r.n));
  const body = rows.length ? rows.map((r) => `
    <li title="${e(fmt(r.k))}: ${num(r.n)}"><span class="rk__k">${e(fmt(r.k))}</span><span class="rk__n">${num(r.n)}</span>
      <span class="rk__bar" style="width:${Math.max(2, (r.n / max) * 100).toFixed(1)}%"></span></li>`).join("") : `<li class="muted">${e(empty)}</li>`;
  return `<section class="card"><h2>${e(title)}</h2><ol class="rk">${body}</ol></section>`;
}

/** Visitors per day as columns, with a hover title per day and a table for screen readers. */
function dailyChart(daily, since, days) {
  return columns("Visitors per day", daily, since, days, { key: "visitors", blank: { views: 0, visitors: 0 },
    tip: (d) => `${d.visitors} visitors, ${d.views} page views`,
    head: ["Visitors", "Page views"], cells: (d) => [d.visitors, d.views] });
}

/** One number per day as columns: `key` sets the height, `tip` and `cells` the detail. */
function columns(title, rows, since, days, { key, blank, tip, head, cells }) {
  const byDay = new Map(rows.map((d) => [d.day, d]));
  const list = [];
  for (let i = 0; i < days; i++) {
    const day = new Date(Date.parse(since) + i * 86400000).toISOString().slice(0, 10);
    list.push(byDay.get(day) || { day, ...blank });
  }
  const W = 960, H = 220, pad = 28, gap = 2;
  const max = Math.max(1, ...list.map((d) => d[key]));
  const bw = Math.max(2, (W - pad) / list.length - gap);
  const bars = list.map((d, i) => {
    const h = (d[key] / max) * (H - 30);
    const x = pad + i * (bw + gap);
    return `<rect x="${x.toFixed(1)}" y="${(H - 18 - h).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(h, d[key] ? 2 : 0).toFixed(1)}" rx="${Math.min(4, bw / 2).toFixed(1)}">
      <title>${e(d.day)}: ${e(tip(d))}</title></rect>
      <rect class="hit" x="${x.toFixed(1)}" y="0" width="${(bw + gap).toFixed(1)}" height="${H}"><title>${e(d.day)}: ${e(tip(d))}</title></rect>`;
  }).join("");
  const ticks = [0, Math.round(max / 2), max].map((v) => `<text x="0" y="${(H - 18 - (v / max) * (H - 30) + 4).toFixed(1)}">${v}</text>
      <line x1="${pad - 4}" x2="${W}" y1="${(H - 18 - (v / max) * (H - 30)).toFixed(1)}" y2="${(H - 18 - (v / max) * (H - 30)).toFixed(1)}"/>`).join("");
  const labels = [list[0], list[Math.floor(list.length / 2)], list[list.length - 1]].map((d, i) =>
    `<text class="x" x="${i === 0 ? pad : i === 1 ? W / 2 : W}" y="${H - 2}" text-anchor="${i === 0 ? "start" : i === 1 ? "middle" : "end"}">${e(d.day.slice(5))}</text>`).join("");
  const table = list.map((d) => `<tr><td>${e(d.day)}</td>${cells(d).map((c) => `<td>${e(c)}</td>`).join("")}</tr>`).join("");
  return `<section class="card wide"><h2>${e(title)}</h2>
    <svg viewBox="0 0 ${W} ${H}" class="daily" role="img" aria-label="${e(title)}">${ticks}${bars}${labels}</svg>
    <details><summary>As a table</summary><table><thead><tr><th>Day</th>${head.map((h) => `<th>${e(h)}</th>`).join("")}</tr></thead><tbody>${table}</tbody></table></details></section>`;
}

function hoursChart(hours) {
  const max = Math.max(1, ...hours);
  return `<section class="card"><h2>Time of day <small>(Chicago)</small></h2><div class="hours">${hours.map((n, h) =>
    `<span title="${h}:00 to ${h}:59: ${n} page views" style="--h:${(n / max * 100).toFixed(1)}%"><i></i><b>${h % 6 === 0 ? h : ""}</b></span>`).join("")}</div></section>`;
}

/** plexbie.com's TV: channel 01 plays the teaser, 02 the three-minute tour. The names match
 *  FILMS[…].channel in src/project/Screening.tsx, which is what the site sends. */
const FILMS = [["Teaser", "The teaser (channel 01)"], ["Full tour", "The tour (channel 02)"]];
/** The home page's sections, top to bottom, as marked with data-seen in src/project/Project.tsx. */
const SECTIONS = ["TV", "Features", "Plugins", "Self-host", "Tune in"];

const playsOf = (video, film, q) => video.filter((r) => (film == null || r.film === film) && r.q === q).reduce((a, r) => a + r.n, 0);

function funnel(video, film, title) {
  const at = Object.fromEntries(video.filter((r) => r.film === film).map((r) => [r.q, r.n]));
  const start = at[0] || 0;
  const rows = [[0, "Pressed play"], [25, "A quarter in"], [50, "Halfway"], [75, "Three quarters"], [100, "To the end"]];
  return `<section class="card"><h2>${e(title)}</h2><ol class="rk">${rows.map(([q, name]) => {
    const n = at[q] || 0;
    return `<li title="${name}: ${n}"><span class="rk__k">${name}</span><span class="rk__n">${num(n)}${q && start ? ` · ${Math.round((n / start) * 100)}%` : ""}</span>
      <span class="rk__bar" style="width:${start ? Math.max(2, (n / start) * 100).toFixed(1) : 0}%"></span></li>`;
  }).join("")}</ol></section>`;
}

/** How far down the home page visitors got: each section's share of the home page's visitors. */
function reached(seen, home) {
  const at = Object.fromEntries(seen.map((r) => [r.k, r.n]));
  return `<section class="card"><h2>Home page sections reached <small>(of ${num(home)} visitors)</small></h2><ol class="rk">${SECTIONS.map((k) => {
    const n = at[k] || 0;
    return `<li title="${e(k)}: ${num(n)}"><span class="rk__k">${e(k)}</span><span class="rk__n">${num(n)}${home ? ` · ${Math.round((n / home) * 100)}%` : ""}</span>
      <span class="rk__bar" style="width:${home && n ? Math.max(2, (n / home) * 100).toFixed(1) : 0}%"></span></li>`;
  }).join("")}</ol></section>`;
}

function recentTable(rows) {
  const t = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  return `<section class="card wide"><h2>Latest</h2><div class="scroll"><table><thead><tr><th>When</th><th>What</th><th>Page</th><th>Detail</th><th>From</th><th>Where</th><th>On</th></tr></thead><tbody>${rows.map((r) => `
    <tr><td>${e(t.format(r.ts))}</td><td>${e(r.event)}</td><td>${e(r.path)}</td><td>${e([r.label, r.value != null ? (r.event === "engage" ? `${r.value}s` : r.event === "video" ? `${r.value}%` : r.value) : ""].filter(Boolean).join(" · "))}</td>
    <td>${e(r.referrer || "")}</td><td>${e(r.country ? country(r.country) : "")}</td><td>${e(`${r.device || ""} ${r.browser || ""}`.trim())}</td></tr>`).join("") || `<tr><td colspan="7" class="muted">Nothing yet.</td></tr>`}</tbody></table></div></section>`;
}

function githubSection(g, since, days) {
  const when = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const status = !g.run.ts ? "Not collected yet."
    : `Last checked ${when.format(g.run.ts)}${g.run.ok ? "." : `, with a problem: ${g.run.note}`}`;
  const head = `<div class="section"><h2 id="github">GitHub</h2><p class="muted">${e(status)}</p>
    <form method="post" action="/stats/github/refresh"><button type="submit">Check now</button></form></div>`;
  if (!g.now.length) {
    return `${head}<section class="card"><p>No GitHub numbers yet. Add a fine-grained token for the repos with
      <b>Administration: Read-only</b> as the <code>GITHUB_TOKEN</code> secret, then press <b>Check now</b>.</p></section>`;
  }
  const sum = (rows, k) => rows.reduce((n, r) => n + (r[k] || 0), 0);
  const thenOf = (repo) => g.then.find((r) => r.repo === repo) || {};
  const starsNew = g.now.reduce((n, r) => n + r.stars - (thenOf(r.repo).stars ?? r.stars), 0);
  const forksNew = g.now.reduce((n, r) => n + r.forks - (thenOf(r.repo).forks ?? r.forks), 0);
  const plus = (n) => (n > 0 ? `+${num(n)} in this range` : "");
  // latest.json is release bookkeeping, not something people download.
  const fileRows = g.downloads.filter((r) => !/\.json$/i.test(r.asset));
  const appFiles = fileRows.filter((r) => /\.(apk|ipa)$/i.test(r.asset));
  const downloads = sum(fileRows, "n");
  const kind = (ext) => num(sum(appFiles.filter((r) => r.asset.toLowerCase().endsWith(ext)), "n"));
  const t = g.totals;
  const perRepo = g.now.map((r) => `${short(r.repo)} ${num(r.stars)}`).join(" · ");
  const label = (r) => (g.now.length > 1 ? ` · ${short(r.repo)}` : "");
  const refs = g.popular.filter((r) => r.kind === "referrer").slice(0, 15).map((r) => ({ k: `${r.k}${label(r)}`, n: r.n }));
  const paths = g.popular.filter((r) => r.kind === "path").slice(0, 15).map((r) => ({ k: r.k.replace(/^\/[^/]+\//, ""), n: r.n }));
  const files = fileRows.slice(0, 15).map((r) => ({ k: r.asset, n: r.n }));
  const day = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric", year: "numeric" });
  const people = `<section class="card"><h2>Newest stars and forks</h2><div class="scroll"><table><thead><tr><th>When</th><th>Who</th><th>What</th></tr></thead><tbody>${
    g.people.map((p) => `<tr><td>${e(day.format(Date.parse(p.at)))}</td><td><a href="https://github.com/${e(p.login)}">${e(p.login)}</a></td>
      <td>${p.kind === "star" ? "Starred" : "Forked"} ${e(short(p.repo))}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">Nobody yet.</td></tr>`}</tbody></table></div>
    <p class="muted small">Since July 2026 GitHub only lists who starred to a repo's admins, and may refuse it to a token; the star count above still counts them.</p></section>`;
  return `${head}
<div class="tiles">
  ${tile("Stars", num(sum(g.now, "stars")), [plus(starsNew), perRepo].filter(Boolean).join(" · "))}
  ${tile("Forks", num(sum(g.now, "forks")), plus(forksNew))}
  ${tile("Watchers", num(sum(g.now, "watchers")))}
  ${tile("Followers", num(g.account.followers), g.account.followers != null && g.accountThen.followers != null ? plus(g.account.followers - g.accountThen.followers) : "")}
  ${tile("Repo views", num(t.views), `${num(t.uniques)} unique a day, added up`)}
  ${g.clones.counted
    ? tile("Clones by others", num(g.clones.outside), `${num(g.clones.cloners)} unique a day, added up · ${num(g.clones.builds)} by Plexbie's own builds left out · your git pulls still count`)
    : tile("Clones", num(t.clones), "GitHub Actions' own checkouts count too")}
  ${tile("Release downloads", num(downloads), [appFiles.length ? `APK ${kind(".apk")} · IPA ${kind(".ipa")}` : "", plus(downloads - (g.dlThen.n ?? downloads))].filter(Boolean).join(" · "))}
  ${g.pulls.n == null ? "" : tile("Image pulls", num(g.pulls.n), ["every install and update, yours included: GitHub doesn't say whose", plus(g.pulls.n - (g.pullsThen.n ?? g.pulls.n))].filter(Boolean).join(" · "))}
  ${tile("Open issues and PRs", num(sum(g.now, "issues")))}
</div>
<div class="grid">
  ${columns("Repo views per day", g.traffic, since, Math.min(days, 365), { key: "views", blank: { views: 0, uniques: 0, clones: 0 },
    tip: (d) => `${d.views} views (${d.uniques} unique), ${d.clones} clones`, head: ["Views", "Unique", "Clones"], cells: (d) => [d.views, d.uniques, d.clones] })}
  ${ranked("Came to GitHub from", refs, undefined, "Nothing yet.").replace("</h2>", " <small>(GitHub's last 14 days)</small></h2>")}
  ${ranked("Most viewed on GitHub", paths).replace("</h2>", " <small>(GitHub's last 14 days)</small></h2>")}
  ${ranked("Release downloads by file", files)}
  ${people}
</div>
<p class="muted small">GitHub keeps 14 days of traffic, so views and clones here start the day collection began. GitHub counts your own visits too; it doesn't say whose they are.</p>`;
}

function page(d, days) {
  const t = d.totals;
  const plays = playsOf(d.video, null, 0);
  const done = playsOf(d.video, null, 100);
  const current = new Set(FILMS.map(([film]) => film));
  // Channels from the old line-up (Requests, Plex access, …) are now cards below the TV.
  const channelRows = d.channels.map((r) => ({ k: current.has(r.k) ? r.k : `${r.k} (old line-up)`, n: r.n }));
  const secs = Math.round(d.engage.seconds || 0);
  const scrollRows = d.scroll.map((r) => ({ k: r.k?.replace("scroll:", "") + "%", n: r.n }));
  const demo = d.site === "demo";
  const q = (n, s) => `?days=${n}${s === "demo" ? "&amp;site=demo" : ""}`;
  const nav = Object.entries(RANGES).map(([n, label]) => `<a href="${q(n, d.site)}"${Number(n) === days ? ' aria-current="page"' : ""}>${label}</a>`).join("");
  const siteNav = [["plexbie.com", "plexbie.com"], ["demo", "Demo"]].map(([s, label]) =>
    `<a href="${q(days, s)}"${s === d.site ? ' aria-current="page"' : ""}>${label}</a>`).join("");
  const name = demo ? "demo.plexbie.com" : "plexbie.com";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><meta name="color-scheme" content="dark"><title>Plexbie stats</title>
<link rel="icon" href="https://plexbie.com/brand/plexbie-96.png">
<style>
:root { --field:#10172b; --panel:#18213a; --rule:#2d385b; --ink:#f7f1f6; --muted:#aeb8d8; --screen:#ffd1e4; --tally:#ff5c93; }
* { box-sizing: border-box; } body { margin:0; background:var(--field); color:var(--ink); font:15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1180px; margin: 0 auto; padding: 24px 16px 64px; display: grid; gap: 16px; }
.navs { display:flex; flex-wrap:wrap; gap:6px 18px; }
header { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:12px; }
h1 { margin:0; font-size:1.6rem; letter-spacing:-0.01em; display:flex; align-items:center; gap:10px; } h1 img { border-radius:7px; }
nav { display:flex; flex-wrap:wrap; gap:6px; } nav a { color:var(--muted); text-decoration:none; padding:8px 12px; border-radius:999px; border:1px solid var(--rule); min-height:40px; display:inline-flex; align-items:center; }
nav a[aria-current] { background:var(--screen); color:#141b30; border-color:var(--screen); font-weight:700; }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:12px; }
.tile, .card { background:var(--panel); border:1px solid var(--rule); border-radius:14px; padding:16px; min-width:0; }
.tile__label { color:var(--muted); font-size:.82rem; } .tile__value { font-size:1.9rem; font-weight:800; letter-spacing:-0.02em; margin-top:2px; } .tile__note { color:var(--muted); font-size:.8rem; }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:16px; } .wide { grid-column:1 / -1; }
h2 { margin:0 0 12px; font-size:1rem; } h2 small, .muted { color:var(--muted); font-weight:400; }
.rk { list-style:none; margin:0; padding:0; display:grid; gap:6px; }
.rk li { position:relative; display:flex; justify-content:space-between; gap:12px; padding:6px 10px; border-radius:6px; overflow:hidden; }
.rk__k { position:relative; z-index:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .rk__n { position:relative; z-index:1; color:var(--muted); font-variant-numeric:tabular-nums; white-space:nowrap; }
.rk__bar { position:absolute; inset:0 auto 0 0; background:rgb(255 209 228 / .16); border-right:2px solid var(--screen); border-radius:6px 4px 4px 6px; }
.daily { width:100%; height:auto; display:block; } .daily rect { fill:var(--screen); } .daily rect.hit { fill:transparent; } .daily rect.hit:hover { fill:rgb(255 255 255 / .05); }
.daily text { fill:var(--muted); font-size:11px; } .daily line { stroke:var(--rule); stroke-width:1; }
.hours { display:grid; grid-template-columns:repeat(24, 1fr); gap:2px; align-items:end; height:140px; }
.hours span { position:relative; height:100%; display:flex; flex-direction:column; justify-content:flex-end; }
.hours i { display:block; height:max(var(--h), 2px); background:var(--screen); border-radius:4px 4px 0 0; opacity:.9; } .hours b { color:var(--muted); font-size:10px; font-weight:400; height:14px; }
details { margin-top:10px; color:var(--muted); } summary { cursor:pointer; min-height:32px; }
table { width:100%; border-collapse:collapse; font-size:.86rem; font-variant-numeric:tabular-nums; } th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--rule); vertical-align:top; }
th { color:var(--muted); font-weight:600; } .scroll { overflow-x:auto; }
footer, .small { color:var(--muted); font-size:.82rem; }
.section { display:flex; flex-wrap:wrap; align-items:center; gap:6px 16px; margin-top:12px; }
.section h2 { font-size:1.25rem; margin:0; } .section p { margin:0; flex:1 1 220px; }
.section form { margin:0; } a { color:var(--screen); }
button { font:inherit; color:var(--ink); background:transparent; border:1px solid var(--rule); border-radius:999px; padding:6px 14px; min-height:36px; cursor:pointer; }
button:hover { border-color:var(--screen); } code { color:var(--screen); }
</style></head><body><main>
<header><h1><img src="https://plexbie.com/brand/plexbie-64.png" alt="" width="34" height="34"> Plexbie stats</h1>
  <div class="navs"><nav aria-label="Site">${siteNav}</nav><nav aria-label="Range">${nav}</nav></div></header>
<div class="section"><h2>${name}</h2><p class="muted">This browser isn't counted on plexbie.com or the demo, and neither is home.</p><a href="#github">GitHub numbers ↓</a></div>
<div class="tiles">
  ${tile("Visitors", num(t.visitors), "one per person per day")}
  ${tile("Page views", num(t.views))}
  ${tile("Time on a page", secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`, "average, while on screen")}
  ${demo ? "" : tile("Video plays", num(plays), `teaser ${num(playsOf(d.video, "Teaser", 0))} · tour ${num(playsOf(d.video, "Full tour", 0))}${plays ? ` · ${Math.round((done / plays) * 100)}% to the end` : ""}`)}
  ${demo ? "" : tile("Went to the demo", num(t.demo), "from plexbie.com")}
  ${tile("GitHub clicks", num(t.github))}
  ${tile("Support clicks", num(t.support), "GitHub Sponsors, Ko-fi and Buy Me a Coffee")}
</div>
<div class="grid">
  ${dailyChart(d.daily, d.since, Math.min(days, 365))}
  ${ranked("Came from", d.sources)}
  ${ranked("Countries", d.countries, country)}
  ${ranked("Pages", d.pages)}
  ${demo ? "" : FILMS.map(([film, title]) => funnel(d.video, film, title)).join("")}
  ${demo ? "" : ranked("TV channels picked", channelRows)}
  ${demo ? "" : reached(d.seen, t.home || 0)}
  ${ranked("Links to other sites", d.outbound)}
  ${ranked("Buttons and links on the page", d.clicks)}
  ${ranked("How far down they read", scrollRows)}
  ${hoursChart(d.hours)}
  ${ranked("Devices", d.devices)}
  ${ranked("Browsers", d.browsers)}
  ${ranked("Systems", d.oses)}
  ${recentTable(d.recent)}
</div>
${githubSection(d.github, d.since, days)}
<footer>plexbie.com's and the demo's own counts: no cookies, no IP addresses kept, and browsers sending Do Not Track or Global Privacy Control aren't counted. Visitors are counted once per day each. Raw events are kept about 13 months.</footer>
</main></body></html>`;
}

// Whoever gets past Access is the maintainer: this tells plexbie.com's counter (../worker.js)
// to leave the browser out, on any network. Set across plexbie.com, renewed on each visit.
const ME_COOKIE = "plexbie_me=1; Domain=plexbie.com; Path=/; Max-Age=34560000; Secure; HttpOnly; SameSite=Lax";

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(collectGithub(env));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/stats")) return new Response("Not found", { status: 404 });
    if (!(await allowed(request, env).catch(() => false))) {
      return new Response("Not allowed.", { status: 403, headers: { "Cache-Control": "no-store" } });
    }
    if (url.pathname === "/stats/github/refresh") {
      if (request.method !== "POST") return new Response("Use the button.", { status: 405, headers: { Allow: "POST" } });
      const last = await ensure(env).then(() => one(env, "SELECT ts FROM gh_runs ORDER BY ts DESC LIMIT 1"));
      if (!(Date.now() - (last.ts || 0) < 60000)) await collectGithub(env);
      return new Response(null, { status: 303, headers: { Location: "/stats#github", "Set-Cookie": ME_COOKIE } });
    }
    const asked = url.searchParams.get("days");
    const days = asked && Object.hasOwn(RANGES, asked) ? Number(asked) : 30;
    const site = url.searchParams.get("site") === "demo" ? "demo" : "plexbie.com";
    const data = await load(env, days, site);
    if (url.pathname === "/stats/data.json") return Response.json(data, { headers: { "Cache-Control": "no-store", "Set-Cookie": ME_COOKIE } });
    return new Response(page(data, days), { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Set-Cookie": ME_COOKIE,
      "X-Robots-Tag": "noindex", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src https://plexbie.com; form-action 'self'; frame-ancestors 'none'" } });
  },
};
