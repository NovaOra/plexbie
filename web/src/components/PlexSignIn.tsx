import { useState, type AnchorHTMLAttributes, type ReactNode } from "react";
import { loginUrl, plexSignIn } from "../api/client";

type Props = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href" | "className" | "children"> & {
  className: string;
  next?: string;
  invite?: boolean;
  children: ReactNode;
  /** Show the sign-in's progress yourself (see plexSignIn); without it, a line follows the link. */
  onStatus?: (text: string, waiting?: boolean) => void;
};

/** "Sign in with Plex": a small Plex window, watched from this tab (see plexSignIn). */
export function PlexSignIn({ className, next = "/", invite = false, children, onStatus, onClick, ...rest }: Props) {
  const [status, setStatus] = useState("");
  const href = invite ? `${loginUrl(next, "plex")}&invite=1` : loginUrl(next, "plex");
  return (
    <>
      <a
        {...rest}
        className={className}
        href={href}
        onClick={(e) => {
          onClick?.(e);
          if (!e.defaultPrevented) plexSignIn(e, next, invite, onStatus ?? setStatus);
        }}
      >
        {children}
      </a>
      {status ? (
        <p className="muted plex-status text-small" role="status">
          {status}
        </p>
      ) : null}
    </>
  );
}
