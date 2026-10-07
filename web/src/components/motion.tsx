import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  AnimatePresence, motion, useReducedMotion,
} from "motion/react";
import { Check, CircleDot, Clock3, Download, FolderInput, Hourglass, PackageOpen, Pause, Play, ShieldCheck, X } from "./icons";
import type { Icon } from "./icons";
import { artUrl } from "../api/client";
import type { Arrival, MediaRequest, RequestProgress, RequestStage, Title } from "../api/types";
import { Art, formatSlot, homeOf, inDays, since, titlePath } from "./ui";

export const EASE_OUT = [0.23, 1, 0.32, 1] as const;

/** Spread on a motion element to rise into place after `delay` seconds; still under reduced motion. */
export function useRise(distance = 12, duration = 0.5) {
  const reduced = useReducedMotion();
  return (delay = 0) => ({
    initial: reduced ? (false as const) : { opacity: 0, transform: `translateY(${distance}px)` },
    animate: { opacity: 1, transform: "translateY(0px)" },
    transition: { duration, ease: EASE_OUT, delay },
  });
}

/** True while the tab is in the background, so nothing animates or advances unseen. */
export function usePageHidden() {
  const [hidden, setHidden] = useState(() => document.hidden);
  useEffect(() => {
    const onVis = () => setHidden(document.hidden);
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);
  return hidden;
}

/** Steps through `count` items every `ms`, holding still while paused, hidden or
 *  under reduced motion. With `once`, it stops after one full round, back on the
 *  first: text that keeps changing on its own is hard to read. */
export function useAutoAdvance(count: number, ms: number, paused: boolean, once = false) {
  const reduced = useReducedMotion();
  const hidden = usePageHidden();
  const [i, setI] = useState(0);
  const [steps, setSteps] = useState(0);
  useEffect(() => {
    if (count < 2 || paused || reduced || hidden || (once && steps >= count)) return;
    const t = setTimeout(() => { setI((n) => (n + 1) % count); setSteps((n) => n + 1); }, ms);
    return () => clearTimeout(t);
  }, [i, paused, reduced, hidden, count, ms, once, steps]);
  return [i, setI] as const;
}

/* --------------------------------------------------------------- reveal */

/** One entrance for a group when it first renders: children rise in, in reading order. */
export function Stagger({ children, className, instant = false }: { children: ReactNode; className?: string; instant?: boolean }) {
  const reduced = useReducedMotion();
  return (
    <motion.div
      className={className}
      initial={reduced || instant ? false : "hidden"}
      animate="shown"
      variants={{ shown: { transition: { staggerChildren: 0.03 } } }}
    >
      {children}
    </motion.div>
  );
}

const riseItem = {
  hidden: { opacity: 0, y: 8 },
  shown: { opacity: 1, y: 0, transition: { duration: 0.25, ease: EASE_OUT } },
};

/* --------------------------------------------------------------- poster */

export function PosterCard({ title, meta, link = true }: { title: Title; meta?: ReactNode; link?: boolean }) {
  return (
    <motion.div variants={riseItem} className="poster-card">
      <Link
        to={link ? titlePath(title) : "#"}
        aria-disabled={link || title.plexUrl ? undefined : true}
        aria-label={!link && title.plexUrl ? `${title.title}: watch on Plex` : undefined}
        className="poster"
        onClick={(e) => {
          if (!link) {
            // No page of its own (Plex knows it, TMDB doesn't): open it on Plex instead.
            e.preventDefault();
            if (title.plexUrl) window.open(title.plexUrl, "_blank", "noopener,noreferrer");
          }
        }}
      >
        <div className="poster__art">
          <Art title={title} />
          {/* Whether it's already there is the first thing you look for: on the poster, not in small print. */}
          {title.availability === "available" || title.availability === "requested" ? (
            <span className="poster__badge">
              <span className={`badge${title.availability === "requested" ? " badge--requested" : ""}`}>
                {title.availability === "available" ? <><CircleDot aria-hidden />On {homeOf(title.kind)}</> : <><Clock3 aria-hidden />Requested</>}
              </span>
            </span>
          ) : null}
        </div>
        <div>
          <div className="poster__title">{title.title}</div>
          <div className="poster__meta">{meta ?? [title.author, title.year].filter(Boolean).join(", ")}</div>
          <Countdown leaving={title.leaving} />
        </div>
      </Link>
    </motion.div>
  );
}

/* --------------------------------------------------------------- ticker */

/** The broadcast crawl. Pauses on hover and keyboard focus; holds still under reduced motion. */
export function Ticker({ items, label = "Latest" }: { items: string[]; label?: string }) {
  const [paused, setPaused] = useState(false);
  const [held, setHeld] = useState(false); // keyboard focus inside the crawl
  if (!items.length) return null;
  // One item doesn't crawl ("3 watching ◆ 3 watching"): it just sits there.
  if (items.length < 2) {
    return (
      <div className="ticker ticker--still">
        <span className="ticker__tab caps">{label}</span>
        <div className="ticker__track"><span className="ticker__item">{items[0]}</span></div>
      </div>
    );
  }
  const run = [...items, ...items];
  return (
    <div className={`ticker${paused || held ? " is-paused" : ""}`} role="marquee" aria-label={`${label}: ${items.join(". ")}`}
      // Keyboard focus only: a tap or click also focuses the button, and "Play" would then do nothing.
      onFocus={(e) => setHeld(e.target.matches(":focus-visible"))}
      onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setHeld(false); }}>
      <span className="ticker__tab caps">{label}</span>
      <div className="ticker__track" aria-hidden>
        <div className="ticker__run" style={{ animationDuration: `${Math.max(24, items.length * 7)}s` }}>
          {run.map((t, i) => <span key={i} className="ticker__item">{t}</span>)}
        </div>
      </div>
      <button type="button" className="ticker__pause" aria-pressed={paused}
        onClick={() => { setPaused((p) => !p); setHeld(false); }}
        aria-label={paused ? "Play the crawl" : "Pause the crawl"}>
        {paused ? <Play size={14} aria-hidden /> : <Pause size={14} aria-hidden />}
      </button>
    </div>
  );
}

/* ---------------------------------------------------------------- stage */

const SLIDE_MS = 7000;

/**
 * The house channel: real backdrops of what just arrived, one at a time, with
 * a lower-third strap per slide and a channel strip that counts down to the next.
 */
export function Stage({ arrivals }: { arrivals: Arrival[] }) {
  // Backdrops when there are any; otherwise (audiobooks, Plex-only items, demo
  // mode) the same channel without the picture, never an empty screen.
  const withArt = arrivals.filter((a) => a.title.backdrop).slice(0, 5);
  const bare = withArt.length === 0;
  const items = bare ? arrivals.slice(0, 5) : withArt;
  const hidden = usePageHidden();
  const [held, setHeld] = useState(false);       // mouse or keyboard focus inside the stage
  const [stopped, setStopped] = useState(false); // the viewer took over: it stays where they put it
  const [loaded, setLoaded] = useState<string | null>(null);
  const reduced = useReducedMotion();
  const paused = hidden || held || stopped;
  const [i, setI] = useAutoAdvance(items.length, SLIDE_MS, held || stopped);

  const current = items[i];
  return (
    <section className={`stage${bare ? " stage--compact stage--bare" : ""}`} aria-roledescription="carousel" aria-label="Just arrived on Plex"
      // Keyboard focus only: a tap or click also focuses the button it lands on,
      // and that would hold the channel after "Play" until the next tap elsewhere.
      onFocus={(e) => setHeld(e.target.matches(":focus-visible"))} onBlur={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setHeld(false); }}>
      <div className="stage__media" aria-hidden>
        <AnimatePresence>
          {current && !bare ? (
            <motion.img
              key={current.title.id}
              src={artUrl(current.title.backdrop, "w1280") ?? undefined}
              alt=""
              // Fades in once the picture has actually arrived, not into a half-loaded frame.
              onLoad={() => setLoaded(current.title.id)}
              initial={{ opacity: 0, scale: reduced ? 1 : 1.12 }}
              animate={{ opacity: loaded === current.title.id ? 1 : 0, scale: 1 }}
              exit={{ opacity: 0 }}
              transition={{ opacity: { duration: 1.1, ease: "easeOut" }, scale: { duration: reduced ? 0 : 9, ease: "linear" } }}
            />
          ) : null}
        </AnimatePresence>
      </div>
      <div className="stage__scrim" aria-hidden />

      <div className="shell stage__inner">
        {!current ? (
          <div className="stage__feature">
            <div className="strap strap--closed">
              <span className="strap__tab">Now</span>
              <span className="strap__bar"><span>Nothing new on the channel yet. New arrivals show up here.</span></span>
            </div>
          </div>
        ) : null}

        {current ? (
          <div className="stage__feature" aria-live={stopped || reduced ? "polite" : "off"}>
            <div className="stage__strapline">
              <div className="strap strap--available strap--wipe" key={current.title.id}>
                <span className="strap__tab"><Play aria-hidden />Just arrived</span>
                <span className="strap__bar"><span>
                  {current.detail ? `${current.detail}, ` : ""}{since(current.addedAt)}
                </span></span>
              </div>
              {items.length > 1 && !reduced ? (
                <button type="button" className="stage__pause" aria-pressed={stopped}
                  aria-label={stopped ? "Play the channel" : "Pause the channel"}
                  // Pressing it settles the matter, keyboard focus or not.
                  onClick={() => { setStopped((s) => !s); setHeld(false); }}>
                  {stopped ? <Play size={16} aria-hidden /> : <Pause size={16} aria-hidden />}
                </button>
              ) : null}
            </div>
            <Link to={titlePath(current.title)} className="stage__title" key={`t-${current.title.id}`}
              data-long={current.title.title.length > 22 ? "" : undefined}>
              {current.title.title}
            </Link>
          </div>
        ) : null}

        {items.length > 1 ? (
          <div className="stage__channels" onPointerEnter={(e) => { if (e.pointerType === "mouse") setHeld(true); }}
            onPointerLeave={(e) => { if (e.pointerType === "mouse") setHeld(false); }}>
            {items.map((a, n) => (
              <button
                key={a.title.id}
                type="button"
                className="channel"
                aria-current={n === i}
                aria-label={`Show ${a.title.title}`}
                onClick={() => { setI(n); setStopped(true); }}
              >
                <span className="channel__art"><Art title={a.title} size="w185" /></span>
                <span className="channel__name">{a.title.title}</span>
                <span className="channel__meter" aria-hidden>
                  {n === i ? <i key={`${i}-${paused}`} style={{ animationDuration: `${SLIDE_MS}ms`, animationPlayState: paused || reduced ? "paused" : "running" }} /> : null}
                </span>
              </button>
            ))}
          </div>
        ) : null}
      </div>
    </section>
  );
}

/* -------------------------------------------------------------- journey */

const STEPS: { id: Exclude<RequestStage, "declined">; label: string; icon: Icon }[] = [
  { id: "requested", label: "Requested", icon: Clock3 },
  { id: "approved", label: "Approved", icon: Check },
  { id: "downloading", label: "Downloading", icon: Download },
  { id: "unpacking", label: "Unpacking", icon: PackageOpen },
  { id: "importing", label: "Adding to Plex", icon: FolderInput },
  { id: "available", label: "On Plex", icon: Play },
];

/** Where a request is on its way to Plex, drawn as a track that fills to the current stage. */
/**
 * Where a request is. It shows its real state straight away (never an empty
 * rail waiting to be scrolled to); a stage that changes while you watch moves
 * the fill on and pops the new node. `intro` builds it step by step on mount,
 * for the moments that earn it: the landing demo and "Request sent".
 */
export function Journey({ stage, compact = false, steps, kind, intro = false }: { stage: RequestStage | number; compact?: boolean; steps?: string[]; kind?: string; intro?: boolean }) {
  const reduced = useReducedMotion();
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; }, []);
  const build = intro && !reduced;
  const labels = steps ?? STEPS.map((s) => (s.id === "available" ? `On ${homeOf(kind)}` : s.id === "importing" ? `Adding to ${homeOf(kind)}` : s.label));
  const declined = stage === "declined";
  const step: Record<string, number> = { requested: 0, approved: 1, upcoming: 1, searching: 1, downloading: 2, unpacking: 3, importing: 4, available: 5 };
  const at = typeof stage === "number" ? stage : declined ? 0 : step[stage] ?? 0;
  const pct = labels.length > 1 ? (at / (labels.length - 1)) * 100 : 0;

  return (
    <div className={`journey${compact ? " journey--compact" : ""}${declined ? " journey--declined" : ""}`}
      role="img" aria-label={declined ? "Declined" : `${labels[at]} (step ${at + 1} of ${labels.length})`}>
      <div className="journey__rail">
        <motion.i
          className="journey__fill"
          initial={build ? { scaleX: 0 } : false}
          animate={{ scaleX: pct / 100 }}
          transition={{ duration: reduced ? 0 : build && !mounted.current ? 1.1 : 0.4, ease: EASE_OUT, delay: build && !mounted.current ? 0.15 : 0 }}
        />
      </div>
      <ol className="journey__steps">
        {labels.map((label, n) => {
          const Icon = steps ? null : STEPS[n]?.icon;
          const state = declined ? (n === 0 ? "stop" : "todo") : n < at ? "done" : n === at ? "now" : "todo";
          return (
            <li key={label} className={`journey__step is-${state}`}>
              <motion.span
                key={state === "now" ? "now" : "other"}
                className="journey__node"
                initial={build && !mounted.current ? { scale: 0.6, opacity: 0 }
                  : state === "now" && mounted.current && !reduced ? { scale: 0.85 } : false}
                animate={{ scale: 1, opacity: 1 }}
                transition={{ delay: build && !mounted.current ? 0.15 + n * 0.18 : 0, duration: build && !mounted.current ? 0.4 : 0.2, ease: EASE_OUT }}
              >
                {state === "stop" ? <X aria-hidden /> : Icon ? <Icon aria-hidden /> : <b>{n + 1}</b>}
              </motion.span>
              {!compact ? <span className="journey__label">{state === "stop" ? "Declined" : label}</span> : null}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/* ---------------------------------------------------------- live detail */

function eta(iso?: string | null) {
  if (!iso) return null;
  const min = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  if (min <= 0) return "almost done";
  if (min < 60) return `about ${min} min left`;
  return `about ${Math.round(min / 60)} h left`;
}

/** Live numbers from Radarr, Sonarr or SABnzbd: a bar, what's happening, and seasons. */
export function LiveProgress({ stage, progress }: { stage: RequestStage; progress?: RequestProgress | null }) {
  if (!progress) return null;
  const showBar = stage === "downloading" && typeof progress.percent === "number";
  // While SABnzbd unpacks: its own count when it gives one, otherwise a bar that keeps moving.
  const unpackBar = stage === "unpacking";
  const line = [progress.detail, stage === "downloading" ? eta(progress.eta) : null].filter(Boolean).join(", ");
  return (
    <div className="live">
      {unpackBar ? (
        typeof progress.percent === "number" ? (
          <div className="live__bar live__bar--unpack" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress.percent ?? undefined} aria-label="Unpacking progress">
            <i style={{ "--p": `${progress.percent}%` } as React.CSSProperties} />
            <span>{progress.percent}%</span>
          </div>
        ) : (
          <div className="live__bar live__bar--busy" role="progressbar" aria-label="Unpacking" aria-valuetext="Unpacking"><i /></div>
        )
      ) : null}
      {showBar ? (
        <div className="live__bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress.percent ?? undefined} aria-label="Download progress">
          <i style={{ "--p": `${progress.percent}%` } as React.CSSProperties} />
          <span>{progress.percent}%</span>
        </div>
      ) : null}
      {line ? <p className="live__line">{line}</p> : null}
      {progress.problem ? <p className="live__problem" role="status">{progress.problem}</p> : null}
      {progress.seasons?.length ? (
        <ul className="live__seasons">
          {progress.seasons.map((s) => (
            <li key={s.n}>
              <span>Season {s.n}</span>
              <span className="live__season-bar"><i style={{ "--p": `${s.total ? Math.min(100, (s.have / s.total) * 100) : 0}%` } as React.CSSProperties} /></span>
              <span className="muted">{s.have}/{s.total || "?"}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------ countdown */

/** Days until media_cleanup removes a title, as a small line under its poster. */
export function Countdown({ leaving, long = false }: { leaving?: import("../api/types").Leaving | null; long?: boolean }) {
  if (!leaving) return null;
  if (leaving.exempt) {
    return long ? (
      <p className="countdown countdown--kept"><ShieldCheck aria-hidden /> Kept permanently. It won’t be cleaned up.</p>
    ) : (
      <span className="countdown countdown--kept" title="Kept permanently"><ShieldCheck aria-hidden />Kept</span>
    );
  }
  const d = leaving.daysLeft ?? 0;
  const when = inDays(d);
  if (long) {
    return (
      <p className={`countdown${leaving.warning ? " is-warning" : ""}`}>
        <Hourglass aria-hidden />
        <span>
          {leaving.practice ? "Cleanup is in practice mode, but this would leave Plex " : "Leaves Plex "}
          {when} unless someone watches it. Watching it resets the clock.
        </span>
      </p>
    );
  }
  return (
    <span className={`countdown${leaving.warning ? " is-warning" : ""}`} title={`Leaves Plex ${when} unless watched`}>
      <Hourglass aria-hidden />{d === 0 ? "Leaving today" : `${d} day${d === 1 ? "" : "s"} left`}
    </span>
  );
}

/** One request in a list: its number, poster, title and journey, linking to the title. */
export function RequestSlot({ r, before, after, children }: {
  r: MediaRequest; before?: ReactNode; after?: ReactNode; children?: ReactNode;
}) {
  return (
    <motion.div variants={riseItem}>
      <Link className="slot slot--journey" to={titlePath(r.title)}>
        <span className="slot__no">No.<b>{formatSlot(r.slot)}</b></span>
        <span className="slot__poster"><Art title={r.title} size="w185" /></span>
        <span className="slot__body">
          <span className="slot__title">{r.title.title}</span>
          {before}
          <Journey stage={r.stage} kind={r.title.kind} compact />
          {children}
        </span>
      </Link>
      {after}
    </motion.div>
  );
}
