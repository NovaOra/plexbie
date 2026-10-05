import { useState, type ReactNode } from "react";
import { loginUrl, plexSignIn } from "../api/client";

/** "Sign in with Plex": a small Plex window, watched from this tab (see plexSignIn). */
export function PlexSignIn({ className, next = "/", invite = false, children }:
  { className: string; next?: string; invite?: boolean; children: ReactNode }) {
  const [status, setStatus] = useState("");
  const href = invite ? `${loginUrl(next, "plex")}&invite=1` : loginUrl(next, "plex");
  return (
    <>
      <a className={className} href={href} onClick={(e) => plexSignIn(e, next, invite, setStatus)}>{children}</a>
      {status ? (
        <p className="muted plex-status" role="status" style={{ fontSize: "0.9rem", flexBasis: "100%", margin: 0 }}>
          {status}
        </p>
      ) : null}
    </>
  );
}
