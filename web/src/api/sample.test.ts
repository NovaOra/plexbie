import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import type { AdminAllRequests, AdminCleanup, AdminPerson, AdminRequests, AdminTickets, Community, HealthCheck, MediaRequest, MessagePerson, Title } from "./types";

/** The sample household's screens, read the way the website reads them, must tell one story:
 *  in the review build, and in the public demo, where the member's newest request plays out. */

/** The sample answers after a short pause: let it pass. */
async function done<T>(p: Promise<T>): Promise<T> {
  await vi.advanceTimersByTimeAsync(1000);
  return p;
}

type Row = { slot: number; title: Title; stage: string; requester: string; seasons?: unknown; progress?: { detail?: string } | null };

for (const demo of [false, true]) describe(demo ? "the demo's sample household" : "the sample household", () => {
  let s: typeof import("./sample");
  let started = 0;
  let me = "";
  let mine: MediaRequest[] = [];
  let all: Row[] = [];
  let requests: AdminRequests;
  let tickets: AdminTickets;
  let onPlex: Set<string>;
  let books: Set<string>;

  beforeAll(async () => {
    vi.useFakeTimers();
    vi.stubGlobal("location", { search: "?as=member", origin: "https://plexbie.example" });
    vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => undefined });
    vi.stubEnv("VITE_DEMO", demo ? "1" : "0");
    vi.resetModules();
    started = Date.now();
    s = await import("./sample");
    expect(s.DEMO).toBe(demo);
    me = (await done(s.session()))!.user.name;
    // Read at the same moment, so the demo's request is at the same step on both.
    const [theirs, everyone] = await done(Promise.all([s.myRequests(), s.adminAll("", true)]));
    mine = theirs;
    all = everyone.rows as Row[];
    requests = await done(s.admin("requests")) as AdminRequests;
    tickets = await done(s.admin("tickets")) as AdminTickets;
    const [films, shows, shelf] = await done(Promise.all([s.library("movie"), s.library("tv"), s.library("book")]));
    onPlex = new Set([...films, ...shows].map((t) => t.title));
    books = new Set(shelf.map((t) => t.title));
  });
  afterAll(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("signs in as the member persona", () => {
    expect(me).toBe("Sam Rivera");
  });

  it("names one title and one requester per request number on every screen", async () => {
    const seen = new Map<number, { title: string; who: string; where: string }>();
    const note = (slot: number, title: string, who: string, where: string) => {
      const before = seen.get(slot);
      if (before) expect({ slot, title, who }, `${where} vs ${before.where}`).toEqual({ slot, title: before.title, who: before.who });
      else seen.set(slot, { title, who, where });
    };
    for (const r of mine) note(r.slot, r.title.title, me, "My requests");
    for (const r of [...requests.pending, ...requests.recent]) note(r.slot, r.title, r.requester, "Manage requests");
    for (const r of all) note(r.slot, r.title.title, r.requester, "All requests");
    for (const t of tickets.rows) {
      note(t.slot, t.title, t.who, `ticket ${t.id}`);
      const open = await done(s.adminTicket(t.id));
      if (open.request) note(open.request.slot, open.request.title.title, open.request.requester, `ticket ${t.id}'s request`);
    }
    const people = await done(s.admin("messages")) as MessagePerson[];
    for (const p of people.filter((x) => x.ticket)) {
      const t = tickets.rows.find((x) => x.id === p.ticket!.id);
      expect(t, `${p.name}'s ticket link`).toBeDefined();
      expect(t!.who).toBe(p.name);
      note(p.ticket!.slot, p.ticket!.title, t!.who, `${p.name}'s messages`);
    }
  });

  it("files a message on the ticket its button names", async () => {
    // A ticket an admin opens in the same visit becomes the one the button names.
    await done(s.requestTicket("6002", "Checking the release names", false));
    const people = await done(s.admin("messages")) as MessagePerson[];
    const p = people.find((x) => x.name === "Alex Kim")!;
    const id = Object.entries({ d302: "Alex Kim", d301: "Marcus T.", pgrandpa_j: "grandpa_j" }).find(([, name]) => name === p.name)![0];
    const dm = (await done(s.conversation(id))).find((m) => m.direction === "in" && m.context === "Discord DM")!;
    expect((await done(s.messageToTicket(dm.id))).message).toBe(`Added to their ticket on ${p.ticket!.title}.`);
    expect((await done(s.conversation(id))).find((m) => m.id === dm.id)!.ticket).toBe(p.ticket!.id);
  });

  it("lists the member's own requests under their name in All requests", () => {
    for (const r of mine) {
      const row = all.find((x) => x.slot === r.slot);
      expect(row && { title: row.title.title, stage: row.stage, requester: row.requester }, `No. ${r.slot}`)
        .toEqual({ title: r.title.title, stage: r.stage, requester: me });
    }
    const theirs = new Set(mine.map((r) => r.title.title));
    const others = [...all.filter((r) => r.requester !== me).map((r) => r.title.title),
      ...[...requests.pending, ...requests.recent].filter((r) => r.requester !== me).map((r) => r.title)];
    expect(others.filter((t) => theirs.has(t))).toEqual([]);
  });

  if (demo) it("plays the member's request out the same on My requests and All requests", async () => {
    const stages: string[] = [];
    for (let second = 0; second <= 24; second++) {
      vi.setSystemTime(started + second * 1000);
      const [theirs, everyone] = await done(Promise.all([s.myRequests(), s.adminAll("")]));
      const r = theirs.find((x) => x.slot === 214)!;
      const row = everyone.rows.find((x) => x.slot === 214)!;
      expect({ stage: row.stage, progress: row.progress ?? null }, `${second} s in`).toEqual({ stage: r.stage, progress: r.progress ?? null });
      if (stages[stages.length - 1] !== r.stage) stages.push(r.stage);
    }
    expect(stages).toEqual(["requested", "approved", "downloading", "unpacking", "importing", "available"]);
  });

  it("waits on the same requests in Manage → Requests and All requests", async () => {
    const asked = (rows: { slot: number; title: string; requester: string }[]) => rows.map(({ slot, title, requester }) => ({ slot, title, requester })).sort((a, b) => a.slot - b.slot);
    // In the demo the member's newest request waits for its first seconds, then moves on.
    for (const second of [0, 2, 5, 30]) {
      vi.setSystemTime(started + second * 1000);
      const [now, everyone] = await done(Promise.all([s.admin("requests"), s.adminAll("")])) as [AdminRequests, AdminAllRequests];
      const waiting = everyone.rows.filter((r) => r.stage === "requested").map((r) => ({ ...r, title: r.title.title }));
      expect(asked(waiting), `${second} s in`).toEqual(asked(now.pending));
      expect(everyone.counts!.waiting, `${second} s in`).toBe(now.pending.length);
    }
  });

  it("files every message about a request in its requester's conversation", async () => {
    const [everyone, now] = await done(Promise.all([s.adminAll("", true), s.admin("requests")])) as [AdminAllRequests, AdminRequests];
    const askedBy = new Map<string, Set<string>>();
    const add = (title: string, who: string) => askedBy.set(title, (askedBy.get(title) ?? new Set()).add(who));
    for (const r of everyone.rows) add(r.title.title, r.requester);
    for (const r of [...now.pending, ...now.recent]) add(r.title, r.requester);
    for (const t of tickets.rows) add(t.title, t.who);
    const people = await done(s.admin("messages")) as MessagePerson[];
    expect(people.length).toBeGreaterThan(0);
    for (const p of people) {
      for (const m of await done(s.conversation(p.id))) {
        const words = `${m.title ?? ""} ${m.text}`;
        for (const [title, who] of askedBy) if (words.includes(title)) expect(who.has(p.name), `"${words.slice(0, 60)}" to ${p.name}`).toBe(true);
      }
    }
  });

  it("warns nobody who is watching or in the top three, and only members are watching", async () => {
    const board = await done(s.community()) as Community;
    const people = await done(s.admin("people")) as AdminPerson[];
    const top = new Set(board.leaderboard.slice(0, 3).map((x) => x.name));
    const watching = new Set(board.onAir.map((o) => o.member));
    for (const p of people) {
      expect(p.topThree, `${p.plexName} in the top three`).toBe(top.has(p.plexName));
      if (top.has(p.plexName) || watching.has(p.plexName)) expect({ who: p.plexName, warned: p.warned, daysIdle: p.daysIdle }).toEqual({ who: p.plexName, warned: false, daysIdle: 0 });
    }
    expect(people.some((p) => p.warned)).toBe(true);
    const joins = await done(s.admin("joins")) as { name: string; status: string }[];
    const invited = await done(s.admin("plexinvites")) as { who: string | null }[];
    const outside = new Set([...joins.filter((j) => j.status === "pending").map((j) => j.name), ...invited.map((i) => i.who)]);
    for (const who of watching) expect(outside.has(who), `${who} On Air`).toBe(false);
  });

  it("keeps forever exactly the films and shows that are on Plex", async () => {
    // Every pair of letters, so a kept title outside the catalog turns up too.
    const chars = "abcdefghijklmnopqrstuvwxyz0123456789 &'-:.,!";
    const probes = [...chars].flatMap((a) => [...chars].map((b) => a + b));
    const found = new Set((await done(Promise.all(probes.map((q) => s.cleanupSearch(q))))).flat().map((t) => t.title));
    expect([...found].sort()).toEqual([...onPlex].sort());
    const cleanup = await done(s.admin("cleanup")) as AdminCleanup;
    for (const t of [...cleanup.warning, ...cleanup.upcoming, ...cleanup.exempt]) expect(onPlex.has(t.title), t.title).toBe(true);
  });

  it("names no service that is down in a request's progress", async () => {
    const health = await done(s.admin("health")) as HealthCheck[];
    const down = health.filter((h) => !h.ok).map((h) => h.name);
    for (const r of [...mine, ...all]) for (const name of down) expect(r.progress?.detail ?? "").not.toContain(name);
    // The board and On Air come from Tautulli.
    expect(health.find((h) => h.name === "Tautulli")?.ok).toBe(true);
  });

  it("has nothing searching or upcoming on Plex or On Air", async () => {
    const board = await done(s.community()) as Community;
    const watched = new Set(board.onAir.map((o) => o.title));
    const waiting = [...mine, ...all].filter((r) => r.stage === "searching" || r.stage === "upcoming").map((r) => r.title.title);
    expect(waiting.length).toBeGreaterThan(0);
    for (const t of waiting) expect(onPlex.has(t) || watched.has(t), t).toBe(false);
    for (const t of watched) expect(onPlex.has(t) || books.has(t), `${t} On Air`).toBe(true);
  });

  it("waits on no decision for a title that is already on Plex", () => {
    // A new season of a show on Plex is a fair ask; a whole film or book already there is not.
    const asked = [...all.filter((r) => r.stage === "requested" && !r.seasons).map((r) => r.title.title),
      ...requests.pending.filter((r) => !r.seasons).map((r) => r.title)];
    expect(asked.length).toBeGreaterThan(0);
    for (const t of asked) expect(onPlex.has(t) || books.has(t), t).toBe(false);
  });

  it("moves a decision made in Manage → Requests to All requests and the member's own list", async () => {
    vi.setSystemTime(started + 30_000);
    const before = await done(s.admin("requests")) as AdminRequests;
    const own = before.pending.find((r) => r.requester === me)!;
    const [yes, no] = before.pending.filter((r) => r.requester !== me);
    expect(own).toBeDefined();
    await done(s.sampleDecide("requests", yes.id, true));
    await done(s.sampleDecide("requests", no.id, false));
    await done(s.sampleDecide("requests", own.id, false));
    const [now, everyone, theirs] = await done(Promise.all([s.admin("requests"), s.adminAll(""), s.myRequests()])) as [AdminRequests, AdminAllRequests, MediaRequest[]];
    expect(now.pending.map((r) => r.slot)).toEqual(before.pending.filter((r) => r !== yes && r !== no && r !== own).map((r) => r.slot));
    expect(everyone.counts!.waiting).toBe(now.pending.length);
    const row = (slot: number) => everyone.rows.find((r) => r.slot === slot)!;
    expect({ stage: row(yes.slot).stage, status: row(yes.slot).status, approvedBy: row(yes.slot).approvedBy }).toEqual({ stage: "approved", status: "approved", approvedBy: me });
    expect({ stage: row(no.slot).stage, status: row(no.slot).status }).toEqual({ stage: "declined", status: "declined" });
    expect(row(own.slot).stage).toBe("declined");
    expect(theirs.find((r) => r.slot === own.slot)?.stage).toBe("declined");
  });

  // Only the demo's newest request waits in its first seconds; an answer then must hold once the journey moves on.
  it.runIf(demo)("keeps an answer to the member's newest request given while it waited", async () => {
    for (const approve of [false, true]) {
      vi.setSystemTime(started);
      vi.resetModules();
      const fresh = await import("./sample");
      const at = Date.now();
      const waiting = (await done(fresh.admin("requests")) as AdminRequests).pending.find((r) => r.slot === 214)!;
      expect(waiting.requester).toBe(me);
      await done(fresh.sampleDecide("requests", waiting.id, approve));
      for (const second of [2, 5, 30]) {
        vi.setSystemTime(at + second * 1000);
        const [now, everyone, theirs] = await done(Promise.all([fresh.admin("requests"), fresh.adminAll(""), fresh.myRequests()])) as [AdminRequests, AdminAllRequests, MediaRequest[]];
        const row = everyone.rows.find((r) => r.slot === 214)!;
        const own = theirs.find((r) => r.slot === 214)!;
        const where = `${approve ? "approved" : "declined"}, ${second} s in`;
        expect(now.pending.some((r) => r.slot === 214), where).toBe(false);
        expect(everyone.counts!.waiting, where).toBe(now.pending.length);
        expect(row.stage, where).toBe(own.stage);
        if (approve) expect(row.status, where).toBe("approved");
        else expect({ stage: row.stage, status: row.status }, where).toEqual({ stage: "declined", status: "declined" });
      }
    }
  });
});
