// Records a short walkthrough so the motion can be reviewed, not just stills.
import { chromium } from "playwright-core";
import { mkdirSync, renameSync, readdirSync } from "node:fs";
const out = process.argv[2] ?? ".impeccable/review/reel";
const base = "http://localhost:5179";
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome" });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 760 }, recordVideo: { dir: out, size: { width: 1280, height: 760 } } });
const page = await ctx.newPage();
const hide = () => page.addStyleTag({ content: ".sample-switch{display:none!important}" });
await page.goto(base + "/app?as=guest", { waitUntil: "networkidle" }); await hide();
await page.waitForTimeout(8500);                      // kinetic headline + one stage change
await page.mouse.wheel(0, 700); await page.waitForTimeout(2200);  // access journey fills
await page.goto(base + "/app?as=member", { waitUntil: "networkidle" }); await hide();
await page.waitForTimeout(3500);
await page.mouse.wheel(0, 900); await page.waitForTimeout(2500);  // request journeys
await page.getByRole("link", { name: "Library", exact: true }).first().click();
await page.waitForTimeout(1800);
const card = page.locator(".wall .poster").nth(2);
await card.hover({ position: { x: 40, y: 60 } }); await page.waitForTimeout(400);
await card.hover({ position: { x: 150, y: 200 } }); await page.waitForTimeout(500);
await page.getByRole("button", { name: "Animation" }).click(); await page.waitForTimeout(1200);
await page.getByRole("button", { name: "All" }).click(); await page.waitForTimeout(1000);
await page.getByRole("tab", { name: "TV" }).click(); await page.waitForTimeout(1500);
await page.locator(".wall .poster").first().click(); await page.waitForTimeout(2200);   // poster morphs into the title page
await page.goto(base + "/app/title/tv/100088", { waitUntil: "networkidle" }); await hide(); await page.waitForTimeout(800);
await page.getByRole("button", { name: /^Request / }).click(); await page.waitForTimeout(3000);  // request journey
await ctx.close(); await browser.close();
const f = readdirSync(out).find((n) => n.endsWith(".webm"));
renameSync(`${out}/${f}`, `${out}/plexbie-walkthrough.webm`);
console.log(`${out}/plexbie-walkthrough.webm`);
