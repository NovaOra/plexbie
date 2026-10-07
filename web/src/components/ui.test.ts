import { describe, expect, it } from "vitest";
import { shortDate, since } from "./ui";

describe("since", () => {
  it("says how long ago for recent times", () => {
    expect(since(new Date(Date.now() - 3 * 86_400_000).toISOString())).toBe("3 days ago");
  });

  it("falls back to the short date after two weeks", () => {
    const iso = new Date(Date.now() - 30 * 86_400_000).toISOString();
    expect(since(iso)).toBe(shortDate(iso));
  });
});
