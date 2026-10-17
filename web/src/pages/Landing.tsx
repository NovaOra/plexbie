import { Fragment, useCallback, useEffect, useRef, useState, type AnchorHTMLAttributes, type ReactNode } from "react";
import { motion, useReducedMotion } from "motion/react";
import { useLocation } from "react-router-dom";
import { PlayCircle } from "../components/icons";
import { loginUrl, signInReturn } from "../api/client";
import { EASE_OUT } from "../components/motion";
import { inviteOutcome } from "./inviteOutcome";
import { DiscordMark, useTitle } from "../components/ui";
import { PlexSignIn } from "../components/PlexSignIn";

// The public face says only what Plexbie is and how to sign in. Nothing about
// the server, its library or its people is shown before login (PRODUCT.md).
// It's a little TV set: channel 1 is the sign-in, channel 2 how to get in, and
// the sign-in keys sit on its deck. The same steps stay on the page below it.

const LOGIN_MESSAGES: Record<string, string> = {
  failed: "Signing in didn’t work just now. Try again in a moment.",
  expired: "That sign-in took too long, so it expired. Try again.",
  cancelled: "Sign-in was cancelled. Nothing was changed.",
  busy: "Lots of sign-ins just now. Wait a few minutes and try again.",
  unavailable: "Signing in with Discord isn’t set up yet. Try signing in with Plex.",
};

const STEPS = [
  { title: "Be in the Discord", body: "Plexbie lives in the household’s private Discord server." },
  { title: "Ask to join", body: "Sign in with Discord and ask to join. An admin reviews it. Not on Discord? Ask an admin for an invite link." },
  { title: "Accept the invite", body: "Once you’re approved, accept the invitation you’re sent, then come back here and sign in." },
];

/** Invite outcomes after which the next sign-in is a Plex one, so the Plex key leads:
 *  the way back from a failed invite, or the account that now has access. */
const PLEX_FIRST = ["email", "unconfirmed", "failed", "ok", "already"];

type Notice = { role: "alert" | "status"; lead: string; text: string; openPlex: boolean };
/** discord: Discord leads. plex: Plex leads (pink). plex-only: Discord can't work, Plex is the one sign-in (amber). */
type Lead = "discord" | "plex" | "plex-only";
type KeyId = "discord" | "plex" | "cta";
type KeyProps = Pick<AnchorHTMLAttributes<HTMLAnchorElement>,
  "aria-busy" | "onPointerEnter" | "onPointerLeave" | "onFocus" | "onBlur" | "onClick">;

/** What the last sign-in or invite came back with, as the screen's lower third. */
function noticeFor(search: string): Notice | null {
  const login = new URLSearchParams(search).get("login") ?? "";
  if (Object.hasOwn(LOGIN_MESSAGES, login)) {
    // Cancelling is something they did, not something that went wrong.
    return login === "cancelled"
      ? { role: "status", lead: "Sign-in cancelled", text: LOGIN_MESSAGES[login], openPlex: false }
      : { role: "alert", lead: "Sign-in problem", text: LOGIN_MESSAGES[login], openPlex: false };
  }
  const invite = inviteOutcome(search);
  if (!invite) return null;
  if (invite.tone === "error") return { role: "alert", lead: "Invite problem", text: invite.text, openPlex: false };
  return { role: "status", lead: invite.outcome === "already" ? "Already in" : "Invite accepted", text: invite.text, openPlex: true };
}

function leadFor(search: string): Lead {
  const q = new URLSearchParams(search);
  if (q.get("login") === "unavailable") return "plex-only";
  return PLEX_FIRST.includes(q.get("invite") ?? "") ? "plex" : "discord";
}

/** The set's rabbit-ears: an original drawing, slate on the studio navy. */
function Antennae() {
  return (
    <svg className="air-antennae" viewBox="0 0 200 58" aria-hidden="true" focusable="false">
      <line x1="100" y1="54" x2="44" y2="8" />
      <line x1="100" y1="54" x2="150" y2="14" />
      <circle cx="44" cy="8" r="5" />
      <circle cx="150" cy="14" r="5" />
      <path d="M78 58a22 14 0 0 1 44 0Z" />
    </svg>
  );
}

function Chevron() {
  return (
    <svg className="air-status__chev" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path d="M6 3l5 5-5 5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function Steps({ className }: { className: string }) {
  return (
    <ol className={className}>
      {STEPS.map((s) => (
        <li key={s.title}>
          <h3>{s.title}</h3>
          <p>{s.body}</p>
        </li>
      ))}
    </ol>
  );
}

export function Landing() {
  useTitle("Sign in");
  const reduced = useReducedMotion();
  const { pathname, search, hash } = useLocation();
  const next = signInReturn(pathname, search);
  const notice = noticeFor(search);
  const lead = leadFor(search);
  const invite = inviteOutcome(search)?.outcome;
  // One sentence per span, so a line breaks between them rather than inside one.
  const lede = invite === "ok" ? ["Your invite worked.", "Sign in with Plex to use Plexbie too."]
    : invite === "already" ? ["You’re already in.", "Sign in with Plex to use Plexbie too."]
    : ["Plexbie is your household’s Plex helper.", "Members sign in below."];

  const [ch, setCh] = useState<1 | 2>(() => (hash === "#getting-in" ? 2 : 1));
  const [preview, setPreview] = useState<string | null>(null);
  // The key pressed, while its sign-in is under way: the tally is live only then.
  const [busy, setBusy] = useState<KeyId | null>(null);
  const [plexStatus, setPlexStatus] = useState<{ from: KeyId; text: string; error: boolean } | null>(null);

  const tv = useRef<HTMLDivElement>(null);
  const channels = useRef<HTMLDivElement>(null);
  const ch2Title = useRef<HTMLHeadingElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const focusAfter = useRef<1 | 2 | null>(null);
  const flickTimer = useRef(0);

  const focusFor = useCallback((n: 1 | 2) => {
    if (n === 1) {
      toggle.current?.focus({ preventScroll: true });
      return;
    }
    ch2Title.current?.focus({ preventScroll: true });
    const r = tv.current?.getBoundingClientRect();
    if (r && (r.top < 0 || r.top > window.innerHeight * 0.4)) {
      tv.current?.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
    }
  }, [reduced]);

  // Focus moves once the channel is drawn (an inert channel can't take it).
  useEffect(() => {
    if (focusAfter.current !== ch) return;
    focusAfter.current = null;
    focusFor(ch);
  }, [ch, focusFor]);

  const tune = useCallback((n: 1 | 2) => {
    if (n === ch) {
      focusFor(n);
      return;
    }
    focusAfter.current = n;
    if (reduced) {
      setCh(n);
      return;
    }
    // A quick vertical-hold flick, and the picture changes as it closes.
    channels.current?.animate(
      [
        { transform: "none", opacity: 1 },
        { transform: "scaleY(0.03)", opacity: 0.4, offset: 0.4 },
        { transform: "none", opacity: 1 },
      ],
      { duration: 240, easing: `cubic-bezier(${EASE_OUT.join(",")})` },
    );
    window.clearTimeout(flickTimer.current);
    flickTimer.current = window.setTimeout(() => setCh(n), 96);
  }, [ch, focusFor, reduced]);

  useEffect(() => () => window.clearTimeout(flickTimer.current), []);

  useEffect(() => {
    if (ch !== 2) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") tune(1);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [ch, tune]);

  // Coming back with Back (or from a cancelled sign-in) shows the set at rest again.
  useEffect(() => {
    const onShow = (e: PageTransitionEvent) => {
      if (!e.persisted) return;
      setBusy(null);
      setPreview(null);
      setPlexStatus(null);
    };
    window.addEventListener("pageshow", onShow);
    return () => window.removeEventListener("pageshow", onShow);
  }, []);

  const names: Record<KeyId, string> = { discord: "Discord", plex: "Plex", cta: lead === "discord" ? "Discord" : "Plex" };
  const readout = ch === 2 ? "CH 2 · Getting in"
    : busy ? `CH 1 · Tuning in to ${names[busy]}`
    : preview ? `CH 1 · ${preview}`
    : "CH 1 · Sign in";

  /** What every sign-in key shares: the readout preview, and staying down once pressed. */
  const keyProps = (id: KeyId): KeyProps => ({
    "aria-busy": busy === id ? true : undefined,
    onPointerEnter: () => setPreview(names[id]),
    onPointerLeave: (e) => {
      if (document.activeElement !== e.currentTarget) setPreview(null);
    },
    onFocus: () => setPreview(names[id]),
    onBlur: () => setPreview(null),
    onClick: (e) => {
      // Already under way: a second click or Enter doesn't start it again.
      if (busy === id) {
        e.preventDefault();
        return;
      }
      setBusy(id);
      if (id !== "discord") setPlexStatus(null);
    },
  });
  /** A Plex sign-in in its own window reports here; when it stops waiting, its key comes back up. */
  const plexReport = (from: KeyId) => (text: string, waiting?: boolean) => {
    // Anything it says once it has stopped waiting is why it stopped: a failure.
    setPlexStatus(text ? { from, text, error: !waiting } : null);
    if (!waiting) setBusy((b) => (b === from ? null : b));
  };

  const tone = (id: "discord" | "plex") => {
    if (id === "discord") return lead === "discord" ? "pink" : lead === "plex" ? "plain" : "off";
    return lead === "discord" ? "plain" : lead === "plex" ? "pink" : "amber";
  };
  const discordTone = tone("discord");
  const plexTone = tone("plex");
  const plexHere = plexStatus?.from === "plex" ? plexStatus : null;
  const plexBelow = plexStatus?.from === "cta" ? plexStatus : null;

  const discordIcon = <DiscordMark />;
  const plexIcon = <PlayCircle size={22} aria-hidden />;

  return (
    <div className="air">
      <section className="air-stage" aria-label="Sign in">
        <motion.div
          className="air-set"
          initial={reduced ? false : { y: 16 }}
          animate={{ y: 0 }}
          transition={{ duration: 0.4, ease: EASE_OUT }}
        >
          <Antennae />
          <div ref={tv} className={`air-tv${busy ? " is-tuning" : ""}`} data-ch={ch}>
            <div className="air-tv__front">
              <div className="air-bezel">
                <div className="air-screen" id="air-screen">
                  <div className="air-osd caps">
                    <span className="air-osd__ch">{readout}</span>
                    <span className={`air-tally${busy ? " is-live" : ""}`}>
                      <span className="air-tally__dot" aria-hidden="true" />
                      <span aria-live="polite">{busy ? "Tuning in" : "Standby"}</span>
                    </span>
                  </div>
                  {/* Channel 2 leaves channel 1 (and its heading) inert: the page keeps its name. */}
                  {ch === 2 ? <h1 className="visually-hidden">Your household’s Plex, on air.</h1> : null}
                  <div className="air-channels" ref={channels}>
                    <div className={`air-ch air-ch1${notice ? " has-notice" : ""}`} inert={ch !== 1}>
                      <img
                        className="air-mascot" src="/brand/plexbie-256.webp" alt="" width={76} height={76}
                        fetchPriority="high" decoding="async"
                      />
                      <h1 className="air-title">Your household’s Plex, on air.</h1>
                      <p className="air-lede">
                        {lede.map((sentence, i) => (
                          <Fragment key={sentence}>{i ? " " : null}<span>{sentence}</span></Fragment>
                        ))}
                      </p>
                      {notice ? (
                        <p className={`air-notice air-notice--${notice.role}`} role={notice.role}>
                          <span className="air-notice__lead caps">{notice.lead}</span>
                          <span className="air-notice__body">
                            {notice.text}
                            {notice.openPlex ? <>{" "}<a href="https://app.plex.tv">Open Plex</a></> : null}
                          </span>
                        </p>
                      ) : null}
                    </div>
                    <div className="air-ch air-ch2" inert={ch !== 2}>
                      <h2 className="air-ch2__title" ref={ch2Title} tabIndex={-1}>Getting in</h2>
                      <Steps className="air-steps" />
                    </div>
                  </div>
                </div>
              </div>
              <div className="air-side" aria-hidden="true">
                <div className="air-knobs">
                  <span className="air-knob air-knob--ch" />
                  <span className="air-knob air-knob--vol" />
                </div>
                <div className="air-grille">{Array.from({ length: 15 }, (_, i) => <i key={i} />)}</div>
              </div>
            </div>

            <div className="air-deck">
              <div className="air-seg" role="group" aria-label="Sign in">
                {discordTone === "off" ? (
                  <a className="air-key air-key--off" role="link" aria-disabled="true" aria-describedby="air-leg-discord">
                    {discordIcon} Sign in with Discord
                  </a>
                ) : (
                  <a
                    className={`air-key air-key--${discordTone}`} href={loginUrl(next, "discord")}
                    aria-describedby="air-leg-discord" {...keyProps("discord")}
                  >
                    {discordIcon} Sign in with Discord
                  </a>
                )}
                <p className="air-legend" id="air-leg-discord">
                  {discordTone === "off" ? "Discord sign-in isn’t set up yet" : "For people in the household’s Discord"}
                </p>
                <PlexSignIn
                  className={`air-key air-key--${plexTone} air-key--plex`} next={next}
                  aria-describedby="air-leg-plex" onStatus={plexReport("plex")} {...keyProps("plex")}
                >
                  {plexIcon} Sign in with Plex
                </PlexSignIn>
                {/* A failure is a new alert (so it's read out at once); progress updates the legend in place. */}
                {plexHere?.error ? (
                  <p className="air-legend is-error" id="air-leg-plex" role="alert" key="error">{plexHere.text}</p>
                ) : (
                  <p className="air-legend" id="air-leg-plex" role="status" key="status">{plexHere?.text ?? "Already watching on Plex"}</p>
                )}
              </div>
              <div className="air-status">
                <button
                  ref={toggle} type="button" className="air-status__btn"
                  aria-controls="air-screen" aria-expanded={ch === 2}
                  onClick={() => tune(ch === 1 ? 2 : 1)}
                >
                  <span className="air-chip caps">{ch === 1 ? "CH 2" : "CH 1"}</span>
                  {ch === 1 ? "How to get in" : "Back to sign-in"}
                  <Chevron />
                </button>
              </div>
            </div>
          </div>
          <div className="air-feet" aria-hidden="true"><i /><i /></div>
        </motion.div>
      </section>

      <section className="air-band" id="getting-in" aria-labelledby="air-band-title">
        <h2 className="air-band__title caps" id="air-band-title">Getting in</h2>
        <Steps className="air-band__steps" />
        <CtaKey lead={lead} next={next} keyProps={keyProps("cta")} onStatus={plexReport("cta")}
          icon={lead === "discord" ? discordIcon : plexIcon} />
        {plexBelow ? (
          <p className={`air-band__status${plexBelow.error ? " is-error" : ""}`} role={plexBelow.error ? "alert" : "status"}
            key={plexBelow.error ? "error" : "status"}>
            {plexBelow.text}
          </p>
        ) : null}
      </section>
    </div>
  );
}

/** The sign-in under the steps: the same key the set leads with. */
function CtaKey({ lead, next, keyProps, onStatus, icon }: {
  lead: Lead; next: string; icon: ReactNode;
  keyProps: KeyProps; onStatus: (text: string, waiting?: boolean) => void;
}) {
  if (lead === "discord") {
    return (
      <a className="air-key air-key--pink air-band__cta" href={loginUrl(next, "discord")} {...keyProps}>
        {icon} Sign in with Discord
      </a>
    );
  }
  return (
    <PlexSignIn
      className={`air-key air-key--${lead === "plex" ? "pink" : "amber"} air-band__cta`} next={next}
      onStatus={onStatus} {...keyProps}
    >
      {icon} Sign in with Plex
    </PlexSignIn>
  );
}
