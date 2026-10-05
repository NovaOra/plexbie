// The README's four phone screenshots (docs/screens), from the dev server's demo
// mode: invented titles and code-drawn covers only.
//
//   npm run dev                                  (in another terminal)
//   node scripts/readme-shots.mjs [outDir] [baseUrl]
import { mkdirSync } from "node:fs";
import { chromium } from "playwright-core";

const out = process.argv[2] ?? "../docs/screens";
const base = process.argv[3] ?? "http://localhost:5179";
const shots = [
  ["manage", "/app/manage?as=member&demo&tab=requests"],
  ["messages", "/app/manage?as=member&demo&tab=messages"],
  ["invite", "/invite?as=guest&demo"],
  ["cleanup", "/app/manage?as=member&demo&tab=cleanup"],
];

mkdirSync(out, { recursive: true });
const browser = await chromium.launch({ channel: "chrome" });
const ctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, reducedMotion: "reduce" });
const page = await ctx.newPage();
const problems = [];
page.on("pageerror", (e) => problems.push(e.message));
for (const [name, path] of shots) {
  await page.goto(base + path, { waitUntil: "networkidle" });
  await page.evaluate(() => document.fonts.ready);
  await page.addStyleTag({ content: ".sample-switch{display:none!important}" });
  await page.waitForTimeout(900);
  // Tabs further along the strip: show the chosen tab's panel, not the overview cards.
  if (!path.includes("tab=requests") && path.includes("tab=")) {
    await page.locator("[role=tablist]").first().evaluate((el) => {
      const header = document.querySelector("header")?.getBoundingClientRect().height ?? 64;
      window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - header - 12, behavior: "instant" });
    }).catch(() => {});
    await page.waitForTimeout(400);
  }
  await page.screenshot({ path: `${out}/${name}.png` });
  console.log(`${name}.png`);
}
await browser.close();
console.log(problems.length ? problems.join("\n") : "no page errors");
