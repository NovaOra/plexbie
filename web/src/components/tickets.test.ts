import { describe, expect, it } from "vitest";
import { inLine, ticketHeading } from "./tickets";

describe("a ticket in words", () => {
  it("lower-cases a reason inside a sentence but keeps the names in it", () => {
    expect(inLine("Won’t play on Plex")).toBe("won’t play on Plex");
    expect(inLine("Plex says it's a different film")).toBe("Plex says it's a different film");
    expect(inLine("Can't get in, or nothing plays")).toBe("can't get in, or nothing plays");
  });

  it("heads a ticket with what it's about", () => {
    expect(ticketHeading("request", "Pepper & Carrot", 214)).toBe("Your ticket on No. 0214");
    expect(ticketHeading("title", "Sintel")).toBe("Your ticket on Sintel");
    expect(ticketHeading("general", null)).toBe("Your ticket");
  });
});
