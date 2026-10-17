import { describe, expect, it } from "vitest";
import type { TicketEntry } from "../api/types";
import { entryBy, inLine, ticketFrom, ticketHeading } from "./tickets";

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

describe("a ticket Plexbie opened itself", () => {
  const plexbies: TicketEntry = { id: "a", at: "2026-10-17T00:00:00Z", by: "Plexbie", kind: "member", plexbie: true,
    text: "Sintel finished downloading, but Radarr won’t import it by itself. An admin will check it." };
  const theirs: TicketEntry = { id: "b", at: "2026-10-17T00:05:00Z", by: "Jordan", kind: "member", text: "It's the extended cut." };

  it("puts Plexbie's first message under Plexbie, for the member, on Manage", () => {
    expect(entryBy(plexbies, "Jordan")).toBe("Plexbie, for Jordan");
    expect(entryBy(theirs, "Jordan")).toBe("Jordan");
  });

  it("puts it under Plexbie on the member's own ticket, and theirs under You", () => {
    expect(entryBy(plexbies)).toBe("Plexbie");
    expect(entryBy({ ...theirs, by: "You" })).toBe("You");
  });

  it("says who it came from on Manage", () => {
    expect(ticketFrom({ who: "Jordan", byPlexbie: true, openedBy: null })).toBe("opened by Plexbie for Jordan");
    expect(ticketFrom({ who: "Jordan", openedBy: "Sam" })).toBe("opened by Sam");
    expect(ticketFrom({ who: "Jordan" })).toBe("asked by Jordan");
  });
});
