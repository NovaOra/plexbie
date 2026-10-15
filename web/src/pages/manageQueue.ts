import type {
  AdminAllRequests, AdminCleanup, AdminCleanupRow, AdminInvite, AdminJoin, AdminPerson, AdminRequest, AdminRequestRow, AdminRequests,
  AdminTicketRow, AdminTickets, BlockedRow, DiscordOverview, HealthCheck, MessagePerson, PlexInvite,
} from "../api/types";

/**
 * Manage's queue: everything an admin has to act on, worked out in one place from what
 * the page already loads (the admin sections and the blocked imports). The queue's rows,
 * the monitors' counts and what's hot, and the menu's counts all come from here, so they
 * can't disagree.
 */

/** What Manage loads: one entry per admin section, and the blocked imports. */
export interface DeskData {
  requests?: AdminRequests; all?: AdminAllRequests; tickets?: AdminTickets; joins?: AdminJoin[]; invites?: AdminInvite[];
  plexinvites?: PlexInvite[]; people?: AdminPerson[]; cleanup?: AdminCleanup; discord?: DiscordOverview;
  messages?: MessagePerson[]; health?: HealthCheck[]; blocked?: BlockedRow[];
}

/** The five monitors, in order (keys 1 to 5). */
export type Area = "requests" | "help" | "people" | "library" | "services";
export const AREAS: { id: Area; label: string }[] = [
  { id: "requests", label: "Requests" }, { id: "help", label: "Help" }, { id: "people", label: "People" },
  { id: "library", label: "Library" }, { id: "services", label: "Services" },
];

/** What a row is, which says which menu row counts it. */
export type Kind = "request" | "stuck" | "ticket" | "import" | "chat" | "join" | "warned" | "invite" | "link" | "leaving" | "down";
/** Where it sits: the three groups of Needs you, or the Waiting and Done tabs. */
export type Group = "urgent" | "today" | "soon" | "waiting" | "done";

/** What a row is made from, for its actions and its detail. */
export type Source =
  | { type: "request"; r: AdminRequest }
  | { type: "stuck"; r: AdminRequestRow }
  | { type: "ticket"; t: AdminTicketRow; stuck?: AdminRequestRow; blocked?: BlockedRow; request?: AdminRequestRow }
  | { type: "import"; b: BlockedRow }
  | { type: "chat"; p: MessagePerson }
  | { type: "join"; j: AdminJoin }
  | { type: "person"; p: AdminPerson; chat?: MessagePerson }
  | { type: "plexinvite"; i: PlexInvite }
  | { type: "link"; i: AdminInvite }
  | { type: "leaving"; c: AdminCleanupRow; warn: number }
  | { type: "down"; h: HealthCheck };

export interface Item {
  key: string;
  kind: Kind;
  area: Area;
  group: Group;
  /** The caps line: "Ticket · import blocked". */
  eyebrow: string;
  /** Beside the eyebrow: "8 min", "checked 19:53", "warned". */
  age?: string;
  title: string;
  /** The second part of the title: "S1", "Picture looks wrong". */
  sub?: string;
  meta: string;
  /** On the monitor when it's the area's first row: "Import blocked · 8 min". */
  top: string;
  /** A poster (or a drawn cover) for films, shows and books; otherwise an icon or an initial. */
  art?: { poster: string | null; title: string; kind: string };
  initial?: string;
  /** How long it has waited (ms), counting a stuck request's age. */
  wait: number;
  /** Days until its deadline (Coming up). */
  due?: number;
  src: Source;
}

export interface Monitor {
  area: Area; label: string; count: number; hot: boolean;
  /** The sections behind it have loaded. */
  known: boolean;
  first?: Item;
  makeup: string;
}

export interface Desk {
  needs: Item[]; waiting: Item[]; done: Item[];
  urgent: number;
  monitors: Monitor[];
  /** One line each under the monitors. */
  facts: { party: string | null; cleanup: string | null };
}

const NB = " ";
const DAY = 864e5;
/** Coming up holds what ends within a week: removal, an invite link. */
export const SOON_DAYS = 7;
/** A Plex invite nobody has accepted in two weeks is probably going nowhere. */
export const STALE_INVITE_DAYS = 14;
/** The Done tab: what was decided, solved or marked done in the last two days. */
export const DONE_HOURS = 48;

/** "8 min", "5 h", "2 days": how long, as the queue says it. */
export function span(ms: number) {
  const m = Math.max(0, ms) / 60_000;
  if (m < 120) return `${Math.max(1, Math.round(m))}${NB}min`;
  const h = m / 60;
  if (h < 48) return `${Math.round(h)}${NB}h`;
  return `${Math.round(h / 24)}${NB}days`;
}
export const days = (n: number) => (n === 1 ? `1${NB}day` : `${n}${NB}days`);
const plural = (n: number, one: string, many: string) => `${n}${NB}${n === 1 ? one : many}`;

/** A quote cut to fit a line, ending in a tight "…" (never a space before it). */
export function clamp(text: string, max = 72) {
  const one = text.replace(/\s+/g, " ").trim();
  if (one.length <= max) return one;
  const cut = one.slice(0, max);
  const word = cut.lastIndexOf(" ");
  return `${(word > max * 0.6 ? cut.slice(0, word) : cut).replace(/[\s,.;:·–-]+$/, "")}…`;
}

const KIND: Record<string, string> = { movie: "Film", tv: "TV", audiobook: "Audiobook", ebook: "Ebook" };
/** "Film", "Season 4", "Seasons 1, 2", "Audiobook". */
export function whatLabel(kind: string, seasons?: number[] | "all" | "latest" | null) {
  if (kind === "tv" || seasons) {
    if (seasons === "all") return "All seasons";
    if (seasons === "latest") return "Latest season";
    if (seasons?.length === 1) return `Season ${seasons[0]}`;
    if (seasons?.length) return `Seasons ${seasons.join(", ")}`;
  }
  return KIND[kind] ?? kind;
}
const seasonShort = (s?: number[] | "all" | null) => (Array.isArray(s) && s.length ? s.map((n) => `S${n}`).join(", ") : undefined);
const slot = (n: number) => String(n).padStart(4, "0");
const at = (iso?: string | null) => (iso ? new Date(iso).getTime() : NaN);
/** A member who can't get in at all is the one ticket that can't wait. */
const lockedOut = (reason: string) => /can.t get in/i.test(reason);
/** "riley.plex (Plex sign-in)" is riley.plex. */
const joinName = (j: AdminJoin) => j.name.replace(/\s*\(Plex sign-in\)$/i, "");
const nameOf = (p: AdminPerson) => p.displayName || p.plexName;

/** A stuck request's age: since its stage began, or since its ticket was opened if that's longer. */
function stuckSince(r: AdminRequestRow, now: number, ticket?: AdminTicketRow) {
  const stage = at(r.stageSince ?? r.updatedAt);
  const opened = ticket ? at(ticket.createdAt) : NaN;
  const ages = [stage, opened].filter((x) => !Number.isNaN(x)).map((x) => now - x);
  return ages.length ? Math.max(...ages) : 0;
}

/** What the latest entry on a ticket says, on one line. */
function ticketMeta(t: AdminTicketRow, me: string | null, now: number) {
  const mine = me && t.owner === me ? "Yours · " : "";
  const last = t.last;
  if (last?.kind === "member") {
    const said = last.text.startsWith(`${t.reason}. `) ? last.text.slice(t.reason.length + 2) : last.text;
    return `${mine}“${clamp(said, 60)}”`;
  }
  if (last?.kind === "reply") return `${mine}${last.by} answered`;
  if (last?.kind === "note") return `${mine}${last.by} left a note`;
  return `${mine}${t.who} · opened ${span(now - at(t.createdAt))} ago`;
}

function ticketItem(t: AdminTicketRow, d: DeskData, now: number, me: string | null, group: Group): Item {
  const request = t.requestKey ? d.all?.rows.find((r) => r.id === t.requestKey) : undefined;
  const stuck = request?.stuck.length ? request : undefined;
  const blocked = d.blocked?.find((b) => b.ticket === t.id);
  const opened = now - at(t.createdAt);
  const stuckAge = stuck ? stuckSince(stuck, now, t) : 0;
  const general = t.subject === "general" || !t.title;
  const locked = lockedOut(t.reason);
  const what = blocked ? "import blocked" : locked ? "can’t get in" : stuck ? `request stuck ${span(stuckAge)}` : "";
  const title = general ? (locked ? `${t.who} can’t get in` : t.who) : t.title;
  const sub = blocked ? seasonShort(t.seasons) : locked && general ? undefined : t.reason;
  const art = general ? undefined : { poster: request?.title.poster ?? null, title: t.title, kind: t.kind || "movie" };
  return {
    key: `ticket:${t.id}`, kind: "ticket", area: "help", group,
    eyebrow: what ? `Ticket · ${what}` : "Ticket",
    age: group === "waiting" ? `waiting ${span(now - at(t.updatedAt))}` : span(opened),
    title, sub,
    meta: group === "waiting" ? `Waiting on ${t.who}` : blocked ? `Won’t import · ${t.who}’s request` : ticketMeta(t, me, now),
    top: `${blocked ? "Import blocked" : locked ? "Can’t get in" : t.reason} · ${span(Math.max(opened, stuckAge))}`,
    art, initial: general ? t.who : undefined,
    wait: Math.max(opened, stuckAge),
    src: { type: "ticket", t, stuck, blocked, request },
  };
}

/** Every row, before hiding and counting. */
function rows(d: DeskData, now: number, me: string | null): Item[] {
  const out: Item[] = [];
  const openTickets = (d.tickets?.rows ?? []).filter((t) => t.status === "open");
  const ticketed = new Set<string | undefined>(openTickets.map((t) => t.requestKey ?? undefined).filter(Boolean));

  // Requests: each waiting for a decision, and a stuck one only when no open ticket already carries it.
  for (const r of d.requests?.pending ?? []) {
    const wait = now - at(r.requestedAt);
    out.push({
      key: `request:${r.id}`, kind: "request", area: "requests", group: "today",
      eyebrow: `Request · No.${NB}${slot(r.slot)}`, age: span(wait), title: r.title, sub: whatLabel(r.kind, r.seasons),
      meta: `${r.requester} asked`, top: `${r.requester} · ${span(wait)}`,
      art: { poster: r.poster, title: r.title, kind: r.kind }, wait, src: { type: "request", r },
    });
  }
  // Only once the tickets are in: until then every stuck request would look ticketless.
  for (const r of d.tickets ? d.all?.rows ?? [] : []) {
    if (!r.stuck.length || r.status === "pending" || ticketed.has(r.id)) continue;
    const wait = stuckSince(r, now);
    out.push({
      key: `stuck:${r.id}`, kind: "stuck", area: "requests", group: "today",
      eyebrow: `Request · stuck ${span(wait)}`, age: `No.${NB}${slot(r.slot)}`, title: r.title.title, sub: whatLabel(r.title.kind, r.seasons),
      meta: clamp(r.stuck.join(" · "), 64), top: `Stuck · ${span(wait)}`,
      art: { poster: r.title.poster, title: r.title.title, kind: r.title.kind }, wait, src: { type: "stuck", r },
    });
  }

  // Help: tickets needing an admin, blocked imports without a ticket, and people waiting on an answer.
  for (const t of openTickets) {
    if (t.waiting) continue;
    const blocked = d.blocked?.some((b) => b.ticket === t.id);
    out.push(ticketItem(t, d, now, me, blocked || lockedOut(t.reason) ? "urgent" : "today"));
  }
  for (const b of d.blocked ?? []) {
    if (b.ticket && openTickets.some((t) => t.id === b.ticket)) continue;
    const app = b.app === "sonarr" ? "Sonarr" : "Radarr";
    out.push({
      key: `import:${b.app}:${b.downloadId}`, kind: "import", area: "help", group: "urgent",
      eyebrow: `${app} · import blocked`, title: b.title, sub: b.episodes.length ? b.episodes.join(", ") : b.year ? String(b.year) : undefined,
      meta: clamp(b.messages[0] ?? `${app} won’t import it by itself`, 64), top: "Import blocked",
      art: { poster: null, title: b.title, kind: b.app === "sonarr" ? "tv" : "movie" }, wait: 0, src: { type: "import", b },
    });
  }
  const people = d.people ?? [];
  const personOf = (p: MessagePerson) => people.find((x) => [x.displayName, x.plexName, x.discordName].includes(p.name));
  const nearRemoval = (p: AdminPerson) => !p.owner && p.tracked !== false && !p.neverRemove && p.removalIn !== null && p.removalIn <= SOON_DAYS;
  for (const p of d.messages ?? []) {
    // A conversation marked Done leaves the queue unless something arrives (unread) or fails after the Done.
    const failedSince = !p.last.delivered && p.last.direction !== "in" && (!p.done || at(p.last.at) > at(p.done.at));
    if (!(p.unread ?? 0) && !(failedSince && p.done)) continue;
    // Someone close to removal: their messages are on that row, under People.
    const person = personOf(p);
    if (person && nearRemoval(person)) continue;
    const wait = now - at(p.last.at);
    const n = p.unread ?? 0;
    out.push({
      key: `chat:${p.id}`, kind: "chat", area: "help", group: "today",
      eyebrow: n ? `Messages · ${n}${NB}new` : "Messages · not delivered", age: span(wait), title: p.name,
      meta: p.last.direction === "in" ? `“${clamp(p.last.text, 60)}”` : `Plexbie: ${clamp(p.last.text, 56)}`,
      top: `${n ? plural(n, "new message", "new messages") : "Not delivered"} · ${span(wait)}`, initial: p.name, wait, src: { type: "chat", p },
    });
  }

  // People: asking to join, close to removal, invites going nowhere and links running out.
  for (const j of d.joins ?? []) {
    if (j.status !== "pending") continue;
    const wait = now - at(j.askedAt);
    out.push({
      key: `join:${j.key}`, kind: "join", area: "people", group: "today",
      eyebrow: `Join request · ${j.via === "plex" ? "Plex" : "Discord"}`, age: span(wait), title: joinName(j),
      meta: j.via === "plex" ? "Signed in with Plex" : j.email ? "On Discord, gave an email" : "On Discord",
      top: `Wants to join · ${span(wait)}`, initial: joinName(j), wait, src: { type: "join", j },
    });
  }
  for (const p of people) {
    if (!nearRemoval(p)) continue;
    const chat = (d.messages ?? []).find((m) => personOf(m) === p);
    const said = [chat?.unread ? (chat.unread === 1 ? "New message" : `${chat.unread}${NB}new messages`) : "",
      chat && !chat.last.delivered ? "last message not delivered" : ""].filter(Boolean);
    const left = p.removalIn ?? 0;
    out.push({
      key: `person:${p.plexName}`, kind: "warned", area: "people", group: "soon",
      eyebrow: left ? `People · removal in ${days(left)}` : "People · due for removal", age: p.warned ? "warned" : undefined,
      title: nameOf(p), meta: said.length ? said.join(" · ") : `Nothing watched in ${days(p.daysIdle)}`,
      top: left ? `Removal in ${days(left)}` : "Due for removal", initial: nameOf(p), wait: 0, due: left, src: { type: "person", p, chat },
    });
  }
  const settings = d.cleanup?.settings;
  if (settings?.enabled && !settings.practice) {
    for (const c of d.cleanup?.warning ?? []) {
      const ago = c.lastActivity ? Math.max(0, Math.round((now - at(c.lastActivity)) / DAY)) : null;
      const why = c.reason === "added" ? `Added ${ago === null ? "a while" : days(ago)} ago, never watched`
        : c.reason === "requested" ? `Requested ${ago === null ? "a while" : days(ago)} ago, never watched`
          : `Last watched ${ago === null ? "a while" : days(ago)} ago`;
      out.push({
        key: `leaving:${c.ratingKey}`, kind: "leaving", area: "library", group: "soon",
        eyebrow: `Cleanup · leaving in ${days(c.daysLeft)}`, age: `${settings.warnDaysBefore}-day warning`, title: c.title, meta: why,
        top: `Leaves in ${days(c.daysLeft)}`, art: { poster: null, title: c.title, kind: c.type === "show" ? "tv" : "movie" },
        wait: 0, due: c.daysLeft, src: { type: "leaving", c, warn: settings.warnDaysBefore },
      });
    }
  }
  for (const i of d.invites ?? []) {
    if (i.status !== "active") continue;
    const left = Math.max(0, Math.ceil((at(i.expiresAt) - now) / DAY));
    if (left > SOON_DAYS) continue;
    out.push({
      key: `link:${i.id}`, kind: "link", area: "people", group: "soon",
      eyebrow: left ? `Invite link · ends in ${days(left)}` : "Invite link · ends today", age: "open", title: `${i.label}’s invite link`,
      meta: `By ${i.createdBy ?? "an admin"} · not used yet`, top: left ? `Link ends in ${days(left)}` : "Link ends today",
      wait: 0, due: left, src: { type: "link", i },
    });
  }
  for (const i of d.plexinvites ?? []) {
    const old = now - at(i.sentAt);
    if (old < STALE_INVITE_DAYS * DAY) continue;
    const who = i.who || i.email || i.name || "A Plex invite";
    out.push({
      key: `plex:${i.email || `${i.name}|${i.sentAt}`}`, kind: "invite", area: "people", group: "soon",
      eyebrow: "Plex invite", age: span(old), title: who, meta: `Sent ${span(old)} ago, not accepted`,
      top: `Plex invite · ${span(old)}`, wait: old, src: { type: "plexinvite", i },
    });
  }

  // Services: a check that's failing can't wait.
  for (const h of d.health ?? []) {
    if (h.ok) continue;
    const said = (h.detail ?? "").split(/(?<=[.!?])\s/)[0];
    out.push({
      key: `down:${h.name}`, kind: "down", area: "services", group: "urgent",
      eyebrow: "Services · check failing", title: h.name, meta: said ? clamp(said, 64) : "Not answering",
      top: "Check failing", wait: 0, src: { type: "down", h },
    });
  }
  return out;
}

/** Urgent first (blocked imports, someone locked out, a service down), then Today by the
 *  longest wait, then Coming up by the soonest deadline. */
const URGENT_ORDER: Kind[] = ["import", "ticket", "down"];
function order(items: Item[]) {
  const urgent = items.filter((i) => i.group === "urgent").sort((a, b) =>
    Number(b.src.type === "ticket" && !!b.src.blocked) - Number(a.src.type === "ticket" && !!a.src.blocked)
    || URGENT_ORDER.indexOf(a.kind) - URGENT_ORDER.indexOf(b.kind) || b.wait - a.wait);
  const today = items.filter((i) => i.group === "today").sort((a, b) => b.wait - a.wait);
  const soon = items.filter((i) => i.group === "soon").sort((a, b) => (a.due ?? Infinity) - (b.due ?? Infinity));
  return [...urgent, ...today, ...soon];
}

/** The Done tab: requests decided, tickets solved and conversations marked done in the last two days, newest first. */
function doneRows(d: DeskData, now: number, inQueue: Set<string>): Item[] {
  const recent = (iso?: string | null) => !!iso && Math.round((now - at(iso)) / 36e5) <= DONE_HOURS;
  const out: (Item & { when: number })[] = [];
  for (const r of d.requests?.recent ?? []) {
    if (!recent(r.resolvedAt)) continue;
    const word = r.status === "approved" ? "approved" : "declined";
    out.push({
      key: `done-request:${r.id}`, kind: "request", area: "requests", group: "done",
      eyebrow: `Request · No.${NB}${slot(r.slot)} · ${word}`, age: span(now - at(r.resolvedAt)), title: r.title, sub: whatLabel(r.kind, r.seasons),
      meta: `${r.requester} asked${r.resolvedBy ? ` · ${word} by ${r.resolvedBy}` : ""}`, top: "",
      art: { poster: r.poster, title: r.title, kind: r.kind }, wait: 0, src: { type: "request", r }, when: at(r.resolvedAt),
    });
  }
  for (const t of d.tickets?.rows ?? []) {
    if (t.status === "open" || !recent(t.updatedAt)) continue;
    const by = t.last?.kind === "status" ? t.last.by : t.owner;
    const general = t.subject === "general" || !t.title;
    out.push({
      key: `done-ticket:${t.id}`, kind: "ticket", area: "help", group: "done",
      eyebrow: "Ticket · solved", age: span(now - at(t.updatedAt)), title: general ? t.who : t.title, sub: t.reason,
      meta: by ? `Solved by ${by}` : `${t.who}’s ticket`, top: "",
      art: general ? undefined : { poster: null, title: t.title, kind: t.kind || "movie" }, initial: general ? t.who : undefined,
      wait: 0, src: { type: "ticket", t }, when: at(t.updatedAt),
    });
  }
  for (const p of d.messages ?? []) {
    if (!p.done || !recent(p.done.at) || inQueue.has(`chat:${p.id}`)) continue;
    out.push({
      key: `done-chat:${p.id}`, kind: "chat", area: "help", group: "done",
      eyebrow: "Messages · done", age: span(now - at(p.done.at)), title: p.name, meta: `${p.done.by ?? "An admin"} marked it done`, top: "",
      initial: p.name, wait: 0, src: { type: "chat", p }, when: at(p.done.at),
    });
  }
  return out.sort((a, b) => b.when - a.when).map(({ when: _when, ...i }) => i);
}

const MAKEUP: Record<Area, [Kind[], (n: number) => string][]> = {
  requests: [[["request"], (n) => `${n}${NB}waiting`], [["stuck"], (n) => `${n}${NB}stuck`]],
  help: [[["ticket"], (n) => plural(n, "ticket", "tickets")], [["import"], (n) => plural(n, "import", "imports")], [["chat"], (n) => plural(n, "chat", "chats")]],
  people: [[["join"], (n) => plural(n, "join", "joins")], [["warned"], (n) => `${n}${NB}warned`], [["invite", "link"], (n) => plural(n, "invite", "invites")]],
  library: [],
  services: [],
};
/** Which sections each monitor counts from: until they've loaded it shows a dash, not a false 0. */
const FEEDS: Record<Area, (keyof DeskData)[]> = {
  requests: ["requests", "all", "tickets"], help: ["tickets", "messages"], people: ["joins", "people", "invites", "plexinvites"],
  library: ["cleanup"], services: ["health"],
};

/**
 * The desk at `now`: the queue (Needs you, Waiting, Done), the monitors and the facts line.
 * `me` is the admin looking (their tickets say "Yours"); `hidden` rows are left out of
 * everything, counts included (a decision waiting out its Undo).
 */
export function deriveDesk(d: DeskData, now: number, opts: { me?: string | null; hidden?: ReadonlySet<string> } = {}): Desk {
  const hidden = opts.hidden ?? new Set<string>();
  const me = opts.me ?? null;
  const all = rows(d, now, me).filter((i) => !hidden.has(i.key));
  const needs = order(all);
  const waiting = (d.tickets?.rows ?? []).filter((t) => t.status === "open" && t.waiting)
    .map((t) => ticketItem(t, d, now, me, "waiting")).filter((i) => !hidden.has(i.key))
    .sort((a, b) => b.wait - a.wait);
  const done = doneRows(d, now, new Set(needs.map((i) => i.key)));

  const cleanup = d.cleanup?.settings;
  const warnDays = cleanup?.warnDaysBefore ?? SOON_DAYS;
  const monitors = AREAS.map(({ id, label }): Monitor => {
    const mine = needs.filter((i) => i.area === id);
    const n = mine.length;
    const parts = MAKEUP[id].map(([kinds, say]) => [say, mine.filter((i) => kinds.includes(i.kind)).length] as const)
      .filter(([, k]) => k).map(([say, k]) => say(k));
    const checks = d.health?.length ?? 0;
    const makeup = id === "library" ? (n ? `${n}${NB}leaving in ${days(warnDays)}` : cleanup?.enabled && !cleanup.practice ? "Nothing leaving" : `Cleanup ${cleanup?.enabled ? "in practice" : "off"}`)
      : id === "services" ? (checks ? `${n ? n : checks}${NB}of ${checks}${NB}${n ? "down" : "up"}` : "No checks yet")
        : parts.length ? parts.join(" · ") : "Nothing waiting";
    return { area: id, label, count: n, hot: mine.some((i) => i.group === "urgent"), known: FEEDS[id].every((s) => d[s] !== undefined), first: mine[0], makeup };
  });

  const party = d.discord?.party;
  return {
    needs, waiting, done,
    urgent: needs.filter((i) => i.group === "urgent").length,
    monitors,
    facts: {
      party: party ? [party.streamer, party.title, party.people.length ? `${party.people.length}${NB}watching` : ""].filter(Boolean).join(" · ") : null,
      cleanup: cleanup ? (cleanup.enabled ? `Cleanup ${cleanup.practice ? "in practice" : "live"} · ${cleanup.warnDaysBefore}-day warning` : "Cleanup off") : null,
    },
  };
}
