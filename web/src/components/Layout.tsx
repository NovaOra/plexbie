import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { Bell, CalendarClock, House, Library, LogOut, Radio, Search, Settings } from "./icons";
import { api, DEMO, SAMPLE } from "../api/client";
import type { ServerStatus, Session } from "../api/types";
import { PROJECT_SITE, SITE } from "../site";
import { useLoad } from "./ui";
import { useBackToClose } from "./overlay";

/* ---------------------------------------------------------------- session */

interface SessionState {
  session: Session | null;
  status?: ServerStatus;
  /** The server status couldn't be loaded (as opposed to: not loaded yet). */
  statusFailed?: boolean;
  retryStatus?: () => void;
}
const SessionContext = createContext<SessionState>({ session: null });
export const useSession = () => useContext(SessionContext);

/* ----------------------------------------------------------------- shell */

const NAV = [
  { to: "/", label: "Home", icon: House, end: true },
  { to: "/library", label: "Library", icon: Library },
  { to: "/search", label: "Request", icon: Search },
  { to: "/schedule", label: "My requests", short: "Requests", icon: CalendarClock },
  { to: "/channel", label: "Channel", icon: Radio },
];

function initials(name: string) {
  return name.split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();
}

function Account({ session }: { session: Session }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const location = useLocation();
  useEffect(() => setOpen(false), [location.pathname]);
  useBackToClose(open, () => setOpen(false));
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) {
        setOpen(false);
        // Escape hands focus back to the button that opened it, not the page.
        if (e instanceof KeyboardEvent) ref.current?.querySelector<HTMLButtonElement>(".menu-button")?.focus();
      }
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const { user } = session;
  return (
    <div ref={ref}>
      <button className="menu-button" aria-expanded={open} aria-controls="account-menu" onClick={() => setOpen((o) => !o)}>
        <span className="avatar">
          {user.avatar ? <img src={user.avatar} alt="" /> : initials(user.name)}
        </span>
        <span className="visually-hidden">Account menu for {user.name}</span>
      </button>
      {/* Always there, hidden when closed, so closing can fade out (styles.css). */}
      <div className="account-menu" id="account-menu" hidden={!open}>
        <div className="account-menu__who">
          <div style={{ fontWeight: 700 }}>{user.name}</div>
          <div className="muted" style={{ fontSize: "0.86rem" }}>
            {session.member ? "Plex member" : "Not on Plex yet"}
          </div>
        </div>
        {session.member ? (
          <Link to="/alerts"><Bell size={18} aria-hidden /> Alerts</Link>
        ) : null}
        {session.admin ? (
          <Link to="/manage"><Settings size={18} aria-hidden /> Manage</Link>
        ) : null}
        <button
          type="button"
          onClick={async () => {
            await api.logout();
            window.location.assign(SAMPLE ? "/?as=guest" : "/");
          }}
        >
          <LogOut size={18} aria-hidden /> Log out
        </button>
      </div>
    </div>
  );
}

function SampleSwitch() {
  const current = (() => {
    try { return localStorage.getItem("plexbie.sample.persona") || "member"; } catch { return "member"; }
  })();
  const options = [["guest", "Logged out"], ["visitor", "Not on Plex"], ["member", "Member"]] as const;
  return (
    <nav className="sample-switch" aria-label="Sample data persona">
      {DEMO && <span className="sample-switch__label">Demo · see it as</span>}
      {options.map(([id, label]) => (
        <a key={id} href={`/?as=${id}`} aria-current={current === id}>{label}</a>
      ))}
    </nav>
  );
}

/** Pages that open on a full-bleed stage let the masthead float over it until scrolled. */
function useStageChrome(onStage: boolean) {
  const { pathname } = useLocation();
  const stage = onStage && pathname === "/";
  const [scrolled, setScrolled] = useState(false);
  // Before the first paint: applied after it, the page jumped up under the header.
  useLayoutEffect(() => {
    document.body.classList.toggle("has-stage", stage);
    if (!stage) return;
    const on = () => setScrolled(window.scrollY > 40);
    on();
    window.addEventListener("scroll", on, { passive: true });
    return () => window.removeEventListener("scroll", on);
  }, [stage]);
  return scrolled;
}

/** After moving to another page, keyboard and screen-reader users start at its
 *  heading, as they would on a full page load, not on the link they pressed. */
function usePageFocus() {
  const { pathname } = useLocation();
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    const t = window.setTimeout(() => {
      const h1 = document.querySelector<HTMLElement>("main h1");
      if (!h1) return;
      if (!h1.hasAttribute("tabindex")) h1.setAttribute("tabindex", "-1");
      h1.focus({ preventScroll: true });
    }, 60);
    return () => window.clearTimeout(t);
  }, [pathname]);
}

/** Pages anyone may read without signing in; everything else is the household's own. */
const OPEN_PAGES = ["/privacy", "/terms", "/invite"];

export function Layout({ children }: { children?: ReactNode }) {
  const session = useLoad(() => api.session());
  usePageFocus();
  const status = useLoad(() => (session.data?.member ? api.status() : Promise.resolve(undefined)), [session.data?.member]);
  const s = session.data ?? null;
  // Only the member home opens on a stage; the sign-in and join pages show nothing
  // about the library, so they have none.
  const scrolled = useStageChrome(!session.loading && !!s?.member);
  const { pathname } = useLocation();
  const inApp = !OPEN_PAGES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
  const member = inApp && !!s?.member;

  // The household's pages need to know who's signed in before they can show anything;
  // the open pages don't, so they draw at once (the account corner fills in after).
  if (session.loading && inApp) return <div className="boot" aria-busy="true" />;

  return (
    <SessionContext.Provider value={{ session: s, status: status.data, statusFailed: !!status.error, retryStatus: status.reload }}>
      {s?.preview && inApp ? (
        <div className="preview-ribbon" role="note">Preview with your real data. Read-only: requesting and joining are switched off.</div>
      ) : null}
      <a className="skip-link" href="#main">Skip to content</a>
      <header className={`masthead${scrolled ? " is-scrolled" : ""}`}>
        <div className="shell masthead__row">
          <Link to="/" className="brand" aria-label="Plexbie home">
            <img src="/brand/plexbie-64.png" alt="" width={34} height={34} />
            Plexbie
          </Link>
          {member && s ? (
            <nav className="masthead__nav" aria-label="Main">
              {NAV.map(({ to, label, end }) => (
                <NavLink key={to} to={to} end={end}>{label}</NavLink>
              ))}
              {s.admin ? <NavLink to="/manage">Manage</NavLink> : null}
            </nav>
          ) : null}
          <div className="masthead__end">
            {member && status.data ? (
              <span className={`tally caps${status.data.online ? "" : " tally--off"}`}>
                {status.data.online ? "On air" : "Off air"}
              </span>
            ) : member && status.loading ? (
              <span className="tally caps" style={{ visibility: "hidden" }} aria-hidden>On air</span>
            ) : null}
            {/* The sign-in page is all sign-in buttons already: no third one up here. */}
            {session.loading ? null : s ? (
              <Account session={s} />
            ) : inApp ? null : (
              <Link className="btn btn--primary" style={{ minHeight: 44, padding: "0 14px" }} to="/">
                Log in
              </Link>
            )}
          </div>
        </div>
      </header>

      <main id="main" tabIndex={-1}>{children ?? <Outlet />}</main>

      <footer className="shell footer">
        <nav className="footer__links" aria-label="Site information">
          <Link to="/privacy">Privacy</Link>
          <Link to="/terms">Terms</Link>
          {SITE.contact ? <a href={`mailto:${SITE.contact}`}>{SITE.contact}</a> : null}
        </nav>
        <p className="footer__powered">
          <a href={PROJECT_SITE} rel="noopener">
            <img src="/brand/plexbie-64.png" alt="" width={18} height={18} /> Powered by Plexbie
          </a>
        </p>
        <p>
          Plexbie is free, open-source software, not affiliated with or endorsed by Plex, Inc. or Discord Inc.
        </p>
        {member ? (
          <p className="footer__credit">
            Film and TV data and artwork from TMDB. This product uses the TMDB API but is not endorsed or certified by TMDB.
            Book data and covers from Open Library.
          </p>
        ) : null}
      </footer>

      {member && s ? (
        <nav className="tabbar" aria-label="Main">
          {NAV.map(({ to, label, short, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end}>
              <Icon size={22} aria-hidden />
              {short ?? label}
            </NavLink>
          ))}
          {s.admin ? (
            <NavLink to="/manage">
              <Settings size={22} aria-hidden />
              Manage
            </NavLink>
          ) : null}
        </nav>
      ) : null}

      {SAMPLE ? <SampleSwitch /> : null}
    </SessionContext.Provider>
  );
}
