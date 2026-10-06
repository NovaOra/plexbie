// Sample data for design review. The people are invented; the titles and
// artwork are real public TMDB / Open Library records. Never in the real site:
// client.ts only reaches this module in a dev build, the private review build,
// and the public demo (build:demo), which always runs in demo mode below.
import type {
  Arrival, BookFormat, Community, LibraryItem, LibraryKind, MediaKind, MediaRequest, ServerStatus, Session, Title,
} from "./types";
import raw from "./sampleTitles.json";

type Persona = "guest" | "visitor" | "member";
const KEY = "plexbie.sample.persona";

function persona(): Persona {
  const asked = new URLSearchParams(location.search).get("as") as Persona | null;
  if (asked && ["guest", "visitor", "member"].includes(asked)) {
    try { localStorage.setItem(KEY, asked); } catch { /* private mode */ }
    return asked;
  }
  try { return (localStorage.getItem(KEY) as Persona) || "member"; } catch { return "member"; }
}

/**
 * Demo mode (?demo, and the demo.plexbie.com build): the request journey plays out and visits
 * are counted. The sample catalog itself is copyright-free in every mode: Blender Foundation
 * open movies (CC BY) and public-domain films with their real posters, and invented shows and
 * books with drawn covers (sampleTitles.json). Remembered for the tab's session.
 */
export const DEMO = import.meta.env.VITE_DEMO === "1" || (import.meta.env.DEV && (() => {
  try {
    if (new URLSearchParams(location.search).has("demo")) sessionStorage.setItem("plexbie.demo", "1");
    return sessionStorage.getItem("plexbie.demo") === "1";
  } catch { return false; }
})());
export const wait = <T,>(value: T, ms = 260) => new Promise<T>((r) => setTimeout(() => r(value), ms));
const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();

const titles: Title[] = (raw as Omit<Title, "availability">[]).map((t) => ({ ...t, availability: "none" }));
const byTitle = (name: string) => titles.find((t) => t.title === name)!;

// Who already has what, so search results show real-looking availability.
const onPlex = new Set(["Elephants Dream", "Big Buck Bunny", "King of the Rocket Men", "Sintel", "Radar Men from the Moon", "Charge", "The Wonderful Wizard of Oz", "Coffee Run", "The Daily Dweebs", "Cosmos Laundromat", "Sprite Fright", "Dracula", "The Great Gatsby", "Plan 9 from Outer Space", "House on Haunted Hill", "Night of the Living Dead", "Carnival of Souls"]);
const requestedByOthers = new Set(["Undersea Kingdom", "Spring"]);
for (const t of titles) {
  const name = t.title;
  t.availability = onPlex.has(name) ? "available" : requestedByOthers.has(name) ? "requested" : "none";
}

let mine: MediaRequest[] = [
  { id: "5001", slot: 214, title: byTitle("Pepper & Carrot"), stage: "downloading", seasons: [2], requestedAt: ago(60 * 26), updatedAt: ago(14) },
  { id: "5002", slot: 211, title: byTitle("The War of the Worlds"), stage: "unpacking", progress: { percent: null, detail: "Unpacking in SABnzbd" }, format: "audiobook", requestedAt: ago(60 * 49), updatedAt: ago(60 * 3) },
  { slot: 209, title: byTitle("Tears of Steel"), stage: "requested", requestedAt: ago(60 * 5), updatedAt: ago(60 * 5) },
  { slot: 205, title: byTitle("Big Buck Bunny"), stage: "upcoming", requestedAt: ago(60 * 24 * 3), updatedAt: ago(60 * 24 * 3),
    progress: { releaseDate: new Date(Date.now() + 23 * 864e5).toISOString().slice(0, 10), releaseKind: "digital",
      detail: `Out to stream ${new Date(Date.now() + 23 * 864e5).toLocaleDateString("en-US", { month: "short", day: "numeric" })}. Plexbie gets it then. In cinemas since Sep 30.` } },
  { slot: 197, title: byTitle("Sintel"), stage: "available", requestedAt: ago(60 * 24 * 6), updatedAt: ago(60 * 24 * 4) },
  { slot: 188, title: byTitle("Zombies of the Stratosphere"), stage: "declined", requestedAt: ago(60 * 24 * 12), updatedAt: ago(60 * 24 * 11), note: "Someone in the household already asked for this one." },
];
let nextSlot = 215;

export const session = async (): Promise<Session | null> => {
  const p = persona();
  if (p === "guest") return wait(null, 120);
  return wait({
    user: { id: "100000000000000001", name: p === "member" ? "Sam Rivera" : "Jordan Lee", avatar: null },
    member: p === "member",
    admin: p === "member",
    joinPending: false,
  }, 120);
};

export const status = (): Promise<ServerStatus> => wait({
  online: true,
  streams: 3,
  checkedAt: ago(1),
  libraries: [
    { title: "Films", kind: "movie", count: 1284 },
    { title: "TV", kind: "show", count: 312 },
    { title: "Audiobooks", kind: "book", count: 96 },
    { title: "Ebooks", kind: "book", count: 241 },
  ],
});

export const arrivals = (): Promise<Arrival[]> => wait([
  { title: { ...byTitle("Sintel"), availability: "available" }, addedAt: ago(40) },
  { title: { ...byTitle("King of the Rocket Men"), availability: "available" }, addedAt: ago(60 * 3), detail: "Chapters 1–6" },
  { title: { ...byTitle("Coffee Run"), availability: "available" }, addedAt: ago(60 * 9) },
  { title: { ...byTitle("Charge"), availability: "available" }, addedAt: ago(60 * 20) },
  { title: { ...byTitle("The Wonderful Wizard of Oz"), availability: "available" }, addedAt: ago(60 * 30), detail: "Audiobook" },
  { title: { ...byTitle("Radar Men from the Moon"), availability: "available" }, addedAt: ago(60 * 44), detail: "Chapters 7–12" },
  { title: { ...byTitle("Elephants Dream"), availability: "available" }, addedAt: ago(60 * 70) },
]);

export const community = (): Promise<Community> => wait({
  onAir: [
    { member: "Alex Kim", title: "King of the Rocket Men", subtitle: "Chapter 3, “The Deadly Fog”", poster: byTitle("King of the Rocket Men").poster, progress: 0.62, device: "Living room TV" },
    { member: "Priya N.", title: "Big Buck Bunny", poster: byTitle("Big Buck Bunny").poster, progress: 0.18, device: "iPad" },
    { member: "Marcus T.", title: "The Wonderful Wizard of Oz", subtitle: "Chapter 7", poster: byTitle("The Wonderful Wizard of Oz").poster, progress: 0.41, device: "Phone" },
  ],
  leaderboard: [
    { name: "Alex Kim", hours: 1412, streak: 12 },
    { name: "Sam Rivera", hours: 1088, streak: 5 },
    { name: "Priya N.", hours: 964, streak: 9 },
    { name: "Marcus T.", hours: 391, streak: 2 },
    { name: "Jordan Lee", hours: 58, streak: 0 },
  ],
  you: {
    rank: 2, hours: 1088, streak: 5, longestStreak: 14, daysIdle: 0, removalAfterDays: 30,
    topThree: true, watchPartyMinutes: 185,
  },
});

const demoStart = Date.now();
function demoJourney(r: MediaRequest): MediaRequest {
  const t = (Date.now() - demoStart) / 1000;
  if (t < 3) return { ...r, stage: "requested", progress: null };
  if (t < 6) return { ...r, stage: "approved", progress: { detail: "Approved, looking for a copy" } };
  if (t < 13) {
    const pct = Math.min(100, Math.round(((t - 6) / 7) * 100));
    return { ...r, stage: "downloading", progress: { percent: pct, detail: `Season pack, 9 episodes, ${pct > 90 ? "under a minute" : `about ${Math.max(1, Math.round((100 - pct) / 12))} min`} left` } };
  }
  if (t < 18) {
    const n = Math.min(12, 1 + Math.floor(((t - 13) / 5) * 12));
    return { ...r, stage: "unpacking", progress: { percent: Math.round((n / 12) * 100), detail: `Unpacking, ${n} of 12 in SABnzbd` } };
  }
  if (t < 21) return { ...r, stage: "importing", progress: { percent: 100, detail: "Plex usually picks it up within a few minutes" } };
  return { ...r, stage: "available", progress: null };
}
export const myRequests = () => wait([...mine].map((r) => withTicket(DEMO && r.slot === 214 ? demoJourney(r) : r)).sort((a, b) => b.slot - a.slot), DEMO ? 60 : 260);

export const search = (q: string, kind: MediaKind) => {
  const needle = q.trim().toLowerCase();
  const isBook = kind === "audiobook" || kind === "ebook";
  const pool = titles.filter((t) => (isBook ? t.kind === "audiobook" || t.kind === "ebook" : t.kind === kind));
  const hits = needle ? pool.filter((t) => t.title.toLowerCase().includes(needle) || t.author?.toLowerCase().includes(needle)) : pool;
  return wait(hits.map((t) => {
    const r = mine.find((m) => m.title.id === t.id && m.stage !== "declined");
    return { ...t, kind: isBook ? kind : t.kind, availability: r ? (r.stage === "available" ? "available" : "requested") : t.availability } as Title;
  }), 420);
};

export const title = (kind: MediaKind, id: string) => {
  const t = titles.find((x) => x.id === id);
  if (!t) return Promise.reject(new Error("Not found"));
  const requested = mine.find((r) => r.title.id === id && r.stage !== "declined");
  const asked = new Set(mine.filter((r) => r.title.id === id && r.stage !== "declined").flatMap((r) => (Array.isArray(r.seasons) ? r.seasons : [])));
  const onPlex: Record<string, Record<number, number>> = { "Pepper & Carrot": { 1: 9 }, "The Daily Dweebs": { 1: 8, 2: 10, 3: 4 } };
  const seasons = t.seasons?.map((s) => {
    const have = onPlex[t.title]?.[s.n] ?? 0;
    const status = s.episodes && have >= s.episodes ? "available" : asked.has(s.n) ? "requested" : have ? "partial" : !s.episodes ? "upcoming" : "none";
    return { ...s, have, status } as const;
  });
  return wait({
    ...t, kind, seasons,
    availability: requested ? (requested.stage === "available" ? "available" : "requested") : t.availability,
    yourRequest: requested ? { slot: requested.slot, stage: requested.stage } : undefined,
  } as Title);
};

export const request = (body: { kind: MediaKind; id: string; seasons?: number[] | "all" | "latest"; format?: BookFormat }) => {
  const t = titles.find((x) => x.id === body.id)!;
  const r: MediaRequest = {
    slot: nextSlot++, title: { ...t, kind: body.kind, availability: "requested" }, stage: "requested",
    seasons: body.seasons, format: body.format, requestedAt: new Date().toISOString(), updatedAt: new Date().toISOString(),
  };
  mine = [r, ...mine];
  return wait(r, 700);
};

export const join = (_email: string) => wait(undefined, 700);
export const logout = () => {
  try { localStorage.setItem(KEY, "guest"); } catch { /* private mode */ }
  return wait(undefined, 100);
};

const libraryOrder = ["Sintel", "King of the Rocket Men", "Coffee Run", "Charge", "The Wonderful Wizard of Oz", "Radar Men from the Moon", "Elephants Dream", "The Daily Dweebs", "Cosmos Laundromat", "Sprite Fright", "Dracula", "The Great Gatsby", "Big Buck Bunny", "Plan 9 from Outer Space", "House on Haunted Hill", "Night of the Living Dead", "Carnival of Souls"];

export const library = (kind: LibraryKind): Promise<LibraryItem[]> => wait(
  libraryOrder
    .map((name, i) => ({ ...byTitle(name), availability: "available" as const, addedAt: ago(60 * (1 + i * i * 9)) }))
    .filter((t) => (kind === "book" ? t.kind === "audiobook" || t.kind === "ebook" : t.kind === kind)),
  360,
);

/** What the admin did in this dev session, so the sample lists react. */
const decided = new Set<string>();
const kept = new Map<string, boolean>();
export const sampleDecide = (what: string, id: string, approve: boolean) => {
  decided.add(`${what}:${id}`);
  return wait({ ok: true, message: approve ? "Request approved and submitted to Seerr!" : "Declined." });
};
export const sampleExempt = (ratingKey: string, keep: boolean) => {
  kept.set(ratingKey, keep);
  return wait({ ok: true, message: keep ? "Kept permanently." : "No longer kept." });
};
export const sampleRemove = (plexName: string) => {
  decided.add(`people:${plexName}`);
  return wait({ ok: true, message: `Removed ${plexName} from Plex.` });
};

/** Invite links made in this dev session. */
const invites: import("./types").AdminInvite[] = [
  { id: "a".repeat(64), label: "Aunt Rosa", email: null, createdBy: "Sam Rivera", createdAt: ago(60 * 26), expiresAt: new Date(Date.now() + 5 * 864e5).toISOString(), status: "active", usedBy: null, usedAt: null },
  { id: "b".repeat(64), label: "Grandpa", email: "grandpa@example.com", createdBy: "Sam Rivera", createdAt: ago(60 * 24 * 9), expiresAt: ago(60 * 24 * 2), status: "used", usedBy: "grandpa_j", usedAt: ago(60 * 24 * 8) },
];
export const inviteInfo = () => wait({ valid: new URLSearchParams(location.search).get("bad") === null, label: "Aunt Rosa", inviter: "Sam Rivera", emailLocked: false, expiresAt: new Date(Date.now() + 5 * 864e5).toISOString(), inactivityDays: 30, warnDays: 25 });
export const createInvite = (body: { label: string; email?: string; days: number }) => {
  const invite = { id: Math.random().toString(16).slice(2).padEnd(64, "0"), label: body.label, email: body.email || null, createdBy: "Sam Rivera", createdAt: new Date().toISOString(), expiresAt: new Date(Date.now() + body.days * 864e5).toISOString(), status: "active" as const, usedBy: null, usedAt: null };
  invites.unshift(invite);
  return wait({ url: `${location.origin}/invite/SAMPLExSAMPLExSAMPLExSAMPLExSAMPLE`, invite }, 400);
};
export const deleteInvite = (id: string) => {
  const i = invites.findIndex((x) => x.id === id);
  const label = i >= 0 ? invites[i].label : "them";
  if (i >= 0) invites.splice(i, 1);
  return wait({ ok: true, message: `Deleted the invite for ${label}.` });
};
export const renewInvite = (id: string) => {
  const old = invites.find((x) => x.id === id);
  if (old && old.status !== "used") invites.splice(invites.indexOf(old), 1);
  return createInvite({ label: old?.label ?? "Someone", email: old?.email ?? undefined, days: 7 });
};
export const revokeInvite = (id: string) => {
  const i = invites.find((x) => x.id === id);
  if (i) i.status = "revoked";
  return wait({ ok: true, message: `The link for ${i?.label ?? "them"} no longer works.` });
};

/** Cleanup settings changed in this dev session. */
const cleanupSettings = { enabled: true, practice: false, inactivityDays: 90, warnDaysBefore: 7, excludedLibraries: [] as string[], channelId: "11" as string | null };
export const sampleCleanupSettings = (change: Partial<typeof cleanupSettings>) => {
  Object.assign(cleanupSettings, change);
  return wait({ ok: true, message: "Saved." });
};

/** Invented admin data for local design work only. */
export const admin = (section: string): Promise<unknown> => {
  const data: Record<string, unknown> = {
    requests: {
      pending: [
        { id: "1", slot: 216, title: "Tears of Steel", kind: "movie", poster: byTitle("Tears of Steel").poster, seasons: null, requester: "Alex Kim", requestedAt: ago(90), status: "pending" },
        { id: "2", slot: 217, title: "Pepper & Carrot", kind: "tv", poster: byTitle("Pepper & Carrot").poster, seasons: "latest", requester: "Jordan Lee", requestedAt: ago(40), status: "pending" },
        { id: "3", slot: 218, title: "The War of the Worlds", kind: "audiobook", poster: null, seasons: null, requester: "Sam Ortiz", requestedAt: ago(12), status: "pending" },
      ],
      older: [],
      recent: [{ id: "9", slot: 214, title: "Elephants Dream", kind: "movie", poster: null, seasons: null, requester: "Alex Kim", requestedAt: ago(3000), status: "approved", resolvedBy: "you", resolvedAt: ago(2000) }],
    },
    plexinvites: [
      { email: "jordan@example.com", name: "", sentAt: ago(60 * 3), who: "Jordan Lee" },
      { email: "old-typo@exmaple.com", name: "", sentAt: ago(60 * 24 * 40), who: null },
    ],
    joins: [
      { key: "d2", messageId: "2", name: "Jordan Lee", via: "discord", email: "jordan@example.com", status: "pending", askedAt: ago(300) },
      { key: "p5", messageId: "5", name: "riley.plex (Plex sign-in)", via: "plex", email: "", status: "pending", askedAt: ago(30) },
    ],
    people: [
      { plexName: "Marcus T.", discordName: "Marcus T.", discordId: "301", linked: true, lastWatched: ago(60 * 24 * 26), daysIdle: 26, warned: true, topThree: false, removalIn: 4, warnAfter: 25 },
      { plexName: "Priya N.", discordName: null, linked: false, lastWatched: ago(60 * 24 * 12), daysIdle: 12, warned: false, topThree: false, removalIn: 18, warnAfter: 25 },
      { plexName: "Alex Kim", discordName: "alexk", discordId: "302", linked: true, lastWatched: ago(60 * 5), daysIdle: 0, warned: false, topThree: true, removalIn: null, warnAfter: 25 },
    ],
    cleanup: {
      settings: { ...cleanupSettings },
      libraries: ["Movies", "TV Shows", "Music", "Kids"],
      channels: [{ id: "11", name: "plex-updates" }, { id: "12", name: "admin" }],
      warning: [
        { ratingKey: "1", title: "Elephants Dream", type: "movie", daysLeft: 5, reason: "added", lastActivity: ago(60 * 24 * 85) },
        { ratingKey: "2", title: "Radar Men from the Moon", type: "show", daysLeft: 6, reason: "last watched", lastActivity: ago(60 * 24 * 84) },
      ],
      upcoming: [{ ratingKey: "3", title: "Pepper & Carrot", type: "show", daysLeft: 41, reason: "last watched", lastActivity: ago(60 * 24 * 49) }],
      exempt: [{ ratingKey: "4", title: "The Daily Dweebs", type: "show" }],
    },
    health: [
      { name: "Plex", ok: true, ms: 12 }, { name: "Seerr", ok: true, ms: 48 }, { name: "Sonarr", ok: true, ms: 30 },
      { name: "Radarr", ok: true, ms: 26 }, { name: "Tautulli", ok: false, ms: 6000, detail: "TimeoutError" }, { name: "SABnzbd", ok: true, ms: 18 },
    ],
  };
  data.invites = invites.map((i) => ({ ...i }));
  data.all = allRequests("");
  data.tickets = ticketsList();
  data.help = helps.map((h) => ({ ...h }));
  data.messages = Object.entries(conversations).map(([id, list]) => {
    const last = list[list.length - 1];
    const done = doneBy[id] ?? null;
    return { id, name: names[id], count: list.filter((m) => m.direction === "out").length, received: list.filter((m) => m.direction === "in").length,
      unread: list.filter((m) => m.direction === "in" && !(done && done.at >= m.at)).length, done,
      ticket: id === "d302" ? { id: "h1", title: "Pepper & Carrot", slot: 213 } : null,
      failed: list.filter((m) => !m.delivered).length, via: [...new Set(list.map((m) => m.channel))],
      last: { at: last.at, text: last.title ?? last.text, channel: last.channel, delivered: last.delivered, direction: last.direction } };
  }).sort((a, b) => b.last.at.localeCompare(a.last.at));
  data.discord = {
    channels: [{ id: "10", name: "general" }, { id: "11", name: "plex-updates" }, { id: "12", name: "movie-night" }],
    inbox: { autoreply: true, threadsMissing: null },
    party: { channel: "Watch Party", streamer: "Alex Kim", title: "Tears of Steel", startedAt: ago(42), people: ["Alex Kim", "Jordan Lee", "Priya"] },
    joins: [
      { who: "Grandpa", by: "Sam Rivera", via: "plexbie", code: "Grandpa", at: ago(60 * 24 * 8), role: null },
      { who: "Jordan Lee", by: "Alex Kim", via: "discord", code: "aB3dEf", at: ago(60 * 24 * 40), role: "Smart" },
      { who: "Priya", by: "Sam Rivera", via: "discord", code: "Xy9zQ1", at: ago(60 * 24 * 95), role: null },
    ],
  };
  type Row = { ratingKey: string; title: string; type: string };
  const req = data.requests as { pending: { id: string }[] };
  req.pending = req.pending.filter((r) => !decided.has(`requests:${r.id}`));
  data.joins = (data.joins as { messageId: string; status: string }[]).map((j) => (decided.has(`joins:${j.messageId}`) ? { ...j, status: "approved" } : j));
  data.people = (data.people as { plexName: string }[]).filter((p) => !decided.has(`people:${p.plexName}`));
  const c = data.cleanup as { warning: Row[]; upcoming: Row[]; exempt: Omit<Row, "daysLeft">[] };
  const all = [...c.warning, ...c.upcoming, ...c.exempt];
  const isKept = (r: Row) => kept.get(r.ratingKey) ?? c.exempt.some((e) => e.ratingKey === r.ratingKey);
  c.warning = c.warning.filter((r) => !isKept(r));
  c.upcoming = c.upcoming.filter((r) => !isKept(r));
  c.exempt = all.filter(isKept).map(({ ratingKey, title, type }) => ({ ratingKey, title, type }));
  return wait(data[section]);
};

/** The language chips, remembered for the visit. */
let languages: string[] = [];
const languageOptions = [{ code: "en", name: "English" }, { code: "ja", name: "Japanese" }, { code: "ko", name: "Korean" }];
export const prefs = () => wait({ languages, languageOptions }, 200);
export const saveLanguages = (next: string[]) => { languages = [...next].sort(); return wait({ languages }, 200); };
export const searchAll = async (q: string) => {
  const [movie, tv, book] = await Promise.all([search(q, "movie"), search(q, "tv"), search(q, "audiobook")]);
  return { movie, tv, book };
};

/** Discover, from the invented catalogue: two shelves per kind, two genres. */
export const discover = (kind: "movie" | "tv") => {
  const mine = titles.filter((t) => t.kind === kind);
  return wait({ kind, genres: [{ id: kind === "movie" ? 878 : 10765, name: "Science Fiction" }, { id: 18, name: "Drama" }], languages, languageOptions, shelves: [
    { key: "trending", title: "Trending this week", titles: mine.slice(0, 8), more: false },
    { key: "popular", title: "Popular", titles: mine.slice(8, 16), more: false },
  ].filter((s) => s.titles.length) }, 380);
};
export const similar = (kind: string, id: string) => wait(titles.filter((t) => t.kind === kind && t.id !== id).slice(0, 8), 300);

/** Trending, from the invented catalogue: the first five films and shows. */
export const popular = () => wait({
  movies: titles.filter((t) => t.kind === "movie").slice(0, 5),
  tv: titles.filter((t) => t.kind === "tv").slice(0, 5),
}, 380);

/** Invented conversations for the Messages tab. */
const names: Record<string, string> = { d301: "Marcus T.", pgrandpa_j: "grandpa_j", d302: "Alex Kim" };
type Msg = { id: string; at: string; direction: "out" | "in"; channel: "discord" | "web" | "email" | "none"; delivered: boolean; title: string | null;
  text: string; context: string; error: string | null; by?: string | null; ticket?: string | null };
const msg = (minutes: number, channel: Msg["channel"], text: string, title: string | null = null, delivered = true, error: string | null = null,
  direction: Msg["direction"] = "out"): Msg => ({ id: `m${minutes}${channel}${direction}`, at: ago(minutes), direction, channel, delivered, title, text, context: "", error });
/** Something the person sent Plexbie. */
const said = (minutes: number, channel: "discord" | "web", text: string, title: string | null = null): Msg =>
  ({ ...msg(minutes, channel, text, title, true, null, "in"), context: title ? "ticket answer" : "Discord DM" });
const conversations: Record<string, Msg[]> = {
  d302: [
    msg(60 * 24 * 3, "discord", "Your request has been approved! A Plex invitation has been sent to alex@example.com.", "Request Approved!"),
    msg(60 * 5, "discord", "Good news! Tears of Steel is now available on Plex."),
    msg(42, "discord", "Good news! Pepper & Carrot is now ready to start on Plex. Season 2 episode 1 is available."),
    said(40, "web", "Stuck downloading. It's been at 62% since this morning.", "Something wrong with Pepper & Carrot"),
    msg(30, "discord", "Found a copy that works. Is the 4K version OK, or would you rather wait for 1080p?", "🛠️ About your request: Pepper & Carrot"),
    said(22, "discord", "4K is great, thank you!", "Answer about Pepper & Carrot"),
    said(6, "discord", "oh and the subtitles on episode 3 are out of sync"),
  ],
  d301: [
    msg(60 * 24, "discord", "Hey there! We noticed you haven't watched anything on the Plex server in 25 days.\nWhat happens next?: If you remain inactive for 5 more days you'll be removed.", "Plex Inactivity Warning"),
    said(60 * 23, "discord", "Sorry! Been travelling, I'll watch something this weekend 🙏"),
    msg(60 * 2, "discord", "Sorry, your request for Zombies of the Stratosphere was declined.", null, false, "Their Discord DMs are closed"),
  ],
  pgrandpa_j: [
    msg(60 * 30, "web", "Season 3 is on its way. You'll hear again when it's ready.", "Request approved: The Daily Dweebs"),
    msg(60 * 8, "none", "You haven't watched anything on the household Plex in 25 days.", "Watch something to keep your Plex access", false, "No phone alerts turned on and no email to send to"),
  ],
};
export const conversation = (who: string) => wait((conversations[who] ?? []).map((m) => ({ ...m })));
/** Who marked each conversation done (sample visit only). */
const doneBy: Record<string, { at: string; by: string }> = { pgrandpa_j: { at: ago(60 * 7), by: "Sam Rivera" } };
export const messageReply = (who: string, text: string) => {
  conversations[who]?.push({ ...msg(0, who.startsWith("d") ? "discord" : "web", `${text}\n— Sam Rivera (admin)`), id: `r${Date.now()}`, by: "Sam Rivera" });
  doneBy[who] = { at: new Date().toISOString(), by: "Sam Rivera" };
  return wait({ ok: true, message: `Sent to ${names[who]} as a ${who.startsWith("d") ? "Discord DM" : "phone alert"}.` }, 500);
};
export const messageDone = (who: string, done: boolean) => {
  if (done) doneBy[who] = { at: new Date().toISOString(), by: "Sam Rivera" }; else delete doneBy[who];
  return wait({ ok: true, message: done ? "Marked done. It stays in the history." : "Marked unread." }, 300);
};
export const messageToTicket = (key: string) => {
  const m = Object.values(conversations).flat().find((x) => x.id === key);
  if (m) m.ticket = "h1";
  return wait({ ok: true, message: "Added to their ticket on Pepper & Carrot." }, 400);
};

/** Help requests made in this dev session. */
const helps: import("./types").AdminHelp[] = [
  { id: "h1", request: "6001", slot: 212, title: "Undersea Kingdom", kind: "tv", seasons: [2], who: "Jordan Lee", reason: "Stuck downloading",
    note: "It's been at 0% since this morning.", status_then: "Downloading, 0%", status: "open", created_at: ago(35) },
  { id: "h2", request: "6002", slot: 214, title: "Elephants Dream", kind: "movie", seasons: null, who: "Alex Kim", reason: "Can't be found", offer: "name",
    note: "Searching by its IDs found nothing Plexbie could grab for Elephants Dream: 212 releases came back. Search by name instead?",
    status_then: "Nothing found", status: "open", created_at: ago(12) },
  { id: "h0", request: "6000", slot: 205, title: "Elephants Dream", kind: "movie", seasons: null, who: "Alex Kim", reason: "Wrong version or quality",
    note: "Audio is in German", status_then: "On Plex", status: "resolved", created_at: ago(60 * 30), resolved_by: "Sam Rivera", resolved_at: ago(60 * 26), reply: "Swapped it for the English release." },
];
export const askHelp = (requestId: string, reason: string) => {
  const r = mine.find((m) => m.id === requestId);
  const words: Record<string, string> = { stuck: "Stuck downloading", quality: "Wrong version or quality", episodes: "Wrong or missing episodes", playback: "Won't play on Plex", other: "Something else" };
  const help = { id: `h${Date.now()}`, reason: words[reason] ?? "Something else" };
  if (r) r.help = help;
  return wait({ ok: true, message: "Sent. An admin will take a look and get back to you.", help }, 500);
};
export const helpResolve = (id: string) => {
  const h = helps.find((x) => x.id === id);
  if (h) { h.status = "resolved"; h.resolved_by = "Sam Rivera"; h.resolved_at = new Date().toISOString(); }
  return wait({ ok: true, message: `Resolved, and ${h?.who ?? "they"} has been told.` });
};

/* ------------------------------------------------- all requests (Manage) */

type Row = import("./types").AdminRequestRow;
const simpsons: Title = { ...byTitle("Undersea Kingdom"), availability: "requested" };
/** Everyone's requests as an admin sees them: two stuck, some on their way, one finished,
 *  one waiting for a decision and one declined. */
const everyone: Row[] = [
  { id: "6001", slot: 212, title: simpsons, stage: "downloading", seasons: [2], requester: "Jordan Lee", status: "approved",
    progress: { percent: 0, detail: "Season pack, 22 episodes" }, help: { id: "h1", reason: "Stuck downloading" },
    requestedAt: ago(60 * 30), updatedAt: ago(60 * 20), approvedBy: "Sam Rivera", approvedAt: ago(60 * 29), stageSince: ago(60 * 9),
    stuck: ["Help asked: Stuck downloading", "Download hasn't moved in 6 hours"] },
  { id: "6002", slot: 214, title: byTitle("Elephants Dream"), stage: "searching", requester: "Alex Kim", status: "approved",
    progress: { detail: "Looking for a copy" }, help: { id: "h2", reason: "Can't be found" },
    requestedAt: ago(60 * 50), updatedAt: ago(60 * 48), approvedBy: "Sam Rivera", approvedAt: ago(60 * 48), stageSince: ago(60 * 48),
    stuck: ["Help asked: Can't be found", "Nothing found for over a day"] },
  { id: "5001", slot: 213, title: byTitle("Pepper & Carrot"), stage: "downloading", seasons: [2], requester: "Jordan Lee", status: "approved",
    progress: { percent: 62, detail: "Season pack, 9 episodes, about 4 min left" },
    requestedAt: ago(60 * 26), updatedAt: ago(14), approvedBy: "Sam Rivera", approvedAt: ago(60 * 25), stageSince: ago(50), stuck: [] },
  { id: "5002", slot: 211, title: byTitle("The War of the Worlds"), stage: "unpacking", format: "audiobook", requester: "Sam Ortiz", status: "approved",
    progress: { percent: null, detail: "Unpacking in SABnzbd" },
    requestedAt: ago(60 * 49), updatedAt: ago(60 * 3), approvedBy: "Alex Kim", approvedAt: ago(60 * 47), stageSince: ago(20), stuck: [] },
  { id: "5003", slot: 210, title: byTitle("Tears of Steel"), stage: "importing", requester: "Priya N.", status: "approved",
    progress: { percent: 100, detail: "Downloaded, moving it onto Plex" },
    requestedAt: ago(60 * 8), updatedAt: ago(30), approvedBy: "Sam Rivera", approvedAt: ago(60 * 7), stageSince: ago(6), stuck: [] },
  { id: "5004", slot: 205, title: byTitle("Big Buck Bunny"), stage: "upcoming", requester: "Priya N.", status: "approved",
    progress: { releaseDate: new Date(Date.now() + 23 * 864e5).toISOString().slice(0, 10), releaseKind: "digital", detail: "Out to stream in 23 days. Plexbie gets it then." },
    requestedAt: ago(60 * 24 * 3), updatedAt: ago(60 * 24 * 3), approvedBy: "Sam Rivera", approvedAt: ago(60 * 24 * 3), stageSince: ago(60 * 24 * 3), stuck: [] },
  { id: "4990", slot: 197, title: byTitle("Sintel"), stage: "available", requester: "Alex Kim", status: "approved",
    requestedAt: ago(60 * 24 * 6), updatedAt: ago(60 * 24 * 4), approvedBy: "Sam Rivera", approvedAt: ago(60 * 24 * 6),
    stageSince: ago(60 * 24 * 4), finishedAt: ago(60 * 24 * 4), stuck: [] },
  { id: "7001", slot: 216, title: byTitle("Tears of Steel"), stage: "requested", requester: "Alex Kim", status: "pending",
    progress: { detail: "Waiting for an admin to approve it" }, requestedAt: ago(90), updatedAt: ago(90), stuck: [] },
  { id: "4980", slot: 199, title: byTitle("Spring"), stage: "declined", requester: "Marcus T.", status: "declined",
    note: "Already on Plex in 4K", requestedAt: ago(60 * 24 * 9), updatedAt: ago(60 * 24 * 9), stuck: [] },
];
/** Older ones, only found by searching (like anything over 30 days done). */
const archive: Row[] = [
  { id: "4100", slot: 120, title: byTitle("King of the Rocket Men"), stage: "available", seasons: [1], requester: "Marcus T.", status: "approved",
    requestedAt: ago(60 * 24 * 70), updatedAt: ago(60 * 24 * 66), approvedBy: "Sam Rivera", approvedAt: ago(60 * 24 * 70),
    stageSince: ago(60 * 24 * 66), finishedAt: ago(60 * 24 * 66), stuck: [] },
  { id: "4020", slot: 64, title: byTitle("Radar Men from the Moon"), stage: "available", seasons: [1], requester: "Priya N.", status: "approved",
    requestedAt: ago(60 * 24 * 200), updatedAt: ago(60 * 24 * 198), approvedBy: "Sam Rivera", approvedAt: ago(60 * 24 * 200),
    stageSince: ago(60 * 24 * 198), finishedAt: ago(60 * 24 * 198), stuck: [] },
  { id: "4001", slot: 1, title: byTitle("The Wonderful Wizard of Oz"), stage: "declined", requester: "Jordan Lee", status: "declined",
    requestedAt: ago(60 * 24 * 400), updatedAt: ago(60 * 24 * 400), stuck: [] },
];
const activity: Record<string, { at: string; by: string; did: string }[]> = {};

function allRequests(q: string, everything = false) {
  const query = q.trim().toLowerCase().replace(/^(no\.|#)\s*/, "").replace(/^0+/, "");
  const open = new Set(helps.filter((h) => h.status === "open").map((h) => h.request));
  const live = everyone.map((r) => (r.help && !open.has(r.id!) ? { ...r, help: null, stuck: r.stuck.filter((s) => !s.startsWith("Help asked")) } : r));
  if (query) {
    const words = (r: Row) => `${r.title.title} ${r.requester}`.toLowerCase();
    const found = [...live, ...archive].filter((r) => words(r).includes(query) || String(r.slot) === query).sort((a, b) => b.slot - a.slot);
    return { rows: found, counts: null, query };
  }
  const ended = (r: Row) => r.stage === "declined" || r.stage === "closed";
  const rows = [...live, ...(everything ? archive : [])].sort((a, b) => Number(!a.stuck.length) - Number(!b.stuck.length) || b.slot - a.slot);
  return { rows, counts: { active: rows.filter((r) => !["available", "requested"].includes(r.stage) && !ended(r)).length, stuck: rows.filter((r) => r.stuck.length).length,
    waiting: rows.filter((r) => r.stage === "requested").length, finished: rows.filter((r) => r.stage === "available").length,
    declined: rows.filter(ended).length }, query: null, everything, total: everyone.length + archive.length };
}
export const adminAll = (q: string, everything = false) => wait(allRequests(q, everything), 300);
export const adminRequest = (key: string) => {
  const r = [...allRequests("").rows, ...archive].find((x) => x.id === key) ?? everyone[0];
  const tickets = helps.filter((h) => h.request === key).map((h) => ({ ...h }));
  return wait({ ...r, via: key === "5002" ? "the website" : "Discord", seerrId: null, discordUrl: null, tickets, activity: activity[key] ?? [] }, 250);
};
export const requestTicket = (key: string, note: string, tell: boolean, message = "") => {
  const r = everyone.find((x) => x.id === key);
  const help = { id: `h${Date.now()}`, reason: "Opened by an admin" };
  helps.unshift({ id: help.id, request: key, slot: r?.slot ?? 0, title: r?.title.title ?? "", kind: r?.title.kind ?? "movie", seasons: null,
    who: r?.requester ?? "Someone", reason: help.reason, note, status_then: r ? r.stage : "", status: "open", created_at: new Date().toISOString(),
    ...({ opened_by: "Sam Rivera" } as object) });
  threadOf(helps[0]);
  if (tell) entry(help.id, "reply", "Sam Rivera", message.trim() || `An admin is looking into your request for ${r?.title.title ?? "it"}.`);
  if (r) { r.help = help; r.stuck = [`Help asked: ${help.reason}`, ...r.stuck]; }
  return wait({ ok: true, message: `Ticket opened${tell ? `, and ${r?.requester ?? "they"} has been told` : ""}. It's on Manage → Tickets.`, help }, 450);
};
export const requestSearch = (key: string, how: "again" | "episodes" | "name") => {
  const said = { again: "Searching again.", episodes: "Searching one episode at a time.", name: "Plexbie is searching NZBHydra by name. It reports back in the admin channel." }[how];
  (activity[key] ??= []).push({ at: new Date().toISOString(), by: "Sam Rivera", did: said });
  return wait({ ok: true, message: said }, 500);
};

/* ------------------------------------------------------------- tickets */

type Entry = import("./types").TicketEntry;
type Help = import("./types").AdminHelp & { opened_by?: string };
const threads: Record<string, Entry[]> = {};
const owners: Record<string, string | null> = { h1: "Sam Rivera" };
const waits: Record<string, boolean> = { h3: true };
let nextEntry = 1;
const at = (minutesAgo: number) => new Date(Date.now() - minutesAgo * 60_000).toISOString();

/** The demo member's own ticket: an admin asked them something and waits on the answer. */
helps.push({ id: "h3", request: "5001", slot: 214, title: "Pepper & Carrot", kind: "tv", seasons: [2], who: "Sam Rivera",
  reason: "Wrong or missing episodes", note: "Episode 4 is missing", status_then: "Downloading, 62%", status: "open", created_at: at(90) });
threads.h3 = [
  { id: "e1", at: at(90), by: "Sam Rivera", kind: "member", text: "Wrong or missing episodes. Episode 4 is missing" },
  { id: "e2", at: at(70), by: "Alex Kim", kind: "note", text: "Sonarr skipped E04, it was flagged as a sample. Retrying." },
  { id: "e3", at: at(65), by: "Alex Kim", kind: "reply", text: "Found it. Is it the 4K version you're after, or is 1080p fine?" },
  { id: "e4", at: at(64), by: "Alex Kim", kind: "status", text: "Waiting on them" },
];

/** A download Sonarr won't import by itself, as Plexbie opens it (core/blocked_imports). */
const BLOCKED_NOTE = "Radar Men from the Moon finished downloading, but Sonarr won't import it by itself: Found matching series via grab history, but release was matched to series by ID. Automatic import is not possible. Look at the files on this ticket before you import it.";
helps.push({ id: "h4", request: "5099", slot: 216, title: "Radar Men from the Moon", kind: "tv", seasons: [1], who: "Jordan Lee",
  reason: "Downloaded, but won't import", note: BLOCKED_NOTE, status_then: "Downloaded, import blocked", status: "open", created_at: at(8), opened_by: "Plexbie" } as Help);
threads.h4 = [{ id: "e9", at: at(8), by: "Plexbie", kind: "note", text: BLOCKED_NOTE }];
const sampleBlocked = { app: "sonarr" as const, downloadId: "SABnzbd_nzo_demo" };
export const adminBlocked = () => wait({ rows: [{ ...sampleBlocked, title: "Radar Men from the Moon", year: 1952,
  release: "Radar.Men.From.The.Moon.S01.1080p.WEB", episodes: ["S01E01", "S01E02", "S01E03"], ticket: "h4",
  messages: ["Found matching series via grab history, but release was matched to series by ID. Automatic import is not possible."] }] });
const RADAR_TITLES = ["Moon Rocket", "Molten Terror", "Bridge of Death", "Flight to Destruction", "Murder Car", "Hills of Death",
  "Camouflaged Destruction", "The Enemy Planet", "Battle in the Stratosphere", "Mass Execution", "Planned Pursuit", "Death of the Moon Man"];
export const sampleEpisodes = RADAR_TITLES.map((title, i) => ({ id: 700 + i + 1, label: `S01E${String(i + 1).padStart(2, "0")}`, season: 1,
  episode: i + 1, title, hasFile: false }));
export const blockedPreview = () => wait({
  ...sampleBlocked, title: "Radar Men from the Moon", year: 1952, release: "Radar.Men.From.The.Moon.S01.1080p.WEB",
  folder: "/data/usenet/complete/tv/Radar.Men.From.The.Moon.S01.1080p.WEB", episodes: ["S01E01", "S01E02", "S01E03"],
  messages: ["Found matching series via grab history, but release was matched to series by ID. Automatic import is not possible."],
  warnings: [],
  files: [1, 2, 3].map((n) => ({ name: `Radar.Men.From.The.Moon.S01E0${n}.1080p.WEB.mkv`, size: (1.1 + n / 10) * 2 ** 30,
    as: n === 3 ? [] : [`S01E0${n}`], quality: "WEBDL-1080p", qualityId: 3, languages: [{ id: 1, name: "English" }], releaseGroup: "WEB",
    rejections: n === 3 ? ["Unable to determine if file is a sample"] : [],
    notes: n === 3 ? ["Unable to determine if file is a sample", "Sonarr can't tell which episode this is: pick it"] : [],
    episodes: n === 3 ? [] : [sampleEpisodes[n - 1]], seriesId: 41, movie: null, ready: n !== 3 })),
  others: [{ name: "Radar.Men.From.The.Moon.S01.nfo", size: 2048, danger: false }],
  ok: true,
  series: { id: 41, title: "Radar Men from the Moon", year: 1952 }, movie: null,
  options: { qualities: [{ id: 3, name: "WEBDL-1080p" }, { id: 7, name: "Bluray-1080p" }, { id: 18, name: "WEBDL-2160p" }],
    languages: [{ id: 1, name: "English" }, { id: 2, name: "French" }], episodes: sampleEpisodes },
}, 400);

function threadOf(h: Help): Entry[] {
  threads[h.id] ??= [
    { id: `x${nextEntry++}`, at: h.created_at, by: h.opened_by ?? h.who, kind: h.opened_by ? "note" : "member",
      text: h.opened_by ? h.note || h.reason : h.note ? `${h.reason}. ${h.note}` : h.reason },
    ...(h.actions ?? []).map((a): Entry => ({ id: `x${nextEntry++}`, at: a.at, by: a.by, kind: "action", text: a.did })),
    ...(h.status === "resolved" ? [
      ...(h.reply ? [{ id: `x${nextEntry++}`, at: h.resolved_at ?? h.created_at, by: h.resolved_by ?? "An admin", kind: "reply", text: h.reply } as Entry] : []),
      { id: `x${nextEntry++}`, at: h.resolved_at ?? h.created_at, by: h.resolved_by ?? "An admin", kind: "status", text: "Solved" } as Entry,
    ] : []),
  ];
  return threads[h.id];
}
function entry(id: string, kind: Entry["kind"], by: string, text: string) {
  const h = helps.find((x) => x.id === id);
  if (h) threadOf(h).push({ id: `x${nextEntry++}`, at: new Date().toISOString(), by, kind, text });
}
function ticketRow(h: Help): import("./types").AdminTicketRow {
  const t = threadOf(h);
  const last = t[t.length - 1];
  return { id: h.id, requestKey: h.request, slot: h.slot, title: h.title, kind: h.kind, seasons: h.seasons, who: h.who,
    reason: h.reason, status: h.status, waiting: !!waits[h.id] && h.status === "open", owner: owners[h.id] ?? null,
    openedBy: h.opened_by ?? null, offer: h.offer ?? null, createdAt: h.created_at, updatedAt: last?.at ?? h.created_at,
    last: last ? { by: last.by, kind: last.kind, text: last.text.slice(0, 160) } : null, count: t.length };
}
export function ticketsList(): import("./types").AdminTickets {
  const rows = helps.map(ticketRow);
  const newest = (rs: typeof rows) => rs.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  const action = newest(rows.filter((r) => r.status === "open" && !r.waiting));
  const waiting = newest(rows.filter((r) => r.status === "open" && r.waiting));
  const solved = newest(rows.filter((r) => r.status !== "open"));
  return { rows: [...action, ...waiting, ...solved], counts: { action: action.length, waiting: waiting.length, solved: solved.length } };
}
export const adminTicket = (id: string) => {
  const h = helps.find((x) => x.id === id) ?? helps[0];
  const request = allRequests("").rows.find((r) => r.id === h.request) ?? null;
  return wait({ ...ticketRow(h), note: h.note, statusThen: h.status_then, quiet: false, thread: [...threadOf(h)], request,
    blocked: h.id === "h4" ? sampleBlocked : null }, 250);
};
export const ticketComment = (id: string, kind: "note" | "reply", text: string) => {
  entry(id, kind, "Sam Rivera", text);
  const who = helps.find((x) => x.id === id)?.who ?? "them";
  return wait({ ok: true, message: kind === "reply" ? `Sent to ${who}.` : "Note added. Only admins see it." }, 350);
};
export const ticketStatus = (id: string, status: "open" | "waiting" | "resolved", message = "") => {
  const h = helps.find((x) => x.id === id);
  if (!h) return wait({ ok: false, message: "No such ticket." });
  if (status === "resolved") {
    if (message.trim()) entry(id, "reply", "Sam Rivera", message.trim());
    entry(id, "status", "Sam Rivera", "Solved");
    h.status = "resolved"; h.resolved_by = "Sam Rivera"; h.resolved_at = new Date().toISOString(); waits[id] = false;
    return wait({ ok: true, message: `Resolved, and ${h.who} has been told.` });
  }
  if (h.status === "resolved") { h.status = "open"; entry(id, "status", "Sam Rivera", "Reopened"); }
  if (status === "waiting") { waits[id] = true; entry(id, "status", "Sam Rivera", "Waiting on them"); }
  else if (waits[id]) { waits[id] = false; entry(id, "status", "Sam Rivera", "Back with the admins"); }
  return wait({ ok: true, message: status === "waiting" ? `Waiting on ${h.who}'s answer.` : "Open." });
};
export const ticketTake = (id: string) => {
  const mine = owners[id] === "Sam Rivera";
  owners[id] = mine ? null : "Sam Rivera";
  entry(id, "status", "Sam Rivera", mine ? "Let it go" : "Took it");
  return wait({ ok: true, message: mine ? "Let go. Anyone can take it." : "It's yours. You'll get an alert when they answer." });
};
export const answerTicket = (requestId: string, text: string) => {
  const h = helps.find((x) => x.request === requestId && x.status === "open");
  if (!h) return wait({ ok: false, message: "This ticket is closed." });
  entry(h.id, "member", h.who, text);
  waits[h.id] = false;
  return wait({ ok: true, message: "Sent. The admins have it." }, 400);
};
/** A member's request with its open ticket, as they see it. */
function withTicket(r: MediaRequest): MediaRequest {
  const h = helps.find((x) => x.request === r.id && x.status === "open");
  if (!h) return r;
  const thread = threadOf(h).filter((e) => e.kind === "member" || e.kind === "reply" || (e.kind === "status" && ["Solved", "Reopened"].includes(e.text)))
    .map((e) => (e.kind === "member" ? { ...e, by: "You" } : e));
  return { ...r, help: { id: h.id, reason: h.reason, status: h.status, waiting: !!waits[h.id], thread } };
}
