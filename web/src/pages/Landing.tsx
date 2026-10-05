import { motion, useReducedMotion } from "motion/react";
import { Link, useSearchParams } from "react-router-dom";
import { PlayCircle } from "../components/icons";
import { loginUrl } from "../api/client";
import { EASE_OUT, Journey, useRise } from "../components/motion";
import { InviteOutcome } from "./InvitePage";
import { DiscordMark, useTitle } from "../components/ui";
import { PlexSignIn } from "../components/PlexSignIn";

// The public face says only what Plexbie is and how to sign in. Nothing about
// the server, its library or its people is shown before login (PRODUCT.md).

function Kinetic({ lines }: { lines: string[] }) {
  const reduced = useReducedMotion();
  return (
    <h1 className="display landing-hero__title">
      {lines.map((line, i) => (
        <span className="kinetic" key={line}>
          <motion.span
            initial={reduced ? false : { y: "105%" }}
            animate={{ y: 0 }}
            transition={{ duration: 0.9, ease: EASE_OUT, delay: 0.15 + i * 0.12 }}
          >
            {line}
          </motion.span>
        </span>
      ))}
    </h1>
  );
}

const LOGIN_MESSAGES: Record<string, string> = {
  failed: "Signing in didn’t work just now. Try again in a moment.",
  expired: "That sign-in took too long, so it expired. Try again.",
  cancelled: "Sign-in was cancelled. Nothing was changed.",
  busy: "Lots of sign-ins just now. Wait a few minutes and try again.",
  unavailable: "Logging in with Discord isn’t set up yet. Try signing in with Plex.",
};

function LoginNotice() {
  const [params] = useSearchParams();
  const message = LOGIN_MESSAGES[params.get("login") ?? ""];
  return message ? <p className="field__error" role="alert">{message}</p> : null;
}

const ACCESS = [
  { title: "Be in the Discord", body: "Plexbie lives in the household's private Discord server. If a link in there brought you here, you're set." },
  { title: "Ask to join", body: "Log in with Discord and ask to join; an admin reviews every request. Not on Discord? An admin can send you an invite link instead." },
  { title: "Accept the invite", body: "Once you're approved, accept the invitation you're sent, then come back here and log in." },
];

export function Landing() {
  useTitle("Log in");
  const reduced = useReducedMotion();
  const rise = useRise(12, 0.45);

  return (
    <div className="shell landing landing--private">
      <section className="landing-hero">
        <div className="landing-hero__copy">
          <Kinetic lines={["Members", "only."]} />
          <motion.p {...rise(0.225)} className="landing__lede">
            This is the private members’ area for one household’s Plex server.
            Sign in with your Discord account to continue.
          </motion.p>
          <LoginNotice />
          <InviteOutcome />
          <motion.div {...rise(0.3)} className="landing__actions landing__signin">
            <a className="btn btn--primary btn--discord" href={loginUrl("/", "discord")}>
              <DiscordMark /> Log in with Discord
            </a>
            <PlexSignIn className="btn btn--plex">
              <PlayCircle size={20} aria-hidden /> Sign in with Plex
            </PlexSignIn>
          </motion.div>
          <motion.p {...rise(0.35)} className="muted" style={{ fontSize: "0.9rem" }}>
            Already watching without Discord? Sign in with your Plex account. New here without Discord? Ask for an invite link. <a href="#access">How to get access</a>
          </motion.p>
        </div>
        <motion.div
          className="landing-hero__mark"
          initial={reduced ? false : { scale: 0.96, rotate: -3 }}
          animate={{ scale: 1, rotate: 0 }}
          transition={{ duration: 0.5, ease: EASE_OUT }}
        >
          <img src="/brand/plexbie-512.webp" alt="Plexbie logo" width={360} height={360} fetchPriority="high" />
        </motion.div>
      </section>

      <section className="access" id="access" aria-labelledby="access-h" style={{ scrollMarginTop: 80 }}>
        <div style={{ display: "grid", gap: 14 }}>
          <h2 id="access-h" className="display" style={{ fontSize: "clamp(2.2rem, 1.6rem + 2.4vw, 3.6rem)" }}>Getting in</h2>
          <p className="muted" style={{ maxWidth: "38ch" }}>
            Access is by invitation only. If you weren’t invited, this site isn’t for you, and that’s fine.
          </p>
        </div>
        <div className="access__steps">
          <Journey stage={ACCESS.length - 1} steps={ACCESS.map((a) => a.title)} />
          <ol className="access__copy" style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {ACCESS.map((a) => (
              <li key={a.title}><h3>{a.title}</h3><p>{a.body}</p></li>
            ))}
          </ol>
        </div>
      </section>

      <p className="muted" style={{ fontSize: "0.92rem" }}>
        Read the <Link to="/privacy">privacy policy</Link> and <Link to="/terms">terms of use</Link>.
      </p>
    </div>
  );
}
