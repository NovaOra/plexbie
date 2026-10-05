import { MoreLikeThis } from "../components/Discover";
import { useEffect, useRef, useState } from "react";
import { motion } from "motion/react";
import { Link, useParams } from "react-router-dom";
import { Ban, Check, CircleDot, Clock3, Play } from "../components/icons";
import { api, ApiError, artUrl } from "../api/client";
import type { BookFormat, MediaKind, MediaRequest, SeasonStatus, Title } from "../api/types";
import { useSession } from "../components/Layout";
import { Mascot } from "../components/Mascot";
import { Countdown, Journey, LiveProgress } from "../components/motion";
import { HelpButton } from "../components/HelpSheet";
import { Art, formatSlot, isBook, KIND_LABEL, homeOf, OffAir, scrollBehavior, stageLabel, stageHelp, useLoad, whileActive, useTitle, Notice } from "../components/ui";

type SeasonPick = "latest" | number[];

const SEASON_WORDS: Partial<Record<SeasonStatus, string>> = {
  available: "On Plex", requested: "Requested", upcoming: "Not aired yet",
};

/** Seasons that can still be asked for: missing, or only partly on Plex. */
const openSeasons = (t: Title) => (t.seasons ?? []).filter((s) => !s.status || s.status === "none" || s.status === "partial");

function seasonSummary(pick: SeasonPick) {
  if (pick === "latest") return "the latest season, plus new episodes as they air";
  return pick.length === 1 ? `season ${pick[0]}` : `seasons ${pick.join(", ")}`;
}

/**
 * Every season with where it stands. What's on Plex gets a tick and what's
 * already requested says so; only the rest can be picked, so asking for more
 * seasons later just works.
 */
function SeasonChooser({ title, pick, setPick }: { title: Title; pick: SeasonPick; setPick: (p: SeasonPick) => void }) {
  const seasons = title.seasons ?? [];
  const open = openSeasons(title).map((s) => s.n);
  const chosen = Array.isArray(pick) ? pick : [];
  const aired = seasons.filter((s) => s.status !== "upcoming").map((s) => s.n);
  const newest = aired.length ? Math.max(...aired) : null;
  const toggle = (n: number) => {
    const next = chosen.includes(n) ? chosen.filter((x) => x !== n) : [...chosen, n].sort((a, b) => a - b);
    setPick(next);
  };
  const allMissing = open.length > 0 && chosen.length === open.length && open.every((n) => chosen.includes(n));
  return (
    <div className="chooser">
      <h2 className="h3">{seasons.some((s) => s.status === "available") ? "Want more seasons?" : "Which seasons?"}</h2>
      <div className="choices">
        {open.length > 1 ? (
          <button type="button" className="choice" aria-pressed={allMissing} onClick={() => setPick(open)}>
            {seasons.some((s) => s.status && s.status !== "none") ? "All the missing ones" : "All seasons"}
          </button>
        ) : null}
        {newest !== null && open.includes(newest) ? (
          <button type="button" className="choice" aria-pressed={pick === "latest"} onClick={() => setPick("latest")}>Latest + new episodes</button>
        ) : null}
      </div>
      <ul className="seasons" aria-label="Seasons">
        {seasons.map((s) => {
          const can = open.includes(s.n);
          const on = can && (pick === "latest" ? s.n === newest : chosen.includes(s.n));
          const status = s.status ?? "none";
          return (
            <li key={s.n}>
              <button type="button" className={`season season--${status}${on ? " is-on" : ""}`} disabled={!can} aria-pressed={can ? on : undefined}
                onClick={() => (pick === "latest" ? setPick([s.n]) : toggle(s.n))}>
                <span className="season__box" aria-hidden>{status === "available" ? <Check size={16} /> : on ? <Check size={16} /> : null}</span>
                <span className="season__name">Season {s.n}<small>{s.episodes ? `${s.episodes} episodes` : "No episodes yet"}</small></span>
                <span className="season__state">
                  {status === "partial" ? `${s.have} of ${s.episodes} on Plex` : SEASON_WORDS[status] ?? ""}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function FormatChooser({ format, setFormat }: { format: BookFormat; setFormat: (f: BookFormat) => void }) {
  const options: [BookFormat, string][] = [["audiobook", "Audiobook"], ["ebook", "Ebook"], ["both", "Both"]];
  return (
    <div className="chooser">
      <h2 className="h3">Which format?</h2>
      <div className="choices">
        {options.map(([id, label]) => (
          <button key={id} type="button" className="choice" aria-pressed={format === id} onClick={() => setFormat(id)}>{label}</button>
        ))}
      </div>
    </div>
  );
}

/**
 * "Request sent": the one moment that earns a little bounce. It settles in where
 * the form was, stays in view (the form above it was taller), and takes keyboard
 * focus so screen readers announce it.
 */
function Requested({ request }: { request: MediaRequest }) {
  const box = useRef<HTMLDivElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
    box.current?.scrollIntoView({ block: "nearest", behavior: scrollBehavior() });
  }, []);
  return (
    <motion.div ref={box} className="confirm" role="status"
      initial={{ opacity: 0, y: 8, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", duration: 0.4, bounce: 0.15 }} style={{ transformOrigin: "50% 0%" }}>
      <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
        <motion.span initial={{ scale: 0.8 }} animate={{ scale: 1 }} style={{ transformOrigin: "50% 100%", display: "inline-flex" }}
          transition={{ type: "spring", duration: 0.45, bounce: 0.3, delay: 0.06 }}>
          <Mascot />
        </motion.span>
        <div style={{ display: "grid", gap: 8 }}>
          <h2 className="h3" ref={heading} tabIndex={-1}>Request No. {formatSlot(request.slot)} is in</h2>
          <p className="muted">
            An admin gets it in Discord now. You’ll get a DM when it’s approved or declined
            {request.title.kind === "movie" || request.title.kind === "tv" ? ", and again when it’s ready to watch." : "."}
          </p>
        </div>
      </div>
      <Journey stage={request.stage} kind={request.title.kind} intro />
      <Link to="/schedule">Follow it in My requests</Link>
    </motion.div>
  );
}

export function TitlePage() {
  const { kind, id } = useParams() as { kind: MediaKind; id: string };
  const { session } = useSession();
  const title = useLoad(() => api.title(kind, id), [kind, id]);
  useTitle(title.data?.title ?? (title.loading ? null : "Not found"));
  const requests = useLoad(() => (title.data?.yourRequest ? api.myRequests() : Promise.resolve([])), [title.data?.yourRequest?.slot], whileActive);
  const mine = requests.data?.find((r) => r.slot === title.data?.yourRequest?.slot);
  const [pickState, setPick] = useState<SeasonPick | null>(null);
  const [format, setFormat] = useState<BookFormat>(kind === "ebook" ? "ebook" : "audiobook");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<MediaRequest | null>(null);

  if (title.loading) {
    // The page's own shape (hero, poster, title, facts), so nothing jumps when it arrives.
    return (
      <div className="shell page" aria-busy="true" aria-label="Loading">
        <div className="title-hero">
          <div className="title-head">
            <div className="poster__art skeleton" />
            <div style={{ display: "grid", gap: 12 }}>
              <div className="skeleton" style={{ height: "2.4rem", width: "70%" }} />
              <div className="skeleton" style={{ height: 16, width: "55%" }} />
            </div>
          </div>
        </div>
        <div className="split">
          <div style={{ display: "grid", gap: 10 }}>
            {[96, 100, 88, 60].map((w, i) => <div key={i} className="skeleton" style={{ height: 16, width: `${w}%` }} />)}
          </div>
        </div>
      </div>
    );
  }
  if (title.error || !title.data) {
    return (
      <div className="shell page">
        <OffAir onRetry={title.reload}>Couldn’t load this title just now. Nothing is lost.</OffAir>
        <Link to="/search">Back to search</Link>
      </div>
    );
  }
  const t = title.data;
  const book = isBook(t.kind);
  const tv = t.kind === "tv";
  const missing = tv ? openSeasons(t) : [];
  // Nothing is ticked until the person picks; "all" goes to the server only when nothing is on Plex or requested yet.
  const pick: SeasonPick = pickState ?? [];
  const nothingYet = tv && (t.seasons ?? []).every((s) => !s.status || s.status === "none" || s.status === "upcoming");
  const sendSeasons = pick === "latest" ? "latest" : nothingYet && pick.length === missing.length ? "all" : pick;
  const backdrop = artUrl(t.backdrop, "w1280");

  const send = async () => {
    setSending(true);
    setError(null);
    try {
      const r = await api.request({
        kind: t.kind, id: t.id,
        seasons: tv ? sendSeasons : undefined,
        format: book ? format : undefined,
      });
      setDone(r);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409
        ? (tv ? e.message : "Someone already asked for this one. It’s in the queue.")
        : e instanceof ApiError && e.status === 403
          ? e.message
          : "That didn’t go through. Try again, or use /request in Discord.");
    } finally {
      setSending(false);
    }
  };

  const facts = [
    KIND_LABEL[t.kind], t.year, t.author,
    t.runtime ? `${Math.floor(t.runtime / 60)} h ${t.runtime % 60} min` : null,
    t.seasons ? `${t.seasons.length} season${t.seasons.length === 1 ? "" : "s"}` : null,
    ...(t.genres ?? []),
  ].filter(Boolean);

  return (
    <div className="shell page">
      <div className="title-hero">
        {backdrop ? <div className="title-hero__backdrop"><img src={backdrop} alt="" /></div> : null}
        <div className="title-head">
          <div className="poster__art"><Art title={t} eager /></div>
          <div>
            <h1>{t.title}</h1>
            <div className="title-facts">{facts.map((f) => <span key={String(f)}>{f}</span>)}</div>
            {t.plexUrl ? (
              <a className="btn btn--primary title-watch" href={t.plexUrl} target="_blank" rel="noopener noreferrer">
                <Play aria-hidden /> Watch on Plex
              </a>
            ) : null}
          </div>
        </div>
      </div>

      <div className="split">
        <div style={{ display: "grid", gap: 28 }}>
          <Countdown leaving={t.leaving} long />
          {t.overview ? <p style={{ fontSize: "1.05rem", maxWidth: "65ch" }}>{t.overview.replace(/\s*[\u2014\u2013]\s*/g, ", ")}</p> : null}
        </div>
        {/* What you can do with it: beside the overview on wide screens (it stays in view), below it on phones. */}
        <div className="title-actions">
          {done ? <Requested request={done} /> : null}

          {!done && t.yourRequest && t.yourRequest.stage !== "available" ? (
            <div className="confirm">
              <h2 className="h3">Your request No. {formatSlot(t.yourRequest.slot)}: {stageLabel(t.yourRequest.stage, t.kind).toLowerCase()}</h2>
              <Journey stage={t.yourRequest.stage} kind={t.kind} />
              {mine ? <LiveProgress stage={mine.stage} progress={mine.progress} /> : null}
              <p className="muted">{stageHelp(t.yourRequest.stage, t.kind)}.</p>
              {mine ? <HelpButton request={mine} /> : null}
              <Link to="/schedule">See all your requests</Link>
            </div>
          ) : null}

          {done ? null : t.availability === "blocked" ? (
            <Notice icon={Ban} title="Not available to request">An admin has blocked this title, so it can’t be requested.</Notice>
          ) : tv && missing.length && session?.member ? (
            <div className="confirm">
              <SeasonChooser title={t} pick={pick} setPick={setPick} />
              <p className="muted">
                {Array.isArray(pick) && !pick.length ? "Tick the seasons you want, or use one of the buttons above. " : `You’re asking for ${seasonSummary(pick)}. `}
                An admin approves every request in Discord before it’s downloaded.
              </p>
              {error ? <p className="field__error" role="alert">{error}</p> : null}
              {session?.preview ? <p className="muted" role="note">This is the read-only preview, so requesting is switched off.</p> : null}
              <button className="btn btn--primary btn--block" onClick={send} disabled={sending || !!session?.preview || (Array.isArray(pick) && !pick.length)}>
                {sending ? "Sending…" : Array.isArray(pick) && !pick.length ? "Pick seasons to request" : pick === "latest" || pick.length !== 1 ? `Request ${pick === "latest" ? "the latest season" : `${pick.length} seasons`}` : `Request season ${pick[0]}`}
              </button>
            </div>
          ) : tv && t.yourRequest && t.yourRequest.stage !== "available" ? null
          : tv && (t.seasons ?? []).length && !missing.length ? (
            <Notice icon={CircleDot} title={(t.seasons ?? []).every((s) => s.status === "available" || s.status === "upcoming") ? "Every season is on Plex" : "Nothing left to ask for"}>
              {(t.seasons ?? []).some((s) => s.status === "requested")
                ? "What isn’t on Plex yet is already requested. It’ll show up in Just arrived."
                : `${t.plexUrl ? "Watch on Plex opens it." : "Open Plex and search for it."} New seasons show up here to request when they air.`}
            </Notice>
          ) : !tv && t.yourRequest && t.yourRequest.stage !== "available" ? null
          : !tv && t.availability === "available" ? (
            <Notice icon={CircleDot} title={`Already on ${homeOf(t.kind)}`}>{t.plexUrl ? "Watch on Plex opens it." : `Open ${homeOf(t.kind)} and search for it.`} If something’s missing, say so in Discord.</Notice>
          ) : !tv && t.availability === "requested" ? (
            <Notice icon={Clock3} title="Already requested">It’s waiting in the queue, so there’s nothing more to do. It’ll show up in Just arrived.</Notice>
          ) : !session?.member ? (
            <Notice icon={Clock3} title="Requests are for Plex members"><Link to="/">Ask to join Plex</Link> first; once you’re in, you can request anything.</Notice>
          ) : !tv ? (
            <div className="confirm">
              {book ? <FormatChooser format={format} setFormat={setFormat} /> : null}
              <p className="muted">An admin approves every request in Discord before it’s downloaded.</p>
              {error ? <p className="field__error" role="alert">{error}</p> : null}
              {session?.preview ? <p className="muted" role="note">This is the read-only preview, so requesting is switched off.</p> : null}
              <button className="btn btn--primary btn--block" onClick={send} disabled={sending || !!session?.preview}>
                {sending ? "Sending…" : `Request ${t.title}`}
              </button>
            </div>
          ) : null}
        </div>
      </div>
      {t.kind === "movie" || t.kind === "tv" ? <MoreLikeThis kind={t.kind} id={t.id} /> : null}
    </div>
  );
}
