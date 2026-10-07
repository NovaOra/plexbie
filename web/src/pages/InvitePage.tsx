import { motion } from "motion/react";
import { Clock, Eye, Heart, Hourglass, Lock, MonitorPlay, PlayCircle, Radio, Search } from "../components/icons";
import { Link } from "react-router-dom";
import { PlexSignIn } from "../components/PlexSignIn";
import { api, SAMPLE } from "../api/client";
import type { InviteInfo } from "../api/types";
import { useRise } from "../components/motion";
import { Mascot } from "../components/Mascot";
import { PROJECT_SITE, SITE } from "../site";
import { OffAir, useLoad, useTitle } from "../components/ui";

const STEPS = [
  { title: "Sign in with Plex, or make an account", body: "The button opens Plex’s own page. Already on Plex? Sign in. New to it? Choose to sign up there: a Plex account is free and takes a minute with your email." },
  { title: "You’re let in", body: "Plexbie accepts the invite for you, so there’s no email to wait for." },
  { title: "Start watching", body: "Open the Plex app on your TV, phone or computer. The household’s films and shows are there." },
];

const OFFERS = [
  { icon: MonitorPlay, title: "Films and TV, on any screen", body: "Stream the household’s library in the Plex app on your TV, phone, tablet or computer, or in a web browser." },
  { icon: Search, title: "Ask for what’s missing", body: "Request a film, a show or a book here. An admin approves it, and you can follow it until it arrives." },
  { icon: Radio, title: "See what’s on", body: "What just arrived, what’s on its way, and what the household is watching right now." },
];

/** Where an invite link lands: who it's from, what it is, what happens next. */
export function InvitePage() {
  useTitle("You’re invited");
  const res = useLoad<InviteInfo>(() => api.invite());
  const rise = useRise(10);

  if (res.loading) {
    // The card's own shape and width, so nothing jumps when it arrives.
    return (
      <div className="shell page invite">
        <div className="invite__card" aria-busy="true" aria-label="Loading your invite">
          <div className="skeleton" style={{ width: 72, height: 72, borderRadius: 18 }} />
          <div className="skeleton" style={{ height: "2.6rem", width: "80%" }} />
          <div className="skeleton" style={{ height: 16, width: "100%" }} />
          <div className="skeleton" style={{ height: 16, width: "70%" }} />
          <div className="skeleton" style={{ height: 52, width: "100%" }} />
        </div>
      </div>
    );
  }
  const info = res.data;

  if (res.error) {
    // An outage isn't a dead link: say the invite is fine and to come back.
    return (
      <div className="shell page invite">
        <motion.div className="invite__card" {...rise(0)}>
          <Mascot />
          <h1>Plexbie can’t be reached right now</h1>
          <p className="muted">Your invite still works. Try this page again in a few minutes.</p>
          <OffAir compact onRetry={res.reload}>No signal from the household’s server.</OffAir>
        </motion.div>
      </div>
    );
  }

  if (!info?.valid) {
    return (
      <div className="shell page invite">
        <motion.div className="invite__card" {...rise(0)}>
          <Mascot />
          <h1>This invite link doesn’t work</h1>
          <p className="muted">
            It may have been used already, cancelled, or expired. Ask whoever sent it for a new one.
          </p>
          <a href={PROJECT_SITE} className="btn btn--quiet" rel="noopener">What is Plexbie?</a>
        </motion.div>
      </div>
    );
  }

  const until = info.expiresAt ? new Date(info.expiresAt).toLocaleDateString(undefined, { month: "long", day: "numeric" }) : null;
  const sampleSignIn = "/?as=member&invite=ok";

  return (
    <div className="shell page invite">
      <motion.div className="invite__card" {...rise(0)}>
        <Mascot label="Plexbie" />
        <h1>{info.label ? `${info.label}, you’re invited.` : "You’re invited."}</h1>
        <p className="invite__lede">
          {info.inviter ?? "Someone in the household"} invited you to the household Plex: their films and shows, on any screen.
        </p>
        {SAMPLE ? (
          <a className="btn btn--plex btn--big invite__go" href={sampleSignIn}>
            <PlayCircle size={22} aria-hidden /> Sign in with Plex
          </a>
        ) : (
          <PlexSignIn className="btn btn--plex btn--big invite__go" invite>
            <PlayCircle size={22} aria-hidden /> Sign in with Plex
          </PlexSignIn>
        )}
        {info.emailLocked ? (
          <p className="invite__lock"><Lock size={14} aria-hidden /> This invite is for one Plex account. Sign in with the one {info.inviter ?? "they"} have the email for.</p>
        ) : null}
        <p className="muted invite__small">
          No Plex account? You can make a free one on the next page.{until ? ` This link works once, until ${until}.` : ""}
        </p>
      </motion.div>

      <motion.section className="invite__section" {...rise(0.12)} aria-labelledby="invite-what">
        <h2 id="invite-what">What you’re joining</h2>
        <p className="muted">
          A private Plex server that one household runs for its friends and family. It isn’t a business: no ads, nothing
          sold, and no Discord needed. Plexbie is the household’s helper that runs it.
        </p>
        <ul className="invite__offers">
          {OFFERS.map(({ icon: Icon, title, body }) => (
            <li key={title}>
              <span className="invite__icon" aria-hidden><Icon size={20} /></span>
              <div><b>{title}</b><p className="muted">{body}</p></div>
            </li>
          ))}
        </ul>
      </motion.section>

      <motion.section className="invite__section" {...rise(0.18)} aria-labelledby="invite-how">
        <h2 id="invite-how">How joining works</h2>
        <ol className="invite__steps">
          {STEPS.map((s, i) => (
            <li key={s.title}>
              <span className="invite__n" aria-hidden>{i + 1}</span>
              <div><b>{s.title}</b><p className="muted">{s.body}</p></div>
            </li>
          ))}
        </ol>
      </motion.section>

      <motion.section className="invite__section" {...rise(0.24)} aria-labelledby="invite-know">
        <h2 id="invite-know">Good to know</h2>
        <ul className="invite__offers invite__offers--rules">
          <li>
            <span className="invite__icon" aria-hidden><Clock size={20} /></span>
            <div>
              <b>Keep watching to keep your spot</b>
              <p className="muted">
                Accounts that don’t watch anything for {info.inactivityDays ?? 30} days are removed to make room, with a
                reminder at {info.warnDays ?? 25} days. Watching anything resets the clock.
              </p>
            </div>
          </li>
          <li>
            <span className="invite__icon" aria-hidden><Eye size={20} /></span>
            <div>
              <b>The household can see what you watch</b>
              <p className="muted">Other members see what you’re watching right now and your total watch time, a bit like a shared living room.</p>
            </div>
          </li>
          <li>
            <span className="invite__icon" aria-hidden><Hourglass size={20} /></span>
            <div>
              <b>Unwatched titles move on</b>
              <p className="muted">Films and shows nobody has watched in a long while are cleared out to make space. If something you wanted is gone, just ask for it again.</p>
            </div>
          </li>
          <li>
            <span className="invite__icon" aria-hidden><Heart size={20} /></span>
            <div>
              <b>You can leave any time</b>
              <p className="muted">Ask {info.inviter ?? "whoever invited you"}{SITE.contact ? `, or email ${SITE.contact}` : ""}, and your access and details are removed.</p>
            </div>
          </li>
        </ul>
      </motion.section>

      <motion.div className="invite__again" {...rise(0.3)}>
        {SAMPLE ? (
          <a className="btn btn--plex btn--big invite__go" href={sampleSignIn}>
            <PlayCircle size={22} aria-hidden /> Sign in or sign up with Plex
          </a>
        ) : (
          <PlexSignIn className="btn btn--plex btn--big invite__go" invite>
            <PlayCircle size={22} aria-hidden /> Sign in or sign up with Plex
          </PlexSignIn>
        )}
        <p className="muted invite__privacy">
          You type your password on plex.tv, never here. Plexbie uses your sign-in once, to accept the invite, then signs
          itself out of your Plex account. It keeps only your Plex name and email, to manage your access.
          {" "}<Link to="/privacy">Privacy policy</Link> · <Link to="/terms">Terms</Link>
        </p>
      </motion.div>
    </div>
  );
}

const OUTCOMES: Record<string, { tone: "ok" | "error"; text: string }> = {
  ok: { tone: "ok", text: "You’re in. Welcome to the household Plex! Open the Plex app and it’s all there." },
  already: { tone: "ok", text: "You already have access, so the invite wasn’t needed. Welcome back." },
  email: { tone: "error", text: "That invite is for a different Plex account. Sign in with the account it was made for, or ask for a new link." },
  invalid: { tone: "error", text: "That invite link doesn’t work any more. Ask whoever sent it for a new one." },
  failed: { tone: "error", text: "Plex didn’t accept the invite just now. Open your invite link again in a minute; it still works." },
};

/** After an invite sign-in: what happened, in one line. Shown wherever they land. */
export function InviteOutcome() {
  const outcome = new URLSearchParams(location.search).get("invite");
  const m = outcome ? OUTCOMES[outcome] : null;
  if (!m) return null;
  return <p className={m.tone === "ok" ? "invite__done" : "field__error"} role={m.tone === "ok" ? "status" : "alert"}>{m.text}</p>;
}
