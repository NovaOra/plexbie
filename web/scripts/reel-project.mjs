// Records the public project page so its motion can be reviewed.
import { chromium } from "playwright-core";
import { mkdirSync, readdirSync, renameSync } from "node:fs";
const out = process.argv[2] ?? ".impeccable/review/reel-project";
mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome" });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 760 }, recordVideo: { dir: out, size: { width: 1280, height: 760 } } });
const page = await ctx.newPage();
await page.goto("http://localhost:5179/?as=guest", { waitUntil: "networkidle" });
await page.addStyleTag({ content: ".sample-switch{display:none!important}" });
await page.waitForTimeout(5200);                                   // power-on, headline, two channel flips
await page.locator(".tv__button").hover({ position: { x: 60, y: 80 }, force: true }); await page.waitForTimeout(500);
await page.locator(".tv__button").hover({ position: { x: 330, y: 300 }, force: true }); await page.waitForTimeout(500);
await page.locator(".tv__button").click({ force: true }); await page.waitForTimeout(900);
await page.locator("#channels").scrollIntoViewIfNeeded(); await page.waitForTimeout(3000);   // Requests channel plays
await page.getByRole("button", { name: "Channel up" }).click(); await page.waitForTimeout(1800);   // static, then Plex access
await page.getByRole("button", { name: /Channel dial/ }).click(); await page.waitForTimeout(2200); // dial turns to Watching
await page.getByRole("button", { name: /Test card/ }).click(); await page.waitForTimeout(1800);
await page.getByRole("button", { name: /Turn the television off/ }).click(); await page.waitForTimeout(1200);
await page.getByRole("button", { name: /Turn the television on/ }).click(); await page.waitForTimeout(1200);
await page.locator(".plugs").scrollIntoViewIfNeeded(); await page.waitForTimeout(1200);
{ const b = await page.locator(".dcrawl").first().boundingBox(); const y = b.y + b.height / 2;
  await page.mouse.move(b.x + 900, y); await page.mouse.down(); await page.mouse.move(b.x + 350, y, { steps: 14 }); await page.mouse.up();
  await page.waitForTimeout(1400); await page.mouse.click(b.x + 500, y); await page.waitForTimeout(1500); }
await page.locator("#self-host").scrollIntoViewIfNeeded(); await page.waitForTimeout(3200); // terminal types
await page.locator(".tunein").scrollIntoViewIfNeeded(); await page.waitForTimeout(2200);
await ctx.close(); await browser.close();
const f = readdirSync(out).find((n) => n.endsWith(".webm"));
renameSync(`${out}/${f}`, `${out}/plexbie-project.webm`);
console.log(`${out}/plexbie-project.webm`);
