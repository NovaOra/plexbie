import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { BlockedRow } from "../api/types";
import { deriveDesk, type DeskData } from "./manageQueue";
import { menuGroups, type MenuGroup } from "./manageMenu";

/** "Everything else": grouped like the monitors, with the same counts. */

const NB = " ";
const SECTIONS = ["requests", "all", "tickets", "joins", "invites", "plexinvites", "people", "cleanup", "discord", "messages", "health"] as const;
let groups: MenuGroup[];
let data: DeskData;

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
  groups = menuGroups(deriveDesk(data, Date.now(), { me: "Sam Rivera" }), data);
});
afterAll(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const row = (id: string) => groups.flatMap((g) => g.rows).find((r) => r.id === id)!;

describe("the Everything else menu", () => {
  it("groups the full views by job, every one once", () => {
    expect(groups.map((g) => [g.label, g.rows.map((r) => r.label)])).toEqual([
      ["Requests", ["All requests", "Older requests"]],
      ["Help", ["All tickets", "Blocked imports", "Messages"]],
      ["People", ["Join requests", "People", "Waiting on Plex", "Invite links", "Who brought whom"]],
      ["Library", ["Leaving soon", "Keep forever", "Cleanup settings"]],
      ["Services", ["Services", "Sign-ins"]],
      ["Discord", ["Watch party", "Say something as Plexbie", "DMs to Plexbie"]],
    ]);
  });

  it("counts each group as its monitor does, and each row as the queue rows it covers", () => {
    expect(groups.map((g) => g.count)).toEqual([3, 6, 5, 2, 1, undefined]);
    for (const g of groups) if (g.count !== undefined) expect(g.rows.reduce((n, r) => n + (r.count ?? 0), 0)).toBe(g.count);
    // What isn't counted says where it is, never a number that reads as part of the count.
    expect(row("all")).toMatchObject({ count: 3, desc: `3${NB}waiting · 3${NB}stuck under Help` });
    expect(row("tickets")).toMatchObject({ count: 5, urgent: 2, desc: `5${NB}need you · 2${NB}urgent · 1${NB}more waiting on a reply` });
    expect(row("messages")).toMatchObject({ count: 1, desc: "Alex Kim unread · Marcus T. under People" });
    expect(row("plex")).toMatchObject({ count: 1, desc: `1 from 40${NB}days ago · 2${NB}more not accepted` });
    expect(row("links")).toMatchObject({ count: 1, desc: `Aunt Rosa’s ends in 5${NB}days` });
    expect(row("leaving").count).toBe(2);
    expect(row("services")).toMatchObject({ count: 1, tally: "down", desc: `Discord Public Bot down · 7${NB}checks` });
  });

  it("marks blocked imports urgent and a live watch party live, without counting them twice", () => {
    expect(row("blocked")).toMatchObject({ hot: true, count: 0, desc: "Radar Men from the Moon under All tickets" });
    expect(row("party")).toMatchObject({ tally: "live", desc: `Alex Kim · Tears of Steel · 3${NB}watching` });
  });

  it("sends each row to its full view", () => {
    expect(groups.flatMap((g) => g.rows).map((r) => (r.part ? `${r.tab}#${r.part}` : r.tab))).toEqual([
      "all", "requests#older", "tickets", "health#blocked", "messages", "joins", "people", "invites#plex", "invites", "invites#who",
      "cleanup", "cleanup#keep", "cleanup#settings", "health", "security", "discord#party", "discord#say", "discord#dms",
    ]);
  });
});
