import type { ReactNode } from "react";
import { Link, Outlet } from "react-router-dom";
import { CONTACT, GITHUB, OPERATOR, REPO_PUBLIC, SUPPORT } from "./links";

/** The project site's frame: what Plexbie is, where the code is, how to support it.
 *  No sign-in here: each household signs in on its own Plexbie. */
export function ProjectLayout({ children }: { children?: ReactNode }) {
  return (
    <>
      <a className="skip-link" href="#main">Skip to content</a>
      <header className="masthead">
        <div className="shell masthead__row">
          <Link to="/" className="brand" aria-label="Plexbie home">
            <img src="/brand/plexbie-64.png" alt="" width={34} height={34} />
            Plexbie
          </Link>
          <nav className="masthead__nav" aria-label="Project">
            <a href="/#channels">Features</a>
            <a href="/#self-host">Self-host</a>
            {REPO_PUBLIC ? <a href={GITHUB} rel="noopener">GitHub</a> : null}
          </nav>
        </div>
      </header>

      <main id="main" tabIndex={-1}>{children ?? <Outlet />}</main>

      <footer className="shell footer">
        <nav className="footer__links" aria-label="Site information">
          <Link to="/privacy">Privacy</Link>
          <Link to="/terms">Terms</Link>
          <a href={`mailto:${CONTACT}`}>{CONTACT}</a>
        </nav>
        <p>
          Plexbie is an independent, open-source project by {OPERATOR}, and is not affiliated with or endorsed by
          Plex, Inc. or Discord Inc.{REPO_PUBLIC ? <> <a href={GITHUB} rel="noopener">Source on GitHub</a>.</> : null}
        </p>
        <p className="footer__support">
          Plexbie is free and built in spare time. To support its development, chip in on{" "}
          <a href={SUPPORT.kofi} rel="noopener">Ko-fi</a> or <a href={SUPPORT.coffee} rel="noopener">Buy Me a Coffee</a>.
          Tips go to building Plexbie, not to anyone’s Plex server.
        </p>
      </footer>
    </>
  );
}
