import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AnimatePresence, motion, useInView, useReducedMotion } from "motion/react";
import { ArrowUpRight, Check, Coffee, Copy, GitFork, Heart, Pause, Play, Star } from "../components/icons";
import type { RequestStage } from "../api/types";
import { CONTACT, DEMO_SITE, GITHUB, REPO_PUBLIC, SUPPORT } from "./links";
import { EASE_OUT, Journey, useAutoAdvance, useRise } from "../components/motion";
import { stageHelp, Strap, useCopied, useTitle } from "../components/ui";
import { DragCrawl } from "../components/DragCrawl";
import { RetroTv, type TvChannel } from "../components/RetroTv";
import { FILMS, TvFilm, type Film } from "./Screening";
import { track } from "./track";

// The public project page: Plexbie the open-source software, for a household's
// Plex server. It shows nothing about any real server or library, every claim
// matches the README and the code, and the logo is the owner's PNG, moved but
// never redrawn (PRODUCT.md).

/* -------------------------------------------------------------- hero ---- */

const DUTIES = ["invites", "requests", "watch parties", "streaks", "new arrivals", "cleanup"];

function ChannelWord({ word }: { word: string }) {
  const reduced = useReducedMotion();
  return (
    <span className="flip">
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span
          key={word}
          className="flip__word"
          initial={reduced ? false : { y: "60%", opacity: 0, filter: "blur(8px)" }}
          animate={{ y: 0, opacity: 1, filter: "blur(0px)" }}
          exit={reduced ? undefined : { y: "-60%", opacity: 0, filter: "blur(8px)" }}
          transition={{ duration: 0.45, ease: EASE_OUT }}
        >
          {word}.
        </motion.span>
      </AnimatePresence>
      {!reduced ? <span className="flip__static" key={`s-${word}`} aria-hidden /> : null}
    </span>
  );
}

/** The owner's logo on a tile. Pressing it changes the channel. */
function TvTile({ onPress }: { onPress: () => void }) {
  const reduced = useReducedMotion();
  const [pressed, setPressed] = useState(0);
  return (
    <div className="tv">
      <motion.button
        type="button"
        className="tv__button"
        aria-label="Change the channel"
        whileTap={{ scale: 0.94 }}
        onClick={() => { setPressed((n) => n + 1); onPress(); }}
      >
        {/* The bob is a CSS animation (off the main thread), on its own layer so it
            doesn't fight the press scale above. */}
        <span className="tv__bob">
          <img src="/brand/plexbie-512.webp" alt="" width={420} height={420} fetchPriority="high" />
        </span>
        {!reduced && pressed ? <span className="tv__flash" key={pressed} aria-hidden /> : null}
      </motion.button>
      <span className="tv__hint caps" aria-hidden>Tap the TV to change the channel</span>
    </div>
  );
}

function Hero({ onWatch }: { onWatch?: () => void }) {
  const reduced = useReducedMotion();
  const [hover, setHover] = useState(false);
  const [i, setI] = useAutoAdvance(DUTIES.length, 2400, hover, true);
  const rise = useRise(18);
  return (
    <section className="p-hero">
      <div className="p-hero__screen" aria-hidden />
      <div className="shell p-hero__inner">
        <div className="p-hero__copy">
          {/* Movement only, no fade: it's the page's largest text, counted as drawn only once visible. */}
          <motion.h1 initial={reduced ? false : { y: 12 }} animate={{ y: 0 }} transition={{ duration: 0.4, ease: EASE_OUT }}
            className="display p-hero__title">
            A Discord bot for your household’s Plex server.
          </motion.h1>
          <motion.p {...rise(0.15)} className="p-hero__line" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
            It handles <ChannelWord word={DUTIES[i]} />
            <span className="visually-hidden"> Invites, requests, watch parties, streaks, new arrivals and cleanup.</span>
          </motion.p>
          <motion.div {...rise(0.25)} className="landing__actions">
            {REPO_PUBLIC ? (
              <a className="btn btn--primary btn--big" href={GITHUB} rel="noopener">
                Get it on GitHub <ArrowUpRight size={20} aria-hidden />
              </a>
            ) : (
              <a className="btn btn--primary btn--big" href="#self-host">How to set it up</a>
            )}
            <a className="btn btn--quiet" href={DEMO_SITE} rel="noopener">
              Try the demo <ArrowUpRight size={18} aria-hidden />
            </a>
            <a className="btn btn--quiet" href="#channels" onClick={() => onWatch?.()}>
              <Play size={18} aria-hidden /> Watch the tour
            </a>
          </motion.div>
        </div>
        <motion.div
          initial={reduced ? false : { scale: 0.96 }}
          animate={{ scale: 1 }}
          transition={{ duration: 0.4, ease: EASE_OUT }}
        >
          <TvTile onPress={() => setI((n) => (n + 1) % DUTIES.length)} />
        </motion.div>
      </div>
    </section>
  );
}

/* ----------------------------------------------------- channel guide ---- */

function WatchingShow() {
  const reduced = useReducedMotion();
  return (
    <div className="show-watch">
      <Strap stage="live">Now watching, updated live</Strap>
      {[0.34, 0.71, 0.12].map((start, n) => (
        <span className="show-watch__bar" key={n}>
          <motion.i
            initial={{ scaleX: start }}
            animate={reduced ? undefined : { scaleX: [start, Math.min(1, start + 0.25)] }}
            transition={{ duration: 6, ease: "linear", repeat: Infinity, repeatType: "reverse", delay: n * 0.4 }}
          />
        </span>
      ))}
    </div>
  );
}

const REQ_STAGES: RequestStage[] = ["requested", "approved", "downloading", "unpacking", "importing", "available"];

function RequestShow() {
  const reduced = useReducedMotion();
  const box = useRef<HTMLDivElement>(null);
  // It plays through once, from the moment it's on screen.
  const seen = useInView(box, { once: true, amount: 0.6 });
  const [i, setI] = useState(reduced ? REQ_STAGES.length - 1 : 0);
  useEffect(() => {
    if (reduced || !seen) return;
    const t = setInterval(() => setI((n) => Math.min(n + 1, REQ_STAGES.length - 1)), 1300);
    return () => clearInterval(t);
  }, [reduced, seen]);
  return (
    <div className="show-req" ref={box}>
      <Journey stage={REQ_STAGES[i]} />
      <Strap key={REQ_STAGES[i]} stage={REQ_STAGES[i]} wipe={!reduced}>{stageHelp(REQ_STAGES[i])}</Strap>
    </div>
  );
}

function Program({ title, text, children }: { title: string; text: string; children?: ReactNode }) {
  return (
    <div className="program">
      <h3 className="program__title">{title}</h3>
      <p className="program__text">{text}</p>
      {children ? <div className="program__show">{children}</div> : null}
    </div>
  );
}

/** What Plexbie does, as cards below the TV. */
const FEATURES: { name: string; render: () => ReactNode }[] = [
  {
    name: "Requests",
    render: () => (
      <Program title="Ask for it, follow it" text="/request for a show, film, audiobook or ebook. One admin queue, and a DM when it’s decided.">
        <RequestShow />
      </Program>
    ),
  },
  {
    name: "Plex access",
    render: () => (
      <Program title="Invites, minus the chores" text="/join-plex, one Approve button, and the bot sends the Plex invite. Accounts stay linked even when names differ.">
        <Strap stage="approved" wipe>Join request approved, invite sent</Strap>
      </Program>
    ),
  },
  {
    name: "Watching",
    render: () => (
      <Program title="Who’s watching what" text="Self-updating now-playing, an all-time leaderboard, daily streaks, and watch-party credit for Discord Go Live.">
        <WatchingShow />
      </Program>
    ),
  },
  {
    name: "Housekeeping",
    render: () => (
      <Program title="The quiet jobs, done" text="Arrival announcements, cleanup of unwatched media with a dry run, books filed into a library, and Plex health alerts.">
        <Strap stage="available" wipe>Added and announced</Strap>
      </Program>
    ),
  },
];

/** The TV's two channels: the teaser (01), then the three-minute tour (02). */
function channels(play?: { film: Film; ask: number }): TvChannel[] {
  return (Object.keys(FILMS) as Film[]).map((film) => ({
    name: FILMS[film].channel,
    hold: true,
    render: () => <TvFilm key={play?.film === film ? play.ask : 0} film={film} start={play?.film === film} />,
  }));
}

function ChannelGuide({ play }: { play?: { film: Film; ask: number } }) {
  const list = useMemo(() => channels(play), [play]);
  const tuneTo = useMemo(() => (play ? { index: list.findIndex((c) => c.name === FILMS[play.film].channel), ask: play.ask } : undefined), [play, list]);
  return (
    <section id="channels" className="guide" aria-labelledby="guide-h" style={{ scrollMarginTop: 80 }}>
      <div className="guide__head">
        <h2 id="guide-h" className="display section-title">Tonight’s line-up</h2>
        <p className="muted">Two channels: the teaser on 01, and the three-minute tour on 02. Or try it yourself in the demo.</p>
      </div>
      <RetroTv channels={list} tuneTo={tuneTo} onTune={(name) => track("channel", { l: name })} />
    </section>
  );
}

function Features() {
  return (
    <section id="features" className="features" aria-labelledby="features-h" style={{ scrollMarginTop: 80 }}>
      <h2 id="features-h" className="display section-title">What it does</h2>
      <div className="features__grid">
        {FEATURES.map((f) => <article key={f.name} className="feature" aria-label={f.name}>{f.render()}</article>)}
      </div>
    </section>
  );
}

/* ----------------------------------------------------------- plugins ---- */

const PLUGINS: [string, string][] = [
  ["user_invites", "/join-plex and the approval flow"],
  ["user_mgmt", "Account linking, inactivity tracking, removal"],
  ["media_requests", "/request for TV, film, audiobook and ebook"],
  ["media_cleanup", "Report and optionally delete unwatched media"],
  ["new_media_added", "Announce additions from Plex webhooks"],
  ["watch_tracking", "Now watching, leaderboard and streaks"],
  ["watch_party", "Watch credit for Discord Go Live streams"],
  ["bookshelf_processor", "File audiobooks and ebooks into a library"],
  ["service_health", "Probe Plex and Tautulli, alert on failure"],
  ["status", "/say, a message from Plexbie in any channel"],
  ["invite_tracker", "Who invited whom to the Discord server"],
];

function Plugins() {
  const half = Math.ceil(PLUGINS.length / 2);
  const [picked, setPicked] = useState<[string, string] | null>(null);
  const [still, setStill] = useState(false);
  const reduced = useReducedMotion();
  const chip = ([name]: [string, string]) => (
    <span className={`crawl__chip${picked?.[0] === name ? " is-picked" : ""}`}>{name}</span>
  );
  return (
    <section className="plugs" aria-labelledby="plugs-h">
      <div className="shell plugs__head">
        <h2 id="plugs-h" className="display section-title">Eleven plugins. Keep the ones you like.</h2>
        <p className="muted">
          Every feature is a plugin you can switch off with one line. Grab the rows and fling them, or tap one to see
          what it does.
        </p>
      </div>
      {reduced ? null : (
        <div className="shell plugs__controls">
          <button type="button" className="btn btn--quiet" aria-pressed={still} onClick={() => setStill((v) => !v)}>
            {still ? <Play size={16} aria-hidden /> : <Pause size={16} aria-hidden />}{still ? "Let them drift" : "Hold still"}
          </button>
        </div>
      )}
      <DragCrawl label="Plugins, first row" items={PLUGINS.slice(0, half)} render={chip} onPick={setPicked} paused={still} />
      <DragCrawl label="Plugins, second row" items={PLUGINS.slice(half)} render={chip} onPick={setPicked} paused={still} reverse speed={30} />
      <div className="shell plugs__foot">
        <div className="plugs__picked" aria-live="polite">
          <AnimatePresence mode="wait" initial={false}>
            {picked ? (
              <motion.p key={picked[0]} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.2 }}>
                <code>{picked[0]}</code> {picked[1]}.
              </motion.p>
            ) : (
              <motion.p key="none" className="muted" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                Tap or click any plugin above.
              </motion.p>
            )}
          </AnimatePresence>
        </div>
        <details className="plugs__all">
          <summary>All eleven, with what they do</summary>
          <ul>
            {PLUGINS.map(([n, d]) => <li key={n}><code>{n}</code> {d}</li>)}
          </ul>
        </details>
      </div>
    </section>
  );
}

/* ---------------------------------------------------------- terminal ---- */

const QUICKSTART = [
  "git clone https://github.com/NovaOra/plexbie.git",
  "cd plexbie",
  "cp config/.env.example config/.env",
  "# fill in DISCORD_BOT_TOKEN, GUILD_ID, PLEX_URL and PLEX_TOKEN",
  "docker build -t plexbie:latest .",
  "docker compose up -d",
];

function Terminal() {
  const full = QUICKSTART.join("\n");
  const [copied, flashCopied] = useCopied();
  return (
    <div className="term">
      <div className="term__bar">
        <span className="caps muted">Terminal</span>
        <button
          type="button"
          className="btn btn--quiet term__copy"
          onClick={async () => {
            try { await navigator.clipboard.writeText(full); flashCopied(); } catch { /* clipboard blocked */ }
          }}
        >
          {copied ? <Check size={16} aria-hidden /> : <Copy size={16} aria-hidden />}{copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre tabIndex={0} aria-label="Quick start commands">
        <code>
          <span className="visually-hidden">{full}</span>
          <span aria-hidden>
            {QUICKSTART.map((line, n) => (
              <span key={n} className={line.startsWith("#") ? "term__comment" : "term__cmd"}>{line}{"\n"}</span>
            ))}
            <span className="term__ok">Plexbie is on air.{"\n"}</span>
          </span>
        </code>
      </pre>
    </div>
  );
}

function SelfHost() {
  return (
    <section id="self-host" className="selfhost" aria-labelledby="self-h" style={{ scrollMarginTop: 80 }}>
      <div className="selfhost__copy">
        <h2 id="self-h" className="display section-title">On air in six commands</h2>
        <p>
          You’ll need Docker, a Discord bot token and a Plex server with its token. Sonarr, Radarr, Seerr, Tautulli
          and SABnzbd are optional and add more features. Use it with media you own or are entitled to use.
        </p>
        {REPO_PUBLIC ? (
          <p className="muted">
            One Discord server per bot, SQLite storage. Bugs and questions go to{" "}
            <a href={`${GITHUB}/issues`} rel="noopener">GitHub Issues</a>.
          </p>
        ) : (
          <p className="muted">
            One Discord server per bot, SQLite storage. The code opens up on GitHub soon; until then, questions go
            to <a href={`mailto:${CONTACT}`}>{CONTACT}</a>.
          </p>
        )}
      </div>
      <Terminal />
    </section>
  );
}

/* --------------------------------------------------------------- end ---- */

function TuneIn() {
  return (
    <section className="tunein" aria-labelledby="tune-h">
      <div className="tunein__copy">
        <h2 id="tune-h" className="display tunein__title">Tune in.</h2>
        {REPO_PUBLIC ? (
          <>
            <p>Plexbie is free, open source under the AGPL-3.0, and built in the open. Star it, fork it, make it yours.</p>
            <div className="landing__actions">
              <a className="btn btn--primary btn--big" href={GITHUB} rel="noopener"><Star size={20} aria-hidden /> Star on GitHub</a>
              <a className="btn btn--quiet" href={`${GITHUB}/fork`} rel="noopener"><GitFork size={16} aria-hidden /> Fork it</a>
            </div>
          </>
        ) : (
          <p>Plexbie is free, and its code is licensed under the AGPL-3.0. It opens up on GitHub soon.</p>
        )}
        <div className="support" aria-labelledby="support-h">
          <h3 id="support-h" className="h3">Help build Plexbie</h3>
          <p>
            Plexbie is made by one person in spare evenings and given away. If it saves your household some admin, you can
            put something toward its development: new features, fixes, and the hours that go into them.
          </p>
          <p className="muted small">
            Tips support Plexbie, the open-source software. They don’t pay for access to anyone’s Plex server or media.
          </p>
          <div className="landing__actions">
            <a className="btn" href={SUPPORT.kofi} rel="noopener"><Heart size={18} aria-hidden /> Support on Ko-fi</a>
            <a className="btn btn--quiet" href={SUPPORT.coffee} rel="noopener"><Coffee size={18} aria-hidden /> Buy Me a Coffee</a>
          </div>
        </div>
      </div>
      <img className="tunein__logo" src="/brand/plexbie-256.webp" alt="" width={220} height={220} loading="lazy" decoding="async" />
    </section>
  );
}

export function Project() {
  useTitle(null);
  const [play, setPlay] = useState<{ film: Film; ask: number }>();
  return (
    <div className="project">
      <Hero onWatch={() => setPlay((p) => ({ film: "tour", ask: (p?.ask ?? 0) + 1 }))} />
      <div className="shell project__body">
        <ChannelGuide play={play} />
        <Features />
      </div>
      <Plugins />
      <div className="shell project__body">
        <SelfHost />
        <TuneIn />
      </div>
    </div>
  );
}
