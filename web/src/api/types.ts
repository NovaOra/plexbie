// Shapes shared with the bot's /api routes. The bot is the source of truth;
// the web app only renders what it is given.

export type MediaKind = "movie" | "tv" | "audiobook" | "ebook";
export type BookFormat = "ebook" | "audiobook" | "both";

export interface Session {
  user: { id: string; name: string; avatar: string | null; via?: "discord" | "plex" };
  /** Holds the Plex-member Discord role, so may request. */
  member: boolean;
  /** Has asked to join and is waiting on an admin. */
  joinPending: boolean;
  /** The read-only review build: real data, nothing can be changed. */
  preview?: boolean;
  /** Holds the admin role (or owns the server): may use the Manage page. */
  admin?: boolean;
  discordId?: string | null;
  plexName?: string | null;
  plexAccountId?: string | null;
  /** Plex sign-in only: where a join invite would be sent. */
  email?: string | null;
  /** Discord sign-in only: whether they're in the Discord server at all. */
  inGuild?: boolean | null;
  /** plex.tv couldn't be reached, so access couldn't be checked just now. */
  accessUnknown?: boolean;
}

export interface Library {
  title: string;
  kind: "movie" | "show" | "artist" | "book";
  count: number;
}

export interface ServerStatus {
  online: boolean;
  streams: number;
  libraries: Library[];
  checkedAt: string;
}

/** A title as found by search, before or after it has been requested. */
export type SeasonStatus = "available" | "partial" | "requested" | "upcoming" | "none";
export interface SeasonInfo { n: number; episodes: number; have?: number; status?: SeasonStatus }

export interface Title {
  kind: MediaKind;
  /** TMDB id for video, Open Library work key for books. */
  id: string;
  title: string;
  year: string;
  poster: string | null;
  backdrop?: string | null;
  overview?: string;
  genres?: string[];
  runtime?: number | null;
  author?: string;
  /** Each season and where it stands; only "none" and "partial" can still be requested. */
  seasons?: SeasonInfo[];
  /** Already on Plex, already requested by someone, or neither. */
  availability: "available" | "requested" | "none" | "blocked";
  /** Plex's own page for it (app.plex.tv) when Plex has it. */
  plexUrl?: string | null;
  /** Set when the signed-in member asked for it. */
  yourRequest?: { slot: number; stage: RequestStage };
  /** media_cleanup's schedule for this title: days until removal, or exempt. */
  leaving?: Leaving | null;
}

export interface Leaving {
  daysLeft?: number;
  /** Inside the final-warning window. */
  warning?: boolean;
  /** What started the clock: last watched, added (never watched) or requested. */
  reason?: "watched" | "added" | "requested";
  exempt?: boolean;
  /** Cleanup runs in dry-run mode, so nothing is actually removed. */
  practice?: boolean;
}

export type RequestStage =
  | "requested" | "approved" | "upcoming" | "searching" | "downloading" | "unpacking" | "importing" | "available" | "declined" | "closed";

/** Live detail from Radarr, Sonarr or SABnzbd once a request is approved. */
export interface RequestProgress {
  percent?: number | null;
  eta?: string | null;
  detail?: string | null;
  problem?: string | null;
  partial?: boolean;
  seasons?: { n: number; have: number; total: number }[];
  /** "upcoming": when it comes out (ISO date) and how: digital, disc, cinemas, premiere or unannounced. */
  releaseDate?: string | null;
  releaseKind?: string | null;
}

export interface MediaRequest {
  /** The request's own key (its admin card in Discord); used to ask for help on it. */
  id?: string;
  /** The slot number members can refer to; stable and sequential. */
  slot: number;
  /** The open ticket on this one, as its member sees it (what they said and what was said to them). */
  help?: MemberTicket | null;
  title: Title;
  stage: RequestStage;
  requestedAt: string;
  updatedAt: string;
  seasons?: number[] | "all" | "latest";
  format?: BookFormat;
  /** Free text the admin left when declining or approving. */
  note?: string;
  progress?: RequestProgress | null;
}

export interface OnAir {
  member: string;
  title: string;
  subtitle?: string;
  poster: string | null;
  progress: number;
  device: string;
}

export interface Standing {
  name: string;
  hours: number;
  streak: number;
}

export interface Community {
  onAir: OnAir[];
  leaderboard: Standing[];
  you: {
    rank: number | null;
    hours: number;
    streak: number;
    longestStreak: number;
    daysIdle: number;
    removalAfterDays: number;
    topThree: boolean;
    watchPartyMinutes: number;
  };
}

export type LibraryKind = "movie" | "tv" | "book";
export type LibraryItem = Title & { addedAt: string };

export interface Arrival {
  title: Title;
  addedAt: string;
  detail?: string;
}

/* ------------------------------------------------------------ admin views */

export interface AdminRequest {
  id: string;
  slot: number;
  title: string;
  kind: string;
  poster: string | null;
  seasons: number[] | "all" | "latest" | null;
  requester: string;
  requestedAt: string;
  status: "pending" | "approved" | "declined";
  resolvedBy?: string | null;
  resolvedAt?: string | null;
  /** The approval message in Discord, where the buttons are the real answer. */
  discordUrl?: string | null;
}
export interface AdminRequests { pending: AdminRequest[]; older: AdminRequest[]; recent: AdminRequest[] }

export interface AdminJoin { key: string; messageId: string; name: string; via: "discord" | "plex"; email: string; status: string; askedAt: string }

export interface AdminPerson {
  plexName: string;
  discordName: string | null;
  discordId?: string | null;
  linked: boolean;
  lastWatched: string | null;
  daysIdle: number;
  warned: boolean;
  topThree: boolean;
  removalIn: number | null;
  warnAfter: number;
  /** False when the inactivity check doesn't follow them (on Plex, not in the bot's table). */
  tracked?: boolean;
  /** False when they're tracked but no longer shared on Plex; null when plex.tv couldn't be asked. */
  hasAccess?: boolean | null;
  /** The server owner: never removable. */
  owner?: boolean;
  /** The name an admin gave them (shown instead of the Plex username). */
  displayName?: string | null;
  /** An admin's "Never remove": tracked, never warned or removed for inactivity. */
  neverRemove?: boolean;
  /** For someone "no longer on Plex": shared Plex accounts nobody is tracked as, to say which one they are. */
  candidates?: string[];
}

export interface AdminCleanupRow { ratingKey: string; title: string; type: string; daysLeft: number; reason: string; lastActivity?: string }
export interface CleanupSettings {
  enabled: boolean; practice: boolean; inactivityDays: number; warnDaysBefore: number; excludedLibraries: string[];
  channelId?: string | null;
}
export interface AdminCleanup {
  settings: CleanupSettings;
  /** Plex library names, and the Discord channels the bot can post cleanup notices in. */
  libraries?: string[];
  channels?: { id: string; name: string }[];
  warning: AdminCleanupRow[];
  upcoming: AdminCleanupRow[];
  exempt: { ratingKey: string; title: string; type?: string | null; year?: number | null }[];
}
/** A film or show the "Keep a title forever" search found on Plex (never one in a library cleanup skips). */
export interface CleanupMatch { ratingKey: string; title: string; type: string; year?: number | null; kept: boolean }

/** A shelf of titles to browse (Trending, Popular, Coming soon, Top rated, or a genre), from Seerr. */
export interface DiscoverShelf { key: string; title: string; titles: Title[]; more: boolean }
export interface Discover {
  kind: "movie" | "tv"; shelves: DiscoverShelf[]; genres: { id: number; name: string }[];
  /** This member's choice (none: everything, the global top), and what they can pick from. */
  languages: string[]; languageOptions: { code: string; name: string }[];
}
export interface ShelfPage { titles: Title[]; more: boolean; page: number }

/** The Android app's latest version (GET /api/app/latest), or null before the first release. */
/** The app's newest release: the Android APK, and the same version for iPhones
 *  (installed through SideStore or AltStore) when the release has one. */
export interface AppRelease {
  version: string; versionCode: number; sha256: string; size: number; notes: string; publishedAt?: string | null;
  ios?: { size: number } | null;
}
/** A member's own SideStore/AltStore source: the address, and links that add it. */
export interface IosSource { url: string; sidestore: string; altstore: string }

export interface HealthCheck { name: string; ok: boolean; ms: number; detail?: string | null }

/** Manage → Health → "Sign out every other session": every website and app sign-in but
 *  the one that pressed it ended; `ended` counts the app ones. */
export interface SignedOutOthers { ok: boolean; message: string; ended: number }

/* ----------------------------------------------------------- invite links */

/** What someone holding an invite link sees before signing in. */
export interface InviteInfo {
  valid: boolean; label?: string; inviter?: string; emailLocked?: boolean; expiresAt?: string;
  /** Days without watching before an account is removed, and when the warning goes out. */
  inactivityDays?: number; warnDays?: number;
}

export type InviteStatus = "active" | "used" | "expired" | "revoked";
/** An invite on plex.tv nobody has accepted yet. */
export interface PlexInvite {
  email: string;
  name: string;
  sentAt: string;
  who: string | null;
}

export interface AdminInvite {
  id: string;
  label: string;
  email: string | null;
  createdBy: string | null;
  createdAt: string;
  expiresAt: string;
  status: InviteStatus;
  usedBy: string | null;
  usedAt: string | null;
}
/** The link exists in readable form only in this response. */
export interface NewInvite { url: string; invite: AdminInvite }

/* ---------------------------------------------------------- discord tools */

export interface DiscordJoin { who: string; by: string; via: "discord" | "plexbie"; code: string | null; at: string | null; role: string | null }
export interface WatchPartyLive { channel: string | null; streamer: string; title: string | null; startedAt: string | null; people: string[] }
export interface DiscordOverview {
  channels: { id: string; name: string }[]; joins: DiscordJoin[]; party: WatchPartyLive | null;
  /** DMs to Plexbie: whether it answers them itself, and what it lacks to keep a thread per person. */
  inbox?: { autoreply: boolean; threadsMissing: string | null };
}
export interface WatchPartyMine { seconds: number; sessions: number; last: string | null }

/* ------------------------------------------------------------ message log */

export type MessageChannel = "discord" | "web" | "email" | "none";
/** "out": Plexbie to them. "in": them to Plexbie (a DM, "Something wrong?", a ticket answer). */
export type MessageDirection = "out" | "in";
export interface MessagePerson {
  /** count: from Plexbie; received: from them. */
  id: string; name: string; count: number; received?: number; failed: number; via: MessageChannel[];
  last: { at: string; text: string; channel: MessageChannel; delivered: boolean; direction?: MessageDirection };
  /** Their messages no admin has marked done. */
  unread?: number;
  /** Who marked the conversation done, and when (the history stays). */
  done?: { at: string; by: string | null } | null;
  /** Their open ticket, for "Add to their ticket". */
  ticket?: { id: string; title: string; slot: number } | null;
}
export interface LoggedMessage {
  id: string; at: string; direction?: MessageDirection; channel: MessageChannel; delivered: boolean;
  /** The admin who wrote it (a reply from Manage or Discord). */
  by?: string | null;
  /** Something they sent that an admin put on their ticket. */
  ticket?: string | null;
  title: string | null; text: string; context: string; error: string | null;
}

/* ------------------------------------------------------------- tickets */

/** One line on a ticket's timeline. "note" is admins only; "reply" was sent to the member. */
export interface TicketEntry {
  id: string; at: string; by: string;
  kind: "member" | "note" | "reply" | "action" | "status";
  text: string;
  /** A reply that reached nobody (admins only). */
  missed?: boolean;
}
export interface MemberTicket {
  id: string; reason: string;
  status?: "open" | "resolved";
  /** An admin asked something and waits on the member's answer. */
  waiting?: boolean;
  thread?: TicketEntry[];
}
/** A ticket on Manage → Tickets. */
export interface AdminTicketRow {
  id: string; requestKey: string; slot: number; title: string; kind: string; seasons: number[] | "all" | null;
  who: string; reason: string; status: "open" | "resolved"; waiting: boolean; owner?: string | null;
  openedBy?: string | null; offer?: "name" | null; createdAt: string; updatedAt: string;
  last?: { by: string; kind: TicketEntry["kind"]; text: string } | null; count: number;
}
export interface AdminTickets {
  rows: AdminTicketRow[];
  /** Needing an admin, waiting on the member, and the last 50 solved. */
  counts: { action: number; waiting: number; solved: number };
}
export interface AdminTicketDetail extends AdminTicketRow {
  note?: string | null; statusThen?: string | null; quiet?: boolean;
  thread: TicketEntry[];
  /** Its request, as Manage → All requests shows it (null if the request is gone). */
  request: AdminRequestRow | null;
  /** A download Sonarr/Radarr won't import by themselves, on this ticket. */
  blocked?: BlockedRef | null;
}

/* ---------------------------------------------- blocked imports (Manage) */

/** A finished download Sonarr or Radarr won't import by themselves (core/blocked_imports). */
export interface BlockedRef { app: "sonarr" | "radarr"; downloadId: string }
export interface BlockedRow extends BlockedRef {
  title: string; year?: number | null; release: string; messages: string[]; episodes: string[];
  /** Its ticket, when it's on a request. */
  ticket?: string | null;
}
/** An episode in Sonarr, to say which one a file is. */
export interface ArrEpisode { id: number; label: string; season: number; episode: number; title: string; hasFile: boolean }
/** A show (Sonarr) or film (Radarr) in the library. */
export interface ArrItem { id: number; title: string; year?: number | null }
export interface BlockedFile {
  name: string; size: number;
  /** What Sonarr/Radarr take it to be ("S01E01", or the film's title). */
  as: string[];
  quality?: string | null; qualityId?: number | null;
  languages: { id: number; name: string }[];
  releaseGroup: string;
  /** Sonarr's/Radarr's own reasons for not importing it, word for word. */
  rejections: string[];
  /** Those reasons and what Plexbie noticed (samples, tiny files, odd extensions). */
  notes: string[];
  episodes: ArrEpisode[]; seriesId?: number | null;
  movie?: ArrItem | null;
  /** Placed: it has an episode (or a film). */
  ready: boolean;
}
/** What's in it, and what looks off, for an admin to look at before importing. */
export interface BlockedPreview extends BlockedRef {
  title: string; year?: number | null; release: string; folder: string; messages: string[]; episodes: string[];
  warnings: string[];
  files: BlockedFile[];
  /** Other files in the folder (Plexbie lists them when it can see it); `danger`: a program. */
  others: { name: string; size: number; danger: boolean }[];
  /** Whether it can be imported from Plexbie at all (never with a program in it). */
  ok: boolean;
  series?: ArrItem | null; movie?: ArrItem | null;
  /** What Sonarr's/Radarr's own Manual Import lets you choose. */
  options: { qualities: { id: number; name: string }[]; languages: { id: number; name: string }[]; episodes?: ArrEpisode[] };
}
/** An admin's choices for one file when importing a blocked download. */
export interface BlockedChoice {
  name: string; skip?: boolean; seriesId?: number; episodeIds?: number[]; movieId?: number;
  qualityId?: number; languageIds?: number[]; releaseGroup?: string;
}

/* ------------------------------------------------- all requests (Manage) */

/** A request as an admin sees it on Manage → All requests: the member's view plus who and why. */
export interface AdminRequestRow extends MediaRequest {
  requester: string;
  status: string;
  approvedBy?: string | null;
  approvedAt?: string | null;
  /** When it reached the stage it's at (as far as Plexbie has seen). */
  stageSince?: string | null;
  finishedAt?: string | null;
  /** Why it looks stuck, in words; empty when it doesn't. */
  stuck: string[];
}
export interface AdminAllRequests {
  rows: AdminRequestRow[];
  /** Without a search: on their way, stuck, waiting for a decision, on Plex, declined (or closed). */
  counts: { active: number; stuck: number; finished: number; waiting?: number; declined?: number } | null;
  query: string | null;
  /** Every request since No. 0001, not just the last 30 days. */
  everything?: boolean;
  /** How many requests there have ever been. */
  total?: number;
}
export interface AdminTicket {
  id: string; status: "open" | "resolved"; reason: string; note?: string; who?: string; opened_by?: string | null;
  created_at?: string; resolved_by?: string | null; resolved_at?: string | null; reply?: string | null;
  status_then?: string; actions?: { at: string; by: string; did: string }[] | null;
}
export interface AdminRequestDetail extends AdminRequestRow {
  via: string;
  seerrId?: number | null;
  discordUrl?: string | null;
  tickets: AdminTicket[];
  activity: { at: string; by: string; did: string }[];
}

/* ---------------------------------------------------------- help requests */

export type HelpReason = "stuck" | "notfound" | "quality" | "episodes" | "playback" | "other";
export interface AdminHelp {
  id: string; request: string; slot: number; title: string; kind: string; seasons: number[] | "all" | null;
  who: string; reason: string; note: string; status_then: string; status: "open" | "resolved";
  created_at: string; resolved_by?: string; resolved_at?: string; reply?: string | null;
  actions?: { at: string; by: string; did: string }[];
  /** "name": Plexbie's search by ID found nothing and it asks whether to search by name. */
  offer?: "name";
}
