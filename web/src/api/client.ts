import type { ArrEpisode, ArrItem, BlockedChoice, BlockedPreview, BlockedRow, AppRelease, Arrival, IosSource, BookFormat, CleanupSettings, Community, Discover, HelpReason, InviteInfo, LibraryItem, LibraryKind, LoggedMessage, MediaKind, MediaRequest, NewInvite, ServerStatus, Session, ShelfPage, Title, WatchPartyMine, AdminAllRequests, AdminRequestDetail, AdminTicketDetail } from "./types";
import * as sample from "./sample";
export { DEMO } from "./sample";

/**
 * Sample mode serves invented people and real public artwork, so the design
 * can be reviewed without the bot. It is on in `npm run dev` unless
 * VITE_SAMPLE=0, and in the private review build (`npm run build:preview`,
 * which sets VITE_SAMPLE=1). The production build leaves it out entirely.
 */
export const SAMPLE = (import.meta.env.DEV && import.meta.env.VITE_SAMPLE !== "0") || import.meta.env.VITE_SAMPLE === "1";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    credentials: "same-origin",
    // X-Plexbie marks the request as coming from this site; the server refuses writes without it.
    headers: { "Content-Type": "application/json", "X-Plexbie": "1" },
    ...init,
  });
  if (!res.ok) {
    let message = res.statusText;
    try {
      // Most refusals carry `error`; a refused admin action answers an Ack, whose line is `message`.
      const body = await res.json();
      message = body.error ?? body.message ?? message;
    } catch {
      /* not JSON; keep the status text */
    }
    throw new ApiError(res.status, message);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

/** What the admin actions answer: whether it worked, and a line to show. `told`, on a
 *  reply to a ticket's member, "Solved" or a ticket opened to tell them: whether it
 *  reached them (false: it reached nobody, and `message` says so). Absent when nobody
 *  was to be told. */
export type Ack = { ok: boolean; message: string; told?: boolean };
const post = <T = Ack,>(path: string, body: unknown = {}) => call<T>(path, { method: "POST", body: JSON.stringify(body) });
const enc = encodeURIComponent;

export const api = {
  session: (): Promise<Session | null> =>
    SAMPLE ? sample.session() : call<Session>("/session").catch((e) => {
      if (e instanceof ApiError && e.status === 401) return null;
      throw e;
    }),
  status: (): Promise<ServerStatus> => (SAMPLE ? sample.status() : call("/status")),
  arrivals: (): Promise<Arrival[]> => (SAMPLE ? sample.arrivals() : call("/arrivals")),
  library: (kind: LibraryKind): Promise<LibraryItem[]> =>
    SAMPLE ? sample.library(kind) : call(`/library?${new URLSearchParams({ kind })}`),
  community: (): Promise<Community> => (SAMPLE ? sample.community() : call("/community")),
  myRequests: (): Promise<MediaRequest[]> => (SAMPLE ? sample.myRequests() : call("/requests")),
  appLatest: (): Promise<AppRelease | null> =>
    SAMPLE ? sample.wait({ version: "1.5.0", versionCode: 6, sha256: "0".repeat(64), size: 47_350_955,
      notes: "**New in 1.5.0: update notices**\n- Plexbie tells you when a new version is out, and downloads it.", publishedAt: null,
      ios: { size: 38_200_000 } }, 300)
      : call("/app/latest"),
  /** A download link for the app, good for ten minutes. */
  appDownloadLink: (): Promise<{ url: string; version: string }> =>
    SAMPLE ? sample.wait({ url: "#", version: "1.5.0" }, 300) : post("/app/download-link"),
  /** This member's own SideStore/AltStore source for the iPhone app. */
  appIosSource: (): Promise<IosSource> =>
    SAMPLE ? sample.wait({ url: "https://plexbie.example/app-source/sample.json", sidestore: "#", altstore: "#" }, 300)
      : post("/app/ios-source"),
  search: (q: string, kind: MediaKind): Promise<Title[]> =>
    SAMPLE ? sample.search(q, kind) : call(`/search?${new URLSearchParams({ q, kind })}`),
  title: (kind: MediaKind, id: string): Promise<Title> =>
    SAMPLE ? sample.title(kind, id) : call(`/titles/${kind}/${enc(id)}`),
  request: (body: {
    kind: MediaKind;
    id: string;
    seasons?: number[] | "all" | "latest";
    format?: BookFormat;
  }): Promise<MediaRequest> =>
    SAMPLE ? sample.request(body) : post("/requests", body),
  join: (email: string): Promise<void> =>
    SAMPLE ? sample.join(email) : post("/join", { email }),
  decide: (what: "requests" | "joins", id: string, approve: boolean): Promise<Ack> =>
    SAMPLE ? sample.sampleDecide(what, id, approve) as Promise<Ack> : post(`/admin/${what}/${enc(id)}/${approve ? "approve" : "decline"}`),
  exempt: (ratingKey: string, keep: boolean): Promise<Ack> =>
    SAMPLE ? sample.sampleExempt(ratingKey, keep) as Promise<Ack> : post("/admin/cleanup/exempt", { ratingKey, keep }),
  removePerson: (plexName: string): Promise<Ack> =>
    SAMPLE ? sample.sampleRemove(plexName) as Promise<Ack> : post("/admin/people/remove", { plexName }),
  cleanupSettings: (change: Partial<CleanupSettings>): Promise<Ack> =>
    SAMPLE ? sample.sampleCleanupSettings(change) : post("/admin/cleanup/settings", change),
  cleanupScan: (): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: "Scan done: 2 titles in the warning window, 0 were removed." }, 1500) : post("/admin/cleanup/scan"),
  linkCandidates: (): Promise<{ discord: { id: string; name: string; username: string }[] }> =>
    SAMPLE ? sample.wait({ discord: [{ id: "201", name: "Priya", username: "priya.n" }, { id: "202", name: "Dev", username: "devr" }, { id: "203", name: "Rosa M", username: "rosam" }] }) : call("/admin/links"),
  linkPerson: (plexName: string, discordId: string): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: `Linked ${plexName}.` }) : post("/admin/people/link", { plexName, discordId }),
  renamePerson: (plexName: string, name: string): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: `Shown as ${name} from now on.` }) : post("/admin/people/rename", { plexName, name }),
  keepPerson: (plexName: string, keep: boolean): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: keep ? `${plexName} will never be removed.` : `${plexName} is back on the check.` }) : post("/admin/people/keep", { plexName, keep }),
  matchPerson: (plexName: string, account: string): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: `${plexName} is ${account} on Plex now.` }) : post("/admin/people/match", { plexName, account }),
  unlinkPerson: (plexName: string): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: `Unlinked ${plexName}.` }) : post("/admin/people/unlink", { plexName }),
  say: (channelId: string, message: string, allowMassPings: boolean): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: "Posted in #general." }, 500) : post("/admin/say", { channelId, message, allowMassPings }),
  discover: (kind: "movie" | "tv"): Promise<Discover> => (SAMPLE ? sample.discover(kind) : call(`/discover/${kind}`)),
  shelf: (kind: "movie" | "tv", key: string, page: number): Promise<ShelfPage> =>
    SAMPLE ? sample.wait({ titles: [], more: false, page }, 300) : call(`/discover/${kind}/${enc(key)}?page=${page}`),
  /** Which languages this member's shelves show; none is everything. */
  /** This member's choices (the languages their shelves show) and what they can pick from. */
  prefs: (): Promise<{ languages: string[]; languageOptions: { code: string; name: string }[] }> =>
    SAMPLE ? sample.prefs() : call("/prefs"),
  /** One search box: films, shows and books at once. */
  searchAll: (q: string): Promise<{ movie: Title[]; tv: Title[]; book: Title[] }> =>
    SAMPLE ? sample.searchAll(q) : call(`/search/all?${new URLSearchParams({ q })}`),
  saveLanguages: (languages: string[]): Promise<{ languages: string[] }> =>
    SAMPLE ? sample.saveLanguages(languages) : post("/prefs", { languages }),
  similar: (kind: MediaKind, id: string): Promise<Title[]> =>
    SAMPLE ? sample.similar(kind, id) : call(`/titles/${kind}/${enc(id)}/similar`),
  popular: (): Promise<{ movies: Title[]; tv: Title[] }> =>
    SAMPLE ? sample.popular() : call("/popular"),
  watchparty: (): Promise<WatchPartyMine> =>
    SAMPLE ? sample.wait({ seconds: 5 * 3600 + 1200, sessions: 7, last: new Date(Date.now() - 3 * 864e5).toISOString() }) : call("/watchparty"),
  invite: (): Promise<InviteInfo> => (SAMPLE ? sample.inviteInfo() : call("/invite")),
  createInvite: (body: { label: string; email?: string; days: number }): Promise<NewInvite> =>
    SAMPLE ? sample.createInvite(body) : post("/admin/invites", body),
  deleteInvite: (id: string): Promise<Ack> =>
    SAMPLE ? sample.deleteInvite(id) : post(`/admin/invites/${enc(id)}/delete`),
  renewInvite: (id: string): Promise<NewInvite> =>
    SAMPLE ? sample.renewInvite(id) : post(`/admin/invites/${enc(id)}/renew`),
  revokeInvite: (id: string): Promise<Ack> =>
    SAMPLE ? sample.revokeInvite(id) : post(`/admin/invites/${enc(id)}/revoke`),
  askHelp: (requestId: string, reason: HelpReason, note: string): Promise<Ack & { help: { id: string; reason: string } }> =>
    SAMPLE ? sample.askHelp(requestId, reason) : post(`/requests/${enc(requestId)}/help`, { reason, note }),
  helpSearch: (id: string): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: "Sonarr is searching for season 2 again." }, 600) : post(`/admin/help/${enc(id)}/search`),
  helpByName: (id: string): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: "Plexbie is searching NZBHydra for \u201cArrival 2016\u201d. It reports back here, and closes this if it finds it." }, 600) : post(`/admin/help/${enc(id)}/name`),
  helpEpisodes: (id: string): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: "Sonarr is searching season 2 one episode at a time." }, 600) : post(`/admin/help/${enc(id)}/episodes`),
  helpResolve: (id: string, reply: string): Promise<Ack> =>
    SAMPLE ? sample.helpResolve(id) : post(`/admin/help/${enc(id)}/resolve`, { reply }),
  conversation: (who: string): Promise<LoggedMessage[]> =>
    SAMPLE ? sample.conversation(who) : call(`/admin/messages/${enc(who)}`),
  /** Answer someone as Plexbie, signed with your name. */
  messageReply: (who: string, text: string): Promise<Ack> =>
    SAMPLE ? sample.messageReply(who, text) : post(`/admin/messages/${enc(who)}/reply`, { text }),
  messageDone: (who: string, done: boolean): Promise<Ack> =>
    SAMPLE ? sample.messageDone(who, done) : post(`/admin/messages/${enc(who)}/done`, { done }),
  /** Put something they sent Plexbie on their open ticket. */
  messageToTicket: (key: string): Promise<Ack> =>
    SAMPLE ? sample.messageToTicket(key) : post(`/admin/message/${enc(key)}/to-ticket`, {}),
  inboxSettings: (autoreply: boolean): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: autoreply ? "Plexbie answers new DMs." : "Plexbie won't answer DMs by itself." }, 400)
      : post("/admin/inbox", { autoreply }),
  plexInviteCancel: (email: string): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: `Plex invite to ${email} cancelled.` }, 500) : post("/admin/plex-invites/cancel", { email }),
  plexInviteChange: (email: string, next: string): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: `Invite sent to ${next}.` }, 700) : post("/admin/plex-invites/change", { email, new: next }),
  /** With `everything`, every request since No. 0001 rather than the last 30 days. */
  adminAll: (q: string, everything = false): Promise<AdminAllRequests> =>
    SAMPLE ? sample.adminAll(q, everything) : call(`/admin/all?q=${enc(q)}${everything ? "&all=1" : ""}`),
  adminRequest: (key: string): Promise<AdminRequestDetail> =>
    SAMPLE ? sample.adminRequest(key) : call(`/admin/request/${enc(key)}`),
  requestTicket: (key: string, note: string, tell: boolean, message = ""): Promise<Ack & { help: { id: string; reason: string } }> =>
    SAMPLE ? sample.requestTicket(key, note, tell, message) : post(`/admin/request/${enc(key)}/ticket`, { note, tell, message }),
  /** `errors` names Sonarr or Radarr when it couldn't be asked (older bots leave it out). */
  adminBlocked: (): Promise<{ rows: BlockedRow[]; errors?: Partial<Record<BlockedRow["app"], string>> }> =>
    SAMPLE ? sample.adminBlocked() : call("/admin/blocked"),
  blockedPreview: (app: string, downloadId: string): Promise<BlockedPreview> =>
    SAMPLE ? sample.blockedPreview() : call(`/admin/blocked/${enc(app)}/${enc(downloadId)}`),
  blockedImport: (app: string, downloadId: string, files?: BlockedChoice[]): Promise<Ack> =>
    SAMPLE ? sample.wait({ ok: true, message: "Imported 3 files." }, 900) : post(`/admin/blocked/${enc(app)}/${enc(downloadId)}/import`, files ? { files } : {}),
  /** Shows (Sonarr) or films (Radarr) in the library, for "Wrong show?". */
  arrLibrary: (app: string, q: string): Promise<{ rows: ArrItem[] }> =>
    SAMPLE ? sample.wait({ rows: [{ id: 41, title: "Radar Men from the Moon", year: 1952 }, { id: 42, title: "King of the Rocket Men", year: 1949 }] })
      : call(`/admin/arr/${enc(app)}/library?q=${enc(q)}`),
  arrEpisodes: (seriesId: number): Promise<{ rows: ArrEpisode[] }> =>
    SAMPLE ? sample.wait({ rows: sample.sampleEpisodes }) : call(`/admin/arr/sonarr/series/${seriesId}/episodes`),
  adminTicket: (id: string): Promise<AdminTicketDetail> =>
    SAMPLE ? sample.adminTicket(id) : call(`/admin/ticket/${enc(id)}`),
  /** "note": admins only; "reply": sent to the member the usual way. */
  ticketComment: (id: string, kind: "note" | "reply", text: string): Promise<Ack> =>
    SAMPLE ? sample.ticketComment(id, kind, text) : post(`/admin/ticket/${enc(id)}/comment`, { kind, text }),
  ticketStatus: (id: string, status: "open" | "waiting" | "resolved", message = ""): Promise<Ack> =>
    SAMPLE ? sample.ticketStatus(id, status, message) : post(`/admin/ticket/${enc(id)}/status`, { status, message }),
  ticketTake: (id: string): Promise<Ack> =>
    SAMPLE ? sample.ticketTake(id) : post(`/admin/ticket/${enc(id)}/take`),
  /** The member answers on the ticket on their own request. */
  answerTicket: (requestId: string, text: string): Promise<Ack> =>
    SAMPLE ? sample.answerTicket(requestId, text) : post(`/requests/${enc(requestId)}/help/reply`, { text }),
  requestSearch: (key: string, how: "again" | "episodes" | "name"): Promise<Ack> =>
    SAMPLE ? sample.requestSearch(key, how) : post(`/admin/request/${enc(key)}/search/${how}`),
  admin: <T,>(section: "requests" | "all" | "tickets" | "joins" | "people" | "cleanup" | "health" | "invites" | "plexinvites" | "discord" | "messages" | "help"): Promise<T> =>
    SAMPLE ? (sample.admin(section) as Promise<T>) : call<T>(`/admin/${section}`),
  logout: (): Promise<void> => (SAMPLE ? sample.logout() : call("/logout", { method: "POST" })),
};

export const loginUrl = (next = "/", via: "discord" | "plex" = "discord") =>
  SAMPLE ? `/?as=member` : `/auth/${via}/login?${new URLSearchParams({ next })}`;

/**
 * "Sign in with Plex" in a small window, watched from here. Plex's own redirect
 * back isn't relied on: on phones the plex.tv link often opens the Plex app, which
 * never returns. Approving there still counts; polling notices and signs in.
 * Use as an onClick on a link to loginUrl(…, "plex"), which stays the fallback.
 */
export function plexSignIn(e: { preventDefault(): void }, next = "/", invite = false, onStatus?: (text: string) => void) {
  if (SAMPLE) return;
  const q = new URLSearchParams({ next });
  if (invite) q.set("invite", "1");
  // Our own page first: it makes the plex.tv PIN from this browser (Plex refuses
  // one made elsewhere) and moves on to plex.tv by itself, after the tap stops
  // counting, so a phone keeps it in the browser instead of opening the Plex app.
  const win = window.open(`/auth/plex/go?${q}`, "plexbie-plex", "width=520,height=720");
  if (!win) return; // blocked: let the link do a whole-page sign-in instead
  e.preventDefault();
  onStatus?.("Waiting for Plex…");
  (async () => {
    try {
      for (let i = 0; i < 240; i++) {
        await new Promise((ok) => setTimeout(ok, 1500));
        const c = await fetch("/auth/plex/check", { credentials: "same-origin", cache: "no-store" });
        const got = await c.json().catch(() => ({}));
        if (got.done) {
          try { win.close(); } catch { /* already closed */ }
          window.location.assign(got.next || next);
          return;
        }
        if (!c.ok && i > 3) throw new Error(got.error || "That sign-in didn’t work.");
      }
      throw new Error("That took too long. Press Sign in with Plex again.");
    } catch (err) {
      onStatus?.(err instanceof Error ? err.message : "That sign-in didn’t work.");
    }
  })();
}

/** TMDB paths, Open Library cover ids ("ol:123") or absolute URLs. */
export function artUrl(path: string | null | undefined, size: "w185" | "w342" | "w780" | "w1280" = "w342") {
  if (!path) return null;
  if (path.startsWith("/img/")) return path;
  if (path.startsWith("ol:")) {
    const olSize = size === "w185" ? "M" : "L";
    return `https://covers.openlibrary.org/b/id/${path.slice(3)}-${olSize}.jpg`;
  }
  if (path.startsWith("http")) return path;
  return `https://image.tmdb.org/t/p/${size}${path}`;
}
