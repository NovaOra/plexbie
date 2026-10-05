// Runs axe-core (WCAG 2.2 A/AA rules) on every route, for every persona, at phone and desktop widths.
import { chromium } from "playwright-core";
import { readFileSync } from "node:fs";
const base = process.argv[2] ?? "http://localhost:5179";
const axe = readFileSync(new URL("../node_modules/axe-core/axe.min.js", import.meta.url), "utf8");
const runs = [
  ["guest", "/"], ["guest", "/app?as=guest"], ["guest", "/privacy"], ["guest", "/terms"], ["visitor", "/app?as=visitor"],
  ["member", "/app?as=member"], ["member", "/app/library"], ["member", "/app/search?q=s&kind=tv"],
  ["member", "/app/title/tv/95396"], ["member", "/app/title/tv/100088"], ["member", "/app/schedule"], ["member", "/app/channel"],
  ["guest", "/invite"], ["member", "/app/alerts"],
  ...["requests", "joins", "invites", "people", "cleanup", "discord", "messages", "health"].map((t) => ["member", `/app/manage?tab=${t}`]),
];
const browser = await chromium.launch({ channel: "chrome" });
const found = new Map();
for (const width of [1440, 390]) {
  // One browser per persona, with the persona set before the first page loads: a
  // "?as=" switch only sticks once the page has saved it, so checking straight
  // after the first load could test the previous persona's pages.
  const pages = {};
  for (const persona of new Set(runs.map(([p]) => p))) {
    const ctx = await browser.newContext({ viewport: { width, height: 900 }, reducedMotion: "reduce" });
    await ctx.addInitScript((who) => { try { localStorage.setItem("plexbie.sample.persona", who); } catch { /* ignore */ } }, persona);
    pages[persona] = await ctx.newPage();
  }
  for (const [persona, path] of runs) {
    const page = pages[persona];
    await page.goto(base + path, { waitUntil: "networkidle" });
    await page.waitForTimeout(600);
    await page.addScriptTag({ content: axe });
    const res = await page.evaluate(async () => (await window.axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"] },
      rules: { region: { enabled: true } },
    })).violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 3).map((n) => n.target.join(" ")) })));
    for (const v of res) {
      const key = v.id;
      const e = found.get(key) ?? { ...v, where: new Set() };
      e.where.add(`${persona} ${path} @${width}`);
      found.set(key, e);
    }
  }
  for (const page of Object.values(pages)) await page.context().close();
}
await browser.close();
if (!found.size) console.log("axe: no violations");
for (const v of [...found.values()].sort((a, b) => ["critical", "serious", "moderate", "minor"].indexOf(a.impact) - ["critical", "serious", "moderate", "minor"].indexOf(b.impact))) {
  console.log(`[${v.impact}] ${v.id}: ${v.help}\n   e.g. ${v.nodes.join(" | ")}\n   on ${[...v.where].slice(0, 4).join("; ")}${v.where.size > 4 ? ` (+${v.where.size - 4} more)` : ""}`);
}
