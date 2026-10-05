// Sample data for design review. The people are invented; the titles and
// artwork are real public TMDB / Open Library records. Never shipped: client.ts
// only reaches this module in a dev build.
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
 * Demo mode (?demo): invented titles, authors and blurbs, and no artwork, so screen
 * recordings for the README show nothing that belongs to a studio or publisher.
 * Covers fall back to the code-drawn ones. Remembered for the tab's session.
 */
export const DEMO = import.meta.env.DEV && (() => {
  try {
    if (new URLSearchParams(location.search).has("demo")) sessionStorage.setItem("plexbie.demo", "1");
    return sessionStorage.getItem("plexbie.demo") === "1";
  } catch { return false; }
})();
const DEMO_NAMES: Record<string, string> = {
  "Severance": "Glass Office", "Dune: Part Two": "Sandsong", "The Bear": "Night Kitchen", "Past Lives": "Two Summers",
  "Shōgun": "Harbor Lords", "Everything Everywhere All at Once": "All the Doors at Once", "Slow Horses": "Back Office Spies",
  "Spider-Man: Across the Spider-Verse": "Thread Runner", "The Last of Us": "After the Bloom", "Oppenheimer": "The Bright Hour",
  "Andor": "Quiet Rebels", "The Wild Robot": "Tinker Island", "Blue Eye Samurai": "Indigo Blade", "Arrival": "First Signal",
  "Abbott Elementary": "Room Twelve", "Paddington 2": "Button & Biscuit", "Project Hail Mary": "Long Way to Tau",
  "Piranesi": "The House of Tides", "Tomorrow, and Tomorrow, and Tomorrow": "Player Two", "The Hobbit": "Over the Low Hills",
  "The Simpsons": "Maple Street", "FROM": "The Town That Stays", "The Office": "Paper & Co.",
  "Andy Weir": "R. Vale", "Susanna Clarke": "M. Ashdown", "Gabrielle Zevin": "J. Okafor", "J.R.R. Tolkien": "E. Thorne",
};
const DEMO_BLURBS: Record<string, string> = {
  "Night Kitchen": "Three roommates open a noodle stand that only trades after midnight, and the regulars start bringing more than appetites.",
  "Glass Office": "A window cleaner forty floors up keeps finding notes taped to the outside of the glass, all addressed to her.",
  "Sandsong": "A lighthouse with no sea, a town of kite-makers, and one very stubborn weather balloon.",
  "Two Summers": "Twin sisters swap summer jobs for a bet and both end up running the wrong family business.",
  "Long Way to Tau": "A retired delivery drone volunteers for one last parcel, to a moon nobody has mapped.",
  "Tinker Island": "An inventor's workshop floats away in a flood and washes up somewhere much stranger.",
};
let demoRe: RegExp | null = null;
function demoize<T>(value: T): T {
  demoRe ??= new RegExp(Object.keys(DEMO_NAMES).sort((a, b) => b.length - a.length).map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
  if (typeof value === "string") return value.replace(demoRe, (m) => DEMO_NAMES[m]) as T;
  if (Array.isArray(value)) return value.map(demoize) as T;
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, ["poster", "backdrop"].includes(k) ? null : demoize(v)])) as T;
  }
  return value;
}

export const wait = <T,>(value: T, ms = 260) => new Promise<T>((r) => setTimeout(() => r(DEMO ? demoize(value) : value), ms));
const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();

const titles: Title[] = (raw as Omit<Title, "availability">[]).map((t) => ({ ...t, availability: "none" }));
const byTitle = (name: string) => titles.find((t) => t.title === name || (t as { _orig?: string })._orig === name)!;

// Who already has what, so search results show real-looking availability.
const onPlex = new Set(["Arrival", "Paddington 2", "Slow Horses", "The Wild Robot", "Andor", "Oppenheimer", "The Hobbit", "Abbott Elementary", "The Bear", "Everything Everywhere All at Once", "Spider-Man: Across the Spider-Verse", "Piranesi", "Tomorrow, and Tomorrow, and Tomorrow"]);
const requestedByOthers = new Set(["Shōgun", "Past Lives"]);
if (DEMO) {
  for (const t of titles) {
    Object.assign(t, { _orig: t.title, title: DEMO_NAMES[t.title] ?? t.title, author: t.author ? DEMO_NAMES[t.author] ?? t.author : t.author,
      poster: null, backdrop: null, overview: DEMO_BLURBS[DEMO_NAMES[t.title] ?? ""] ?? "A household favourite, made up for this demo." });
  }
}
for (const t of titles) {
  const name = (t as { _orig?: string })._orig ?? t.title;
  t.availability = onPlex.has(name) ? "available" : requestedByOthers.has(name) ? "requested" : "none";
}

let mine: MediaRequest[] = [
  { id: "5001", slot: 214, title: byTitle("Severance"), stage: "downloading", seasons: [2], requestedAt: ago(60 * 26), updatedAt: ago(14) },
  { id: "5002", slot: 211, title: byTitle("Project Hail Mary"), stage: "unpacking", progress: { percent: null, detail: "Unpacking in SABnzbd" }, format: "audiobook", requestedAt: ago(60 * 49), updatedAt: ago(60 * 3) },
  { slot: 209, title: byTitle("Dune: Part Two"), stage: "requested", requestedAt: ago(60 * 5), updatedAt: ago(60 * 5) },
  { slot: 205, title: byTitle("Paddington 2"), stage: "upcoming", requestedAt: ago(60 * 24 * 3), updatedAt: ago(60 * 24 * 3),
    progress: { releaseDate: new Date(Date.now() + 23 * 864e5).toISOString().slice(0, 10), releaseKind: "digital",
      detail: `Out to stream ${new Date(Date.now() + 23 * 864e5).toLocaleDateString("en-US", { month: "short", day: "numeric" })}. Plexbie gets it then. In cinemas since Sep 30.` } },
  { slot: 197, title: byTitle("The Wild Robot"), stage: "available", requestedAt: ago(60 * 24 * 6), updatedAt: ago(60 * 24 * 4) },
  { slot: 188, title: byTitle("Blue Eye Samurai"), stage: "declined", requestedAt: ago(60 * 24 * 12), updatedAt: ago(60 * 24 * 11), note: "Season 2 isn't out yet. Ask again when it airs." },
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
  { title: { ...byTitle("The Wild Robot"), availability: "available" }, addedAt: ago(40) },
  { title: { ...byTitle("Slow Horses"), availability: "available" }, addedAt: ago(60 * 3), detail: "Season 4, 6 episodes" },
  { title: { ...byTitle("Abbott Elementary"), availability: "available" }, addedAt: ago(60 * 9), detail: "S4 E12" },
  { title: { ...byTitle("Oppenheimer"), availability: "available" }, addedAt: ago(60 * 20) },
  { title: { ...byTitle("The Hobbit"), availability: "available" }, addedAt: ago(60 * 30), detail: "Audiobook" },
  { title: { ...byTitle("Andor"), availability: "available" }, addedAt: ago(60 * 44), detail: "Season 2" },
  { title: { ...byTitle("Arrival"), availability: "available" }, addedAt: ago(60 * 70) },
]);

export const community = (): Promise<Community> => wait({
  onAir: [
    { member: "Alex Kim", title: "Slow Horses", subtitle: "S4 E3, “Hello Goodbye”", poster: byTitle("Slow Horses").poster, progress: 0.62, device: "Living room TV" },
    { member: "Priya N.", title: "Paddington 2", poster: byTitle("Paddington 2").poster, progress: 0.18, device: "iPad" },
    { member: "Marcus T.", title: "The Hobbit", subtitle: "Chapter 7", poster: byTitle("The Hobbit").poster, progress: 0.41, device: "Phone" },
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
export const myRequests = () => wait([...mine].map((r) => (DEMO && r.slot === 214 ? demoJourney(r) : r)).sort((a, b) => b.slot - a.slot), DEMO ? 60 : 260);

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
  const onPlex: Record<string, Record<number, number>> = { Severance: { 1: 9 }, "The Bear": { 1: 8, 2: 10, 3: 4 } };
  const seasons = t.seasons?.map((s) => {
    const have = onPlex[(t as { _orig?: string })._orig ?? t.title]?.[s.n] ?? 0;
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

const libraryOrder = ["The Wild Robot", "Slow Horses", "Abbott Elementary", "Oppenheimer", "The Hobbit", "Andor", "Arrival", "The Bear", "Everything Everywhere All at Once", "Spider-Man: Across the Spider-Verse", "Piranesi", "Tomorrow, and Tomorrow, and Tomorrow", "Paddington 2"];

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
        { id: "1", slot: 216, title: "Dune: Part Two", kind: "movie", poster: byTitle("Dune: Part Two").poster, seasons: null, requester: "Alex Kim", requestedAt: ago(90), status: "pending" },
        { id: "2", slot: 217, title: "Severance", kind: "tv", poster: byTitle("Severance").poster, seasons: "latest", requester: "Jordan Lee", requestedAt: ago(40), status: "pending" },
        { id: "3", slot: 218, title: "Project Hail Mary", kind: "audiobook", poster: null, seasons: null, requester: "Sam Ortiz", requestedAt: ago(12), status: "pending" },
      ],
      older: [],
      recent: [{ id: "9", slot: 214, title: "Arrival", kind: "movie", poster: null, seasons: null, requester: "Alex Kim", requestedAt: ago(3000), status: "approved", resolvedBy: "you", resolvedAt: ago(2000) }],
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
        { ratingKey: "1", title: "Arrival", type: "movie", daysLeft: 5, reason: "added", lastActivity: ago(60 * 24 * 85) },
        { ratingKey: "2", title: "FROM", type: "show", daysLeft: 6, reason: "last watched", lastActivity: ago(60 * 24 * 84) },
      ],
      upcoming: [{ ratingKey: "3", title: "Severance", type: "show", daysLeft: 41, reason: "last watched", lastActivity: ago(60 * 24 * 49) }],
      exempt: [{ ratingKey: "4", title: "The Office", type: "show" }],
    },
    health: [
      { name: "Plex", ok: true, ms: 12 }, { name: "Seerr", ok: true, ms: 48 }, { name: "Sonarr", ok: true, ms: 30 },
      { name: "Radarr", ok: true, ms: 26 }, { name: "Tautulli", ok: false, ms: 6000, detail: "TimeoutError" }, { name: "SABnzbd", ok: true, ms: 18 },
    ],
  };
  data.invites = invites.map((i) => ({ ...i }));
  data.help = helps.map((h) => ({ ...h }));
  data.messages = Object.entries(conversations).map(([id, list]) => {
    const last = list[list.length - 1];
    return { id, name: names[id], count: list.length, failed: list.filter((m) => !m.delivered).length,
      via: [...new Set(list.map((m) => m.channel))], last: { at: last.at, text: last.title ?? last.text, channel: last.channel, delivered: last.delivered } };
  }).sort((a, b) => b.last.at.localeCompare(a.last.at));
  data.discord = {
    channels: [{ id: "10", name: "general" }, { id: "11", name: "plex-updates" }, { id: "12", name: "movie-night" }],
    party: { channel: "Watch Party", streamer: "Alex Kim", title: "Dune: Part Two", startedAt: ago(42), people: ["Alex Kim", "Jordan Lee", "Priya"] },
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
const msg = (minutes: number, channel: "discord" | "web" | "email" | "none", text: string, title: string | null = null, delivered = true, error: string | null = null) =>
  ({ id: `m${minutes}${channel}`, at: ago(minutes), channel, delivered, title, text, context: "", error });
const conversations: Record<string, ReturnType<typeof msg>[]> = {
  d302: [
    msg(60 * 24 * 3, "discord", "Your request has been approved! A Plex invitation has been sent to alex@example.com.", "Request Approved!"),
    msg(60 * 5, "discord", "Good news! Dune: Part Two is now available on Plex."),
    msg(42, "discord", "Good news! Severance is now ready to start on Plex. Season 2 episode 1 is available."),
  ],
  d301: [
    msg(60 * 24, "discord", "Hey there! We noticed you haven't watched anything on the Plex server in 25 days.\nWhat happens next?: If you remain inactive for 5 more days you'll be removed.", "Plex Inactivity Warning"),
    msg(60 * 2, "discord", "Sorry, your request for Blue Eye Samurai was declined.", null, false, "Their Discord DMs are closed"),
  ],
  pgrandpa_j: [
    msg(60 * 30, "web", "Season 3 is on its way. You'll hear again when it's ready.", "Request approved: The Bear"),
    msg(60 * 8, "none", "You haven't watched anything on the household Plex in 25 days.", "Watch something to keep your Plex access", false, "No phone alerts turned on and no email to send to"),
  ],
};
export const conversation = (who: string) => wait(conversations[who] ?? []);

/** Help requests made in this dev session. */
const helps: import("./types").AdminHelp[] = [
  { id: "h1", request: "6001", slot: 212, title: "The Simpsons", kind: "tv", seasons: [2], who: "Jordan Lee", reason: "Stuck downloading",
    note: "It's been at 0% since this morning.", status_then: "Downloading, 0%", status: "open", created_at: ago(35) },
  { id: "h2", request: "6002", slot: 214, title: "Arrival", kind: "movie", seasons: null, who: "Alex Kim", reason: "Can't be found", offer: "name",
    note: "Searching by its IDs found nothing Plexbie could grab for Arrival: 212 releases came back. Search by name instead?",
    status_then: "Nothing found", status: "open", created_at: ago(12) },
  { id: "h0", request: "6000", slot: 205, title: "Arrival", kind: "movie", seasons: null, who: "Alex Kim", reason: "Wrong version or quality",
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
