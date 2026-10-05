// Writes the two home-screen and notification images that aren't the logo itself:
//
//   node scripts/app-icons.mjs
//
// public/brand/plexbie-maskable-512.png: the logo for Android's home screen. The
// launcher cuts every icon to its own shape (a circle, a squircle), keeping only
// the middle 80%. So the logo is drawn a little smaller, inside that circle, and
// its own background is carried out to the edges: what gets cut is background,
// never a tile in a circle.
//
// public/brand/badge-96.png: the small icon in Android's status bar and on each
// alert. Android draws it from the outline only (any colour becomes white), so a
// full-colour square shows as a white box. This is Plexbie's TV as a silhouette.
import { readFileSync, writeFileSync } from "node:fs";
import { chromium } from "playwright-core";

const brand = new URL("../public/brand/", import.meta.url);

/** Plexbie's TV: two antennae, the set, a screen cut out of it, and its face. */
const BADGE = `<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96" viewBox="0 0 96 96">
  <g fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round">
    <path d="M38 27 C33 18 27 13 20 12" />
    <path d="M58 27 C63 18 69 13 76 12" />
  </g>
  <circle cx="18" cy="12" r="5" fill="#fff" />
  <circle cx="78" cy="12" r="5" fill="#fff" />
  <path fill="#fff" fill-rule="evenodd" d="
    M22 26 h52 a14 14 0 0 1 14 14 v32 a14 14 0 0 1 -14 14 h-52 a14 14 0 0 1 -14 -14 v-32 a14 14 0 0 1 14 -14 z
    M27 35 h42 a7 7 0 0 1 7 7 v28 a7 7 0 0 1 -7 7 h-42 a7 7 0 0 1 -7 -7 v-28 a7 7 0 0 1 7 -7 z" />
  <circle cx="38" cy="52" r="4" fill="#fff" />
  <circle cx="58" cy="52" r="4" fill="#fff" />
  <path d="M42 60 q3 4 6 0 q3 4 6 0" fill="none" stroke="#fff" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" />
</svg>`;

const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage();

// Badge: rendered on a transparent page, so only the white shape has any alpha.
await page.setViewportSize({ width: 96, height: 96 });
await page.setContent(`<style>html,body{margin:0;background:transparent}</style>${BADGE}`);
writeFileSync(new URL("badge-96.png", brand), await page.locator("svg").screenshot({ omitBackground: true }));

// Maskable: the logo at 84% in the middle; each edge row and column of it stretched
// outwards to the canvas edge (the logo's background is a soft gradient, so this
// carries it on without a seam). Fully opaque, as a launcher icon must be.
const logo = readFileSync(new URL("plexbie-512.png", brand)).toString("base64");
const png = await page.evaluate(async (src) => {
  const img = new Image();
  img.src = `data:image/png;base64,${src}`;
  await img.decode();
  const N = 512, S = Math.round(N * 0.84), o = (N - S) / 2, w = img.width;
  const c = document.createElement("canvas");
  c.width = c.height = N;
  const g = c.getContext("2d");
  g.imageSmoothingQuality = "high";
  const strip = (sx, sy, sw, sh, dx, dy, dw, dh) => g.drawImage(img, sx, sy, sw, sh, dx, dy, dw, dh);
  strip(0, 0, w, 1, o, 0, S, o);                 // top
  strip(0, w - 1, w, 1, o, o + S, S, o);         // bottom
  strip(0, 0, 1, w, 0, o, o, S);                 // left
  strip(w - 1, 0, 1, w, o + S, o, o, S);         // right
  strip(0, 0, 1, 1, 0, 0, o, o);                 // corners
  strip(w - 1, 0, 1, 1, o + S, 0, o, o);
  strip(0, w - 1, 1, 1, 0, o + S, o, o);
  strip(w - 1, w - 1, 1, 1, o + S, o + S, o, o);
  g.drawImage(img, o, o, S, S);
  // The logo's own edge pixels are slightly see-through; an icon can't be.
  const d = g.getImageData(0, 0, N, N);
  for (let i = 3; i < d.data.length; i += 4) d.data[i] = 255;
  g.putImageData(d, 0, 0);
  return c.toDataURL("image/png").split(",")[1];
}, logo);
writeFileSync(new URL("plexbie-maskable-512.png", brand), Buffer.from(png, "base64"));

await browser.close();
console.log("badge-96.png and plexbie-maskable-512.png written");
