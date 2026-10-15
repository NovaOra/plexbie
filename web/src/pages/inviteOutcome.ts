// What an invite sign-in came back with: the server sends people on with ?invite=…

const OUTCOMES: Record<string, { tone: "ok" | "error"; text: string }> = {
  ok: { tone: "ok", text: "You’re in. Welcome to the household Plex! Open the Plex app and it’s all there." },
  already: { tone: "ok", text: "You already have access, so the invite wasn’t needed. Welcome back." },
  email: { tone: "error", text: "That invite is for a different Plex account. Sign in with the account it was made for, or ask for a new link." },
  unconfirmed: { tone: "error", text: "Confirm your email with Plex first, then open your invite link again; it still works." },
  invalid: { tone: "error", text: "That invite link doesn’t work any more. Ask whoever sent it for a new one." },
  failed: { tone: "error", text: "Plex didn’t accept the invite just now. Open your invite link again in a minute; it still works." },
};

/** What an invite sign-in came back with (?invite=…), or null when the address has none. */
export function inviteOutcome(search: string): { outcome: string; tone: "ok" | "error"; text: string } | null {
  const outcome = new URLSearchParams(search).get("invite");
  return outcome && Object.hasOwn(OUTCOMES, outcome) ? { outcome, ...OUTCOMES[outcome] } : null;
}
