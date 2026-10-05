import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import { useSession } from "../components/Layout";
import { Mascot } from "../components/Mascot";
import { InviteOutcome } from "./InvitePage";
import { NowStrap } from "./Home";
import { useTitle } from "../components/ui";

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function Join() {
  useTitle("Join");
  const { session } = useSession();
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent" | "error">(session?.joinPending ? "sent" : "idle");
  const [touched, setTouched] = useState(false);
  const [problem, setProblem] = useState("");
  const viaPlex = session?.user.via === "plex";
  // The form is only there for a Discord sign-in (a Plex one joins by invite link).
  const valid = EMAIL.test(email.trim());

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (!valid) { document.getElementById("email")?.focus(); return; }
    setState("sending");
    try {
      await api.join(email.trim());
      setState("sent");
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "");
      setState("error");
    }
  };

  const first = session?.user.name.split(" ")[0];
  // Why there's no form to fill in, when there isn't one.
  const blocked = session?.accessUnknown
    ? "Plexbie can’t reach Plex right now to check whether you already have access. Give it a minute and reload this page."
    : viaPlex
      ? `You’re signed in as ${session?.plexName ?? "your Plex account"}, which isn’t shared on this server. Joining `
        + "without Discord is by invite only: ask whoever runs the server to send you an invite link, then open it and "
        + "sign in with this account."
      : session && !session.inGuild
        ? "This Discord account isn’t in the household server, so Plexbie can’t ask for you from here. Join the "
          + "Discord server first, or ask whoever runs it for an invite link."
        : null;

  return (
    <div className="shell page">
      <NowStrap />
      <div className="split">
        {state === "sent" ? (
          <section className="moment moment--start" role="status">
            <Mascot />
            <h1>You’re in the queue.</h1>
            <p style={{ maxWidth: "48ch" }}>
              An admin will look at it soon. Then Plex emails you an invitation{viaPlex ? "" : ", and Plexbie sends you a DM"}.
              Accept the invite and come back here to request things.
            </p>
          </section>
        ) : (
          <section style={{ display: "grid", gap: 20 }}>
            <h1>{first ? `${first}, let’s get you on Plex.` : "Let’s get you on Plex."}</h1>
            <InviteOutcome />
            {blocked ? (
              <p className="muted join__lede">{blocked}</p>
            ) : (
              <>
                <p className="muted join__lede">
                  You’re in the Discord, but not on the Plex server yet. Give the email you use for Plex (or want to use)
                  and an admin will send you an invite. It’s the same as /join-plex.
                </p>
                <form onSubmit={submit} style={{ display: "grid", gap: 14, maxWidth: 460 }} noValidate>
                  <div className="field">
                    <label htmlFor="email">Email for Plex</label>
                    <input
                      id="email" type="email" inputMode="email" autoComplete="email" placeholder="you@example.com"
                      value={email} onChange={(e) => setEmail(e.target.value)} onBlur={() => setTouched(true)}
                      aria-invalid={touched && !valid} aria-describedby="email-hint"
                    />
                    {touched && !valid ? (
                      <span className="field__error" id="email-hint">That doesn’t look like an email address.</span>
                    ) : (
                      <span className="field__hint" id="email-hint">Only the admins see it, and only to send the Plex invite.</span>
                    )}
                  </div>
                  {state === "error" ? (
                    <p className="field__error" role="alert">{problem || "That didn’t send. Try again in a minute."}</p>
                  ) : null}
                  <button className="btn btn--primary" type="submit" disabled={state === "sending" || !!session?.preview}>
                    {state === "sending" ? "Sending…" : "Ask to join"}
                  </button>
                </form>
              </>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
