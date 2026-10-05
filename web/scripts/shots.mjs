// Batched review captures: every page at desktop and phone width.
// Usage: node scripts/shots.mjs <outDir> [baseUrl]
import { chromium } from "playwright-core";
import { mkdirSync } from "node:fs";

const out = process.argv[2] ?? ".impeccable/review";
const base = process.argv[3] ?? "http://localhost:5179";
mkdirSync(out, { recursive: true });

const pages = [
  ["project", "/"],
  ["guest", "/app?as=guest"],
  ["privacy", "/privacy"],
  ["visitor", "/app?as=visitor"],
  ["home", "/app?as=member"],
  ["library", "/app/library"],
  ["search", "/app/search?q=s&kind=tv"],
  ["title-tv", "/app/title/tv/95396"],
  ["schedule", "/app/schedule"],
  ["channel", "/app/channel"],
];

const browser = await chromium.launch({ channel: "chrome" });
const problems = [];
for (const [w, h, tag] of [[1440, 900, "d"], [390, 844, "m"]]) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: tag === "m" ? 2 : 1, reducedMotion: "reduce" });
  const page = await ctx.newPage();
  page.on("console", (m) => m.type() === "error" && problems.push(`${tag} console: ${m.text()}`));
  page.on("pageerror", (e) => problems.push(`${tag} pageerror: ${e.message}`));
  for (const [name, url] of pages) {
    await page.goto(base + url, { waitUntil: "networkidle" });
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(700);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (overflow > 0) problems.push(`${tag} ${name}: horizontal overflow ${overflow}px`);
    await page.addStyleTag({ content: ".sample-switch{display:none!important}" });
    await page.screenshot({ path: `${out}/${name}-${tag}.png`, fullPage: true });
  }
  // The request flow end to end: pick a title nobody has asked for, request it.
  await page.goto(base + "/app/title/tv/100088", { waitUntil: "networkidle" });
  await page.addStyleTag({ content: ".sample-switch{display:none!important}" });
  await page.getByRole("button", { name: /^Request / }).click();
  await page.getByText(/is in$/).waitFor();
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${out}/requested-${tag}.png`, fullPage: true });
  await ctx.close();
}
await browser.close();
console.log(problems.length ? problems.join("\n") : "no console errors, no horizontal overflow");
