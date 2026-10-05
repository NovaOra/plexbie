// Record the README's demo animations from the dev server's demo mode.
//
//   npm run dev                                  (in another terminal)
//   node scripts/demo-gifs.mjs [outDir] [baseUrl]
//
// Each scene is clicked through in Chrome at phone size while the page is
// streamed frame by frame (Chrome's screencast), so timing is real. Frames land
// in <outDir>/<scene>/ with a frames.txt for ffmpeg's concat demuxer; turn them
// into GIFs with the commands printed at the end. Demo mode (?demo) shows only
// invented titles and code-drawn covers, never real artwork.
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { chromium } from "playwright-core";

const out = process.argv[2] ?? "demo-frames";
const base = process.argv[3] ?? "http://localhost:5179";

// A soft pink dot wherever the "finger" goes down, so viewers can follow along.
const TOUCH_UI = `
  .sample-switch { display: none !important; }
  .demo-tap { position: fixed; z-index: 9999; width: 34px; height: 34px; margin: -17px 0 0 -17px; border-radius: 50%;
    background: rgb(255 92 147 / 0.35); border: 2px solid rgb(255 209 228 / 0.9); pointer-events: none;
    transition: transform 380ms cubic-bezier(.23,1,.32,1), opacity 380ms ease; }
  .demo-tap.is-up { transform: scale(1.7); opacity: 0; }`;
const TOUCH_JS = () => {
  let dot = null;
  addEventListener("pointerdown", (e) => {
    dot = document.createElement("div"); dot.className = "demo-tap";
    dot.style.left = `${e.clientX}px`; dot.style.top = `${e.clientY}px`; document.body.append(dot);
  }, true);
  addEventListener("pointermove", (e) => { if (dot) { dot.style.left = `${e.clientX}px`; dot.style.top = `${e.clientY}px`; } }, true);
  addEventListener("pointerup", () => { const d = dot; dot = null; if (d) { d.classList.add("is-up"); setTimeout(() => d.remove(), 400); } }, true);
};

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function record(browser, name, run) {
  const dir = join(out, name);
  mkdirSync(dir, { recursive: true });
  const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: false });
  await page.addInitScript(TOUCH_JS);
  const cdp = await page.context().newCDPSession(page);
  const frames = [];
  cdp.on("Page.screencastFrame", async ({ data, metadata, sessionId }) => {
    const file = `f${String(frames.length).padStart(5, "0")}.jpg`;
    writeFileSync(join(dir, file), Buffer.from(data, "base64"));
    frames.push({ file, t: metadata.timestamp });
    await cdp.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
  });
  const go = async (path) => {
    await page.goto(`${base}${path}`, { waitUntil: "networkidle" });
    await page.addStyleTag({ content: TOUCH_UI });
  };
  // Mouse moves glide, so drags and holds look like a finger, not a teleport.
  const tap = async (locator, holdMs = 90) => {
    // Bring it on screen first, as a thumb would, so the tap lands where it should.
    await locator.evaluate((el) => {
      const r = el.getBoundingClientRect();
      if (r.top < 80 || r.bottom > innerHeight - 80) el.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    await wait(450);
    const box = await locator.boundingBox();
    const x = box.x + box.width / 2, y = box.y + box.height / 2;
    await page.mouse.move(x, y, { steps: 6 });
    await page.mouse.down(); await wait(holdMs); await page.mouse.up();
  };
  const ctx = { page, go, tap, wait };
  await go(run.start);
  await cdp.send("Page.startScreencast", { format: "jpeg", quality: 88, everyNthFrame: 1 });
  await wait(700);
  await run.steps(ctx);
  await wait(900);
  await cdp.send("Page.stopScreencast");
  await page.close();
  // ffmpeg concat list: each frame shown until the next one arrived.
  const lines = [];
  frames.forEach((f, i) => {
    const next = frames[i + 1]?.t ?? f.t + 1.2;
    lines.push(`file '${f.file}'`, `duration ${Math.max(0.02, next - f.t).toFixed(3)}`);
  });
  lines.push(`file '${frames.at(-1).file}'`);
  writeFileSync(join(dir, "frames.txt"), lines.join("\n") + "\n");
  console.log(`${name}: ${frames.length} frames, ${(frames.at(-1).t - frames[0].t).toFixed(1)} s`);
}

const SCENES = {
  request: {
    start: "/search?as=member&demo&kind=tv",
    async steps({ page, tap, wait }) {
      await wait(1200);
      const input = page.locator(".finder input");
      await tap(input);
      await input.pressSequentially("night", { delay: 160 });
      await page.waitForSelector("a.poster .poster__art");
      await wait(1300);
      await tap(page.locator("a.poster").first());
      await page.waitForSelector(".season");
      await wait(900);
      await page.locator(".chooser").evaluate((el) => el.scrollIntoView({ behavior: "smooth", block: "start" }));
      await wait(1100);
      await tap(page.locator(".season:not([disabled])").nth(1));
      await wait(500);
      await tap(page.locator("button.choice", { hasText: "All the missing ones" }));
      await wait(900);
      await page.locator(".confirm .btn--primary").evaluate((el) => el.scrollIntoView({ behavior: "smooth", block: "center" }));
      await wait(700);
      await tap(page.locator(".confirm .btn--primary"));
      await page.waitForSelector("text=is in");
      await wait(2200);
    },
  },
  journey: {
    start: "/schedule?as=member&demo",
    async steps({ wait }) { await wait(26500); },
  },
  manage: {
    start: "/manage?as=member&demo&tab=requests",
    async steps({ page, tap, wait }) {
      await wait(900);
      // centred, so the sticky section bar never covers the card being swiped
      const card = page.locator(".m-swipe__card").first();
      await card.evaluate((el) => el.scrollIntoView({ behavior: "smooth", block: "center" }));
      await wait(900);
      // swipe the first card right: approve
      const box = await card.boundingBox();
      const y = box.y + 50;
      await page.mouse.move(box.x + 60, y, { steps: 4 });
      await page.mouse.down();
      await page.mouse.move(box.x + 330, y, { steps: 22 });
      await wait(250);
      await page.mouse.up();
      await wait(2200);
      // hold to decline the next one
      const hold = page.locator(".m-hold").first();
      await hold.evaluate((el) => el.scrollIntoView({ behavior: "smooth", block: "center" }));
      await wait(700);
      await tap(hold, 1150);
      await wait(2400);
    },
  },
};

const only = process.argv[4]?.split(",");
const browser = await chromium.launch({ channel: "chrome" });
for (const [name, scene] of Object.entries(SCENES)) {
  if (only && !only.includes(name)) continue;
  await record(browser, name, scene);
}
await browser.close();
console.log(`
To make the GIFs (ffmpeg), for each scene:
  ffmpeg -f concat -safe 0 -i <scene>/frames.txt -vf "fps=15,scale=360:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a" <scene>.gif`);
