import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { BlockedRow } from "../api/types";
import { clamp, deriveDesk, listFor, span, type Desk, type DeskData } from "./manageQueue";

/** The sample household's Manage page: the numbers the approved design shows for it. */

const NB = " ";
const SECTIONS = ["requests", "all", "tickets", "joins", "invites", "plexinvites", "people", "cleanup", "discord", "messages", "health"] as const;

let data: DeskData;
let now = 0;
let desk: Desk;

beforeAll(async () => {
  vi.useFakeTimers();
  vi.stubGlobal("location", { search: "?as=member", origin: "https://plexbie.example" });
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => undefined });
  vi.stubEnv("VITE_DEMO", "0");
  vi.resetModules();
  const s = await import("../api/sample");
  const asked = Promise.all([...SECTIONS.map((x) => s.admin(x)), s.adminBlocked()]);
  await vi.advanceTimersByTimeAsync(1000);
  const got = await asked;
  data = Object.fromEntries(SECTIONS.map((x, i) => [x, got[i]])) as DeskData;
  data.blocked = (got[SECTIONS.length] as { rows: BlockedRow[] }).rows;
  now = Date.now();
  desk = deriveDesk(data, now, { me: "Sam Rivera" });
});
afterAll(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const titles = (group: string) => desk.needs.filter((i) => i.group === group).map((i) => i.title);
const monitor = (area: string) => desk.monitors.find((m) => m.area === area)!;

describe("the Manage queue, from the sample household", () => {
  it("counts 17 things that need you, 3 of them urgent", () => {
    expect(desk.needs).toHaveLength(17);
    expect(desk.urgent).toBe(3);
    expect(titles("urgent")).toHaveLength(3);
    expect(titles("today")).toHaveLength(9);
    expect(titles("soon")).toHaveLength(5);
  });

  it("puts the blocked import, the member locked out and the failing check first", () => {
    expect(titles("urgent")).toEqual(["Radar Men from the Moon", "Jordan Lee can’t get in", "Discord Public Bot"]);
    const radar = desk.needs[0];
    expect(radar.eyebrow).toBe("Ticket · import blocked");
    expect(radar.sub).toBe("Season 1");
    expect(radar.meta).toBe("Won’t import · Jordan Lee’s request");
  });

  it("orders Today by the longest wait, counting how long a request has been stuck", () => {
    expect(titles("today")).toEqual([
      "Spring", "Cosmos Laundromat", "Undersea Kingdom", "Tears of Steel", "Jordan Lee",
      "The Daily Dweebs", "riley.plex", "Dracula", "Alex Kim",
    ]);
    const spring = desk.needs.find((i) => i.title === "Spring")!;
    // Searching since 48 h ago, though its ticket is 12 minutes old.
    expect(spring.eyebrow).toBe(`Ticket · request stuck 2${NB}days`);
    expect(spring.age).toBe(`12${NB}min`);
    expect(desk.needs.find((i) => i.title === "Undersea Kingdom")!.eyebrow).toBe(`Ticket · request stuck 9${NB}h`);
  });

  it("shows a stuck request with an open ticket as a hint on that ticket, never as a second row", () => {
    expect(desk.needs.filter((i) => i.kind === "stuck")).toEqual([]);
    expect(desk.needs.filter((i) => i.eyebrow.includes("request stuck"))).toHaveLength(2);
  });

  it("orders Coming up by the soonest deadline, the old Plex invite last", () => {
    expect(desk.needs.filter((i) => i.group === "soon").map((i) => [i.title, i.due ?? null])).toEqual([
      ["Marcus T.", 4], ["Elephants Dream", 5], ["Aunt Rosa’s invite link", 5], ["Radar Men from the Moon", 6], ["old-typo@exmaple.com", null],
    ]);
  });

  it("puts Marcus's unread message on his removal row, and leaves grandpa_j (marked done after the failure) out", () => {
    const marcus = desk.needs.find((i) => i.title === "Marcus T.")!;
    expect(marcus.meta).toBe("New message · not delivered");
    expect(desk.needs.filter((i) => i.kind === "chat").map((i) => i.title)).toEqual(["Alex Kim"]);
    expect(desk.needs.some((i) => i.title === "grandpa_j")).toBe(false);
  });

  it("gives each monitor the rows in its area, hot only with something urgent", () => {
    expect(desk.monitors.map((m) => [m.area, m.count, m.hot])).toEqual([
      ["requests", 3, false], ["help", 6, true], ["people", 5, false], ["library", 2, false], ["services", 1, true],
    ]);
    expect(monitor("requests").makeup).toBe(`3${NB}waiting`);
    expect(monitor("help").makeup).toBe(`5${NB}tickets · 1${NB}chat`);
    expect(monitor("people").makeup).toBe(`2${NB}joins · 1${NB}warned · 2${NB}invites`);
    expect(monitor("library").makeup).toBe(`2${NB}leaving in 7${NB}days`);
    expect(monitor("services").makeup).toBe(`1${NB}of 7${NB}down`);
    expect(monitor("requests").first?.title).toBe("Tears of Steel");
    expect(monitor("people").first?.top).toBe(`Wants to join · 5${NB}h`);
    // Every row is under exactly one monitor.
    expect(desk.monitors.reduce((n, m) => n + m.count, 0)).toBe(desk.needs.length);
  });

  it("has Pepper & Carrot waiting on its member, still showing its request is stuck", () => {
    expect(desk.waiting.map((i) => [i.title, i.eyebrow])).toEqual([["Pepper & Carrot", `Ticket · request stuck 90${NB}min`]]);
    expect(desk.waiting[0].age).toBe(`waiting 64${NB}min`);
  });

  it("lists everything done in the last two days, newest first, Spring's approval 48 h ago included", () => {
    expect(desk.done.map((i) => i.title)).toEqual(["Night of the Living Dead", "grandpa_j", "Elephants Dream", "Spring"]);
  });

  it("leaves hidden rows (a decision waiting out its Undo) out of every count", () => {
    const dracula = desk.needs.find((i) => i.title === "Dracula")!;
    const less = deriveDesk(data, now, { me: "Sam Rivera", hidden: new Set([dracula.key]) });
    expect(less.needs).toHaveLength(16);
    expect(less.monitors.find((m) => m.area === "requests")!.count).toBe(2);
    expect(less.monitors.find((m) => m.area === "requests")!.first?.title).toBe("Tears of Steel");
  });

  it("says the facts under the monitors", () => {
    expect(desk.facts).toEqual({ party: `Alex Kim · Tears of Steel · 3${NB}watching`, cleanup: "Cleanup live · 7-day warning" });
  });

  it("shows a dash rather than a false 0 until a monitor's sections have loaded", () => {
    const early = deriveDesk({ requests: data.requests }, now);
    expect(early.monitors.map((m) => m.known)).toEqual([false, false, false, false, false]);
    expect(deriveDesk({ requests: data.requests, all: data.all, tickets: data.tickets }, now).monitors[0]).toMatchObject({ known: true, count: 3 });
  });

  it("is loading (never all clear) until every feed has answered or failed", () => {
    expect(deriveDesk({}, now)).toMatchObject({ loading: true, missing: [], needs: [] });
    const { health: _health, ...rest } = data;
    expect(deriveDesk(rest, now).loading).toBe(true);
    expect(desk.loading).toBe(false);
    expect(desk.missing).toEqual([]);
  });

  it("says which feeds couldn't load, and marks their monitors failed, not empty", () => {
    const { health: _health, tickets: _tickets, ...rest } = data;
    const half = deriveDesk(rest, now, { failed: { health: true, tickets: true } });
    expect(half.loading).toBe(false);
    expect(half.missing).toEqual(["tickets", "health"]);
    expect(half.monitors.filter((m) => m.failed).map((m) => m.area)).toEqual(["requests", "help", "services"]);
    // A refresh that failed over data already here is old data, not missing.
    expect(deriveDesk(data, now, { failed: { health: true } }).missing).toEqual([]);
  });

  it("counts honestly when a feed is missing: Needs you at least, Waiting unknown without tickets", () => {
    expect(desk.counts).toEqual({ needs: "17", waiting: "1" });
    const { tickets: _tickets, ...rest } = data;
    const half = deriveDesk(rest, now, { failed: { tickets: true } });
    expect(half.counts).toEqual({ needs: `${half.needs.length}+`, waiting: "?" });
    const { health: _health, ...noHealth } = data;
    expect(deriveDesk(noHealth, now, { failed: { health: true } }).counts).toEqual({ needs: "16+", waiting: "1" });
  });

  it("narrows only Needs you by a monitor; Waiting and Done stay whole", () => {
    expect(listFor(desk, "needs", "requests")).toMatchObject({ filtered: true });
    expect(listFor(desk, "needs", "requests").list.map((i) => i.title)).toEqual(["Tears of Steel", "The Daily Dweebs", "Dracula"]);
    expect(listFor(desk, "needs", null)).toMatchObject({ filtered: false, list: desk.needs });
    expect(listFor(desk, "waiting", "requests")).toEqual({ filtered: false, list: desk.waiting });
    expect(listFor(desk, "done", "people")).toEqual({ filtered: false, list: desk.done });
  });

  it("finds a poster for a ticket or a decided request from what's loaded", () => {
    expect(desk.needs[0].art?.poster).toBeTruthy();
    expect(desk.done.find((i) => i.title === "Night of the Living Dead")?.art?.poster).toBeTruthy();
    // A row that brings none still finds its title's request in All requests.
    const rows = data.tickets!.rows.map((t) => ({ ...t, poster: null }));
    const bare = deriveDesk({ ...data, tickets: { ...data.tickets!, rows } }, now);
    expect(bare.needs.find((i) => i.title === "Spring")?.art?.poster).toBeTruthy();
    expect(bare.needs.find((i) => i.key === "ticket:h4")?.art?.poster).toBeNull();
  });

  it("puts a ticket first by its reason code, whatever its words", () => {
    const rows = data.tickets!.rows.map((t) => (t.id === "g2" ? { ...t, reason: "Some new wording" } : t));
    const worded = deriveDesk({ ...data, tickets: { ...data.tickets!, rows } }, now);
    expect(worded.needs.find((i) => i.key === "ticket:g2")!.group).toBe("urgent");
    const coded = data.tickets!.rows.map((t) => (t.id === "g2" ? { ...t, reasonCode: "other" } : t));
    const plain = deriveDesk({ ...data, tickets: { ...data.tickets!, rows: coded } }, now);
    expect(plain.needs.find((i) => i.key === "ticket:g2")!.group).toBe("today");
    expect(plain.urgent).toBe(2);
  });

  it("says the gist of a long health check on its row, and seasons in words", () => {
    expect(desk.needs.find((i) => i.kind === "down")!.meta).toBe("Anyone with its ID can add it");
    const alone = deriveDesk({ ...data, tickets: { ...data.tickets!, rows: data.tickets!.rows.filter((t) => t.id !== "h4") } }, now);
    expect(alone.needs.find((i) => i.kind === "import")!.sub).toBe("Season 1, 3 episodes");
  });

  it("brings a stuck request with no open ticket in as its own row", () => {
    const tickets = { ...data.tickets!, rows: data.tickets!.rows.filter((t) => t.requestKey !== "6001") };
    const alone = deriveDesk({ ...data, tickets }, now);
    const undersea = alone.needs.find((i) => i.kind === "stuck")!;
    expect(undersea.title).toBe("Undersea Kingdom");
    expect(undersea.area).toBe("requests");
    expect(alone.monitors[0].makeup).toBe(`3${NB}waiting · 1${NB}stuck`);
  });
});

describe("the queue's words", () => {
  it("says how long in minutes, then hours, then days", () => {
    expect(span(8 * 60_000)).toBe(`8${NB}min`);
    expect(span(90 * 60_000)).toBe(`90${NB}min`);
    expect(span(5 * 36e5)).toBe(`5${NB}h`);
    expect(span(48 * 36e5 + 5)).toBe(`2${NB}days`);
  });

  it("cuts a long quote at a word, with a tight ellipsis", () => {
    expect(clamp("Found it. Is it the 4K version you’re after, or is 1080p fine?", 30)).toBe("Found it. Is it the 4K…");
    expect(clamp("short")).toBe("short");
  });
});
