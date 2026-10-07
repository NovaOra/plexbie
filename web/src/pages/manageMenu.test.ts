import { describe, expect, it } from "vitest";
import { menuItems, menuLabel, waitingElsewhere } from "./manageMenu";

const TABS = [
  { id: "requests", label: "Requests" },
  { id: "all", label: "All requests" },
  { id: "tickets", label: "Tickets" },
  { id: "invites", label: "Invites" },
  { id: "people", label: "People" },
  { id: "cleanup", label: "Cleanup" },
  { id: "messages", label: "Messages" },
  { id: "health", label: "Health" },
] as const;

describe("the Manage sections menu", () => {
  it("lists every section in order, with what each one counts", () => {
    const items = menuItems(TABS, "requests", { requests: 3, all: 2, tickets: 2, invites: 1, cleanup: 4, messages: 1, health: 1 }, {});
    expect(items.map((i) => i.id)).toEqual(TABS.map((t) => t.id));
    expect(Object.fromEntries(items.map((i) => [i.id, i.note]))).toEqual({
      requests: "3 waiting", all: "2 stuck", tickets: "2 open", invites: "1 ready", people: "",
      cleanup: "4 waiting", messages: "1 new", health: "1 down",
    });
  });

  it("marks the section that's open", () => {
    const items = menuItems(TABS, "tickets", {}, {});
    expect(items.filter((i) => i.current).map((i) => i.id)).toEqual(["tickets"]);
  });

  it("shows pink only for Health and Cleanup, and only when they have something", () => {
    const hot = (counts: Record<string, number>) => menuItems(TABS, "requests", counts, {}).filter((i) => i.hot).map((i) => i.id);
    expect(hot({ requests: 3, tickets: 2, health: 1, cleanup: 2 })).toEqual(["cleanup", "health"]);
    expect(hot({ requests: 3 })).toEqual([]);
  });

  it("says a count is in doubt when its refresh failed, except a ready invite link", () => {
    const items = menuItems(TABS, "requests", { requests: 3, invites: 1 }, { requests: true, invites: true });
    expect(items.find((i) => i.id === "requests")?.note).toBe("couldn’t refresh");
    expect(items.find((i) => i.id === "invites")?.note).toBe("1 ready");
  });

  it("names the button by what's waiting in the other sections", () => {
    expect(menuLabel(menuItems(TABS, "requests", {}, {}))).toBe("Sections");
    // The open section's count is in its heading already.
    expect(menuLabel(menuItems(TABS, "requests", { requests: 3 }, {}))).toBe("Sections");
    const items = menuItems(TABS, "requests", { requests: 3, tickets: 2, health: 1 }, {});
    expect(waitingElsewhere(items).map((i) => i.id)).toEqual(["tickets", "health"]);
    expect(menuLabel(items)).toBe("Sections, 2 need you");
    expect(menuLabel(menuItems(TABS, "requests", { health: 1 }, {}))).toBe("Sections, 1 needs you");
  });
});
