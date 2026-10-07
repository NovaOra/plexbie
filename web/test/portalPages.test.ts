// The pages the bot serves itself (portal/*.html, outside this website): every inline
// script must at least parse, or the browser runs none of it. The first-time setup
// page once declared the same name twice and showed only its headings.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Script } from "node:vm";
import { describe, expect, it } from "vitest";

const PORTAL = fileURLToPath(new URL("../../portal/", import.meta.url));
const pages = readdirSync(PORTAL).filter((f) => f.endsWith(".html"));

function scripts(html: string): { code: string; line: number }[] {
  const out: { code: string; line: number }[] = [];
  for (const m of html.matchAll(/<script(\s[^>]*)?>([\s\S]*?)<\/script>/gi)) {
    const attrs = m[1] ?? "";
    if (/\ssrc=|type=["']?(module|application\/json|application\/ld\+json|importmap)/i.test(attrs)) continue;
    if (!m[2].trim()) continue;
    out.push({ code: m[2], line: html.slice(0, m.index).split("\n").length });
  }
  return out;
}

describe("the bot's own pages", () => {
  it("include the first-time setup page", () => {
    expect(pages).toContain("setup.html");
  });

  for (const page of pages) {
    it(`${page}: every inline script parses`, () => {
      for (const { code, line } of scripts(readFileSync(join(PORTAL, page), "utf8"))) {
        expect(() => new Script(code, { filename: `portal/${page}:${line}` }), `portal/${page}, script at line ${line}`).not.toThrow();
      }
    });
  }
});
