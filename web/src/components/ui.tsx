import { useEffect, useRef, useState, type ReactNode } from "react";
import { Archive, PackageOpen, CalendarClock, Check, CircleDot, Clock3, Download, FolderInput, Headphones, BookOpen, Radio as RadioTower, RefreshCw, X } from "./icons";
import type { Icon as IconType } from "./icons";
import { artUrl, DEMO, SAMPLE } from "../api/client";
import { Mascot } from "./Mascot";
import type { MediaKind, RequestStage, Title } from "../api/types";

/* ------------------------------------------------------------ data loading */

export function useLoad<T>(load: () => Promise<T>, deps: unknown[] = [], refreshMs?: (data: T | undefined) => number | null) {
  const [state, setState] = useState<{ data?: T; error?: Error; loading: boolean }>({ loading: true });
  const tick = useRef(0);
  const [round, setRound] = useState(0);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const id = ++tick.current;
    if (round === 0) setState((s) => ({ ...s, loading: true, error: undefined }));
    load().then(
      (data) => id === tick.current && setState({ data, loading: false }),
      (error) => id === tick.current && setState((s) => (round === 0 ? { error, loading: false } : { ...s, loading: false })),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, round, attempt]);
  // Quiet background refresh while the page is visible (live request progress).
  const every = refreshMs ? refreshMs(state.data) : null;
  useEffect(() => {
    if (!every) return;
    const t = setInterval(() => { if (!document.hidden) setRound((r) => r + 1); }, every);
    return () => clearInterval(t);
  }, [every]);
  /** Try again after a failure: loads afresh, showing the loading state. */
  const reload = () => { setRound(0); setAttempt((a) => a + 1); };
  return { ...state, reload };
}

/** What `useLoad` gives back, for a page that loads something once and hands it to its parts. */
export type Loaded<T> = ReturnType<typeof useLoad<T>>;

/** A load failed: say so (never "nothing here", which would be a false answer), and offer another go. */
export function OffAir({ children, onRetry, compact = false }: { children?: ReactNode; onRetry?: () => void; compact?: boolean }) {
  return (
    <div className={`off-air${compact ? " off-air--compact" : ""}`} role="alert">
      <div className="strap strap--closed">
        <span className="strap__tab"><RadioTower size={14} aria-hidden />No signal</span>
        <span className="strap__bar"><span>{children ?? "Couldn’t reach Plexbie just now. Nothing is lost."}</span></span>
      </div>
      {onRetry ? (
        <button type="button" className="btn off-air__retry" onClick={onRetry}>
          <RefreshCw size={16} aria-hidden /> Try again
        </button>
      ) : null}
    </div>
  );
}

/** Request rows while they load: the same shape, so nothing jumps when they arrive. */
export function SlotSkeletons({ count = 3 }: { count?: number }) {
  return (
    <div className="schedule" aria-busy="true" aria-label="Loading requests">
      {Array.from({ length: count }, (_, i) => (
        <div className="slot slot--skeleton" key={i} aria-hidden>
          <span className="skeleton" style={{ width: 34, height: 34 }} />
          <span className="skeleton slot__poster" />
          <span className="slot__body">
            <span className="skeleton" style={{ height: 18, width: "60%" }} />
            <span className="skeleton" style={{ height: 12, width: "85%" }} />
            <span className="skeleton" style={{ height: 12, width: "40%" }} />
          </span>
        </div>
      ))}
    </div>
  );
}

/** The demo recordings run the request journey in seconds, so pages refresh faster there. */
const DEMO_FAST = SAMPLE && DEMO;

/** Refresh while any request is still moving: every 5s mid-download, so the bar tracks SABnzbd; 15s otherwise. */
export const whileActive = (list: { stage: RequestStage }[] | undefined) =>
  list?.some((r) => ["downloading", "unpacking", "importing"].includes(r.stage)) ? (DEMO_FAST ? 700 : 5_000)
    : list?.some((r) => ["requested", "approved", "searching"].includes(r.stage)) && DEMO_FAST ? 700
    : list?.some((r) => ["approved", "searching"].includes(r.stage)) ? 15_000 : null;

/* ----------------------------------------------------------------- titles */

const SITE_TITLE = "Plexbie: a Discord bot for your household’s Plex server";

/** The tab's title: "Library · Plexbie", or the full name with none. Never a person's
 *  name on public pages (link previews read it). */
export function useTitle(title?: string | null) {
  useEffect(() => {
    document.title = title ? `${title} · Plexbie` : SITE_TITLE;
  }, [title]);
}

/* ------------------------------------------------------------------ words */

const STAGE_LABEL: Record<RequestStage, string> = {
  requested: "Requested",
  approved: "Approved",
  upcoming: "Upcoming",
  searching: "Searching",
  downloading: "Downloading",
  unpacking: "Unpacking",
  importing: "Adding to Plex",
  available: "On Plex",
  declined: "Declined",
  closed: "Closed",
};

const STAGE_HELP: Record<RequestStage, string> = {
  requested: "Waiting for an admin to approve it",
  approved: "Approved, looking for a copy",
  upcoming: "Approved. It isn’t out yet",
  searching: "Approved, looking for a copy",
  downloading: "On its way to Plex",
  unpacking: "Downloaded, SABnzbd is unpacking it",
  importing: "Downloaded, waiting for Plex to add it",
  available: "Ready to watch",
  declined: "Not added",
  closed: "Closed with the old backlog",
};

/** Books live on Audiobookshelf, everything else on Plex. */
export const isBook = (kind?: string | null) => kind === "audiobook" || kind === "ebook";
export const homeOf = (kind?: string | null) => (isBook(kind) ? "Audiobookshelf" : "Plex");

/** The stage's name, saying where the title ends up for its kind. */
export const stageLabel = (s: RequestStage, kind?: string | null) =>
  s === "available" ? `On ${homeOf(kind)}` : s === "importing" ? `Adding to ${homeOf(kind)}` : STAGE_LABEL[s];

/** One line on what the stage means, in words that fit a film, a show or a book. */
export function stageHelp(s: RequestStage, kind?: string | null, format?: string | null) {
  if (isBook(kind)) {
    if (s === "available") return format === "both" ? "Ready to read or listen" : kind === "audiobook" ? "Ready to listen" : "Ready to read";
    if (s === "downloading") return "On its way to Audiobookshelf";
    if (s === "importing") return "Downloaded, being added to Audiobookshelf";
  }
  return STAGE_HELP[s];
}

const STAGE_ICON: Record<RequestStage, ReactNode> = {
  requested: <Clock3 aria-hidden />,
  approved: <Check aria-hidden />,
  upcoming: <CalendarClock aria-hidden />,
  searching: <Check aria-hidden />,
  downloading: <Download aria-hidden />,
  unpacking: <PackageOpen aria-hidden />,
  importing: <FolderInput aria-hidden />,
  available: <CircleDot aria-hidden />,
  declined: <X aria-hidden />,
  closed: <Archive aria-hidden />,
};

export const KIND_LABEL: Record<MediaKind, string> = {
  movie: "Film",
  tv: "TV",
  audiobook: "Audiobook",
  ebook: "Ebook",
};

export function since(iso: string) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  const m = s / 60;
  if (m < 60) return `${Math.round(m)}\u00a0min ago`;
  const h = m / 60;
  if (h < 24) return `${Math.round(h)}\u00a0h ago`;
  const d = h / 24;
  if (d < 2) return "yesterday";
  if (d < 14) return `${Math.round(d)}\u00a0days ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export const formatSlot = (n: number) => String(n).padStart(4, "0");

/** One wording for a season choice everywhere: "All seasons", "Latest season + new episodes", "S1, S2". */
export function seasonsLabel(s?: number[] | "all" | "latest" | null) {
  if (s === "all") return "All seasons";
  if (s === "latest") return "Latest season + new episodes";
  return s?.length ? s.map((n) => `S${n}`).join(", ") : null;
}

/** "Jul 8", kept on one line. */
export const shortDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }).replace(" ", "\u00a0");

/** "today", "in 1 day", "in 5 days". */
export const inDays = (d: number) => (d <= 0 ? "today" : d === 1 ? "in 1 day" : `in ${d} days`);

/** Smooth scrolling, unless the person asked for less motion. */
export const scrollBehavior = (): ScrollBehavior =>
  matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";

/** A "Copied" state that clears itself: `const [copied, flash] = useCopied()`. */
export function useCopied(ms = 1600) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const flash = () => {
    setCopied(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), ms);
  };
  return [copied, flash] as const;
}

/* ----------------------------------------------------------------- straps */

/** A page's title and its one-line lede. */
export function PageHead({ title, lede, as: Tag = "div", children }: {
  title: ReactNode; lede?: ReactNode; as?: "div" | "header"; children?: ReactNode;
}) {
  return (
    <Tag className="page-head">
      <h1 className="display page-title">{title}</h1>
      {lede ? <p className="muted">{lede}</p> : null}
      {children}
    </Tag>
  );
}

/** The mascot with a line or two: empty lists, nothing found, off the air. */
export function Moment({ children }: { children: ReactNode }) {
  return <div className="moment"><Mascot />{children}</div>;
}

/** An icon beside a heading and a line of explanation. */
export function Notice({ icon: Icon, title, level = 2, children }: {
  icon: IconType; title: ReactNode; level?: 2 | 3; children: ReactNode;
}) {
  return (
    <div className="notice">
      <Icon aria-hidden />
      <div>{level === 2 ? <h2 className="h3">{title}</h2> : <h3>{title}</h3>}<p className="muted">{children}</p></div>
    </div>
  );
}

/** A page section with its heading (and an optional link beside it). */
export function Section({ id, title, action, className, busy, children }: {
  id: string; title: ReactNode; action?: ReactNode; className?: string; busy?: boolean; children?: ReactNode;
}) {
  return (
    <section className={className ? `section ${className}` : "section"} aria-labelledby={id} aria-busy={busy}>
      <div className="section__head"><h2 id={id}>{title}</h2>{action}</div>
      {children}
    </section>
  );
}

export function Strap({ stage, children, wipe = false }: { stage: RequestStage | "live"; children?: ReactNode; wipe?: boolean }) {
  const label = stage === "live" ? "On air" : STAGE_LABEL[stage];
  const icon = stage === "live" ? null : STAGE_ICON[stage];
  return (
    <div className={`strap strap--${stage}${wipe ? " strap--wipe" : ""}`}>
      <span className="strap__tab">{icon}{label}</span>
      {children ? <span className="strap__bar"><span>{children}</span></span> : null}
    </div>
  );
}

/* ---------------------------------------------------------------- posters */

export function Art({ title, size = "w342", eager = false }: { title: Pick<Title, "poster" | "title" | "kind">; size?: "w185" | "w342"; eager?: boolean }) {
  const [broken, setBroken] = useState(false);
  const src = artUrl(title.poster, size);
  if (!src || broken) {
    const Icon = title.kind === "audiobook" ? Headphones : title.kind === "ebook" ? BookOpen : null;
    // No artwork: a cover drawn in code, its colours picked from the title so it stays the same everywhere.
    let h = 0;
    for (const ch of title.title) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    // Two-letter initials for covers too small to hold the title (the CSS picks which shows).
    const initials = title.title.replace(/^(the|a|an)\s+/i, "").split(/[\s:&-]+/).filter(Boolean)
      .slice(0, 2).map((w) => [...w][0]).join("").toUpperCase();
    return (
      <div className={`art-fallback art-fallback--${h % 6}`} aria-hidden>
        {Icon ? <Icon /> : null}
        <span className="art-fallback__title">{title.title}</span>
        <b className="art-fallback__initials">{initials}</b>
      </div>
    );
  }
  return <img src={src} alt="" loading={eager ? "eager" : "lazy"} decoding="async" onError={() => setBroken(true)} />;
}

export const titlePath = (t: Pick<Title, "kind" | "id">) => `/title/${t.kind}/${encodeURIComponent(t.id)}`;

/* ------------------------------------------------------------ discord mark */

export function DiscordMark() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden fill="currentColor">
      <path d="M19.6 5.3A17.4 17.4 0 0 0 15.3 4l-.2.4-.3.6a16 16 0 0 0-4.8 0l-.3-.6L9.5 4a17.4 17.4 0 0 0-4.3 1.3C2.4 9.4 1.7 13.4 2 17.4a17.6 17.6 0 0 0 5.3 2.6l.7-1 .4-.8a11 11 0 0 1-1.7-.8l.4-.3a12.5 12.5 0 0 0 10.7 0l.4.3-1.7.8.4.8.7 1a17.5 17.5 0 0 0 5.3-2.6c.4-4.6-.7-8.6-3.3-12.1ZM8.7 15c-1 0-1.9-1-1.9-2.1s.8-2.1 1.9-2.1 1.9 1 1.9 2.1-.8 2.1-1.9 2.1Zm6.6 0c-1 0-1.9-1-1.9-2.1s.8-2.1 1.9-2.1 1.9 1 1.9 2.1-.8 2.1-1.9 2.1Z" />
    </svg>
  );
}
