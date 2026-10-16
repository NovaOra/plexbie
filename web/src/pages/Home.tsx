import { useRef, useState, type FormEvent, useEffect } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence } from "motion/react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { LifeBuoy, Search } from "../components/icons";
import { useMedia } from "../components/useMedia";
import { StageSkeleton } from "../components/StageSkeleton";
import { api } from "../api/client";
import { NewTicketSheet, TicketSheet } from "../components/HelpSheet";
import { afterOverlay } from "../components/overlay";
import { asMemberTicket, ticketHeading } from "../components/tickets";
import { useSession } from "../components/Layout";
import { LiveProgress, RequestSlot, Stage, Stagger, Ticker } from "../components/motion";
import { InviteOutcome } from "./InvitePage";
import { AlertsPanel } from "../components/Alerts";
import { Art, OffAir, SlotSkeletons, formatSlot, since, stageLabel, stageHelp, useLoad, whileActive, useTitle, Section, Moment, type Loaded } from "../components/ui";
import type { Community, MemberTicket, MyTicket } from "../api/types";

export function NowStrap() {
  const { status, statusFailed, session } = useSession();
  // The server's status is for members; someone not on Plex yet just doesn't see this strip
  // (and isn't sent asking for what's new, which the server would refuse them).
  const arrivals = useLoad(() => (session?.member ? api.arrivals() : Promise.resolve(null)), [session?.member]);
  if (!session?.member) return null;
  if (!status && statusFailed) {
    return (
      <div className="now" role="status">
        <span className="now__tab caps is-off">No signal</span>
        <span className="now__item">Couldn’t reach Plexbie just now.</span>
      </div>
    );
  }
  if (!status) return <div className="skeleton now-skeleton" />;
  const films = status.libraries.find((l) => l.kind === "movie")?.count;
  const shows = status.libraries.find((l) => l.kind === "show")?.count;
  const newest = arrivals.data?.[0];
  return (
    <div className="now" role="status">
      <span className={`now__tab caps${status.online ? "" : " is-off"}`}>{status.online ? "Now" : "Off air"}</span>
      {status.online ? (
        <>
          <span className="now__item"><b>{status.streams}</b> watching</span>
          {films != null ? <span className="now__item"><b>{films.toLocaleString()}</b> films</span> : null}
          {shows != null ? <span className="now__item"><b>{shows.toLocaleString()}</b> shows</span> : null}
          {newest ? <span className="now__item">Newest: <b>{newest.title.title}</b></span> : null}
        </>
      ) : (
        <span className="now__item">Plex isn’t answering. Requests still go through.</span>
      )}
    </div>
  );
}

/**
 * The search box. With `live` (the Request page) it searches as you type:
 * after a short pause, from two letters, by updating the address so results,
 * the back button and sharing all keep working. Enter still searches at once.
 */
/** The one search box: films, shows and books at once (the Request page groups the results).
 *  `type` keeps the Request page's Type filter when the search changes. */
export function Finder({ initialQuery = "", heading = true, live = false, type = "all" }: {
  initialQuery?: string; heading?: boolean; live?: boolean; type?: string;
}) {
  const [q, setQ] = useState(initialQuery);
  // The narrowest phones get the short hint, so it's never cut.
  const narrow = useMedia("(max-width: 379px)");
  const navigate = useNavigate();
  const go = (query: string, replace: boolean) => {
    const params = new URLSearchParams();
    if (query.trim().length >= 2) params.set("q", query.trim());
    if (type !== "all") params.set("type", type);
    navigate(`/search${params.size ? `?${params}` : ""}`, { replace });
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!q.trim()) return;
    go(q, live);
  };
  useEffect(() => {
    if (!live || q.trim() === initialQuery.trim()) return;
    const t = window.setTimeout(() => go(q, true), 350);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q, live]);
  return (
    <section className="finder">
      {heading ? <h1>What should be on Plexbie?</h1> : null}
      <form className="finder__form" onSubmit={submit} role="search">
        <label className="finder__field">
          <Search size={22} aria-hidden />
          <span className="visually-hidden">Title</span>
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={narrow ? "Search titles" : "Films, shows and books"}
            enterKeyHint="search"
            autoComplete="off"
            type="search"
            aria-label="Search films, shows and books"
          />
          {/* On a phone just the magnifier (the field keeps its words); its hit area is 44px either way. */}
          <button className="btn btn--primary finder__go" type="submit" disabled={!q.trim()} aria-label="Search">
            <Search size={18} aria-hidden className="finder__go-icon" /><span className="finder__go-word">Search</span>
          </button>
        </label>
      </form>
    </section>
  );
}

function YourSchedule() {
  const requests = useLoad(() => api.myRequests(), [], whileActive);
  const live = (requests.data ?? []).filter((r) => r.stage !== "declined").slice(0, 3);
  return (
    <Section id="sched-h" title="Your requests" action={<Link to="/schedule">All requests</Link>}>
      {requests.loading ? (
        <SlotSkeletons count={2} />
      ) : requests.error ? (
        <OffAir compact onRetry={requests.reload}>Couldn’t load your requests. They’re still there.</OffAir>
      ) : live.length ? (
        <Stagger className="schedule">
          {live.map((r) => (
            <RequestSlot key={r.slot} r={r}>
              <span className="slot__meta"><b className="slot__stage">{stageLabel(r.stage, r.title.kind)}.</b> {r.progress?.detail ?? stageHelp(r.stage, r.title.kind, r.format)}, {since(r.updatedAt)}</span>
              {["downloading", "unpacking", "importing"].includes(r.stage) ? <LiveProgress stage={r.stage} progress={{ percent: r.progress?.percent ?? undefined }} /> : null}
            </RequestSlot>
          ))}
        </Stagger>
      ) : (
        <Moment>
          <p>Nothing requested yet. Search above for a film, show or book and it shows up here.</p>
        </Moment>
      )}
    </Section>
  );
}

/** What one of the member's tickets is about, on their list: "General", "Sintel (2010)" or "No. 0214 · Pepper & Carrot". */
function ticketAbout(t: MyTicket) {
  if (t.subject === "general" || !t.title) return "General";
  if (t.subject === "request") return t.slot ? `No. ${formatSlot(t.slot)} · ${t.title}` : t.title;
  return t.year ? `${t.title} (${t.year})` : t.title;
}

/**
 * "Need help?": a ticket about anything that isn't one of their requests (a film or show's
 * own page has one for that), and every ticket of theirs, open or solved in the last 30 days,
 * to read and add to. An alert about one of them opens it here (?ticket=…).
 */
function NeedHelp() {
  const [params, setParams] = useSearchParams();
  const tickets = useLoad(() => api.myTickets(), [], (list) => (list?.some((t) => t.status === "open") ? 60_000 : null));
  const [open, setOpen] = useState<string | null>(params.get("ticket"));
  const [making, setMaking] = useState(false);
  // Sent or answered here, until the list that comes back has it too.
  const [added, setAdded] = useState<MyTicket[]>([]);
  const [answered, setAnswered] = useState<Record<string, MemberTicket>>({});
  const fetched = tickets.data ?? [];
  const rows = [...added.filter((a) => !fetched.some((t) => t.id === a.id)), ...fetched];
  const live = (t: MyTicket): MemberTicket => {
    const mine = answered[t.id];
    return mine && (mine.thread?.length ?? 0) > t.thread.length ? mine : asMemberTicket(t);
  };
  const shown = rows.find((t) => t.id === open);
  const close = () => {
    setOpen(null);
    // Once the sheet's own history entry is off, or replacing would overwrite it and leave an extra Back.
    if (params.get("ticket")) afterOverlay(() => setParams((p) => { p.delete("ticket"); return p; }, { replace: true }));
  };
  return (
    <Section id="need-help-h" title="Need help?" className="need-help">
      <p className="muted">Something wrong with Plex, or with something you didn’t request? Tell the admins.</p>
      <button type="button" className="btn need-help__open" onClick={() => setMaking(true)}>
        <LifeBuoy size={16} aria-hidden /> Open a ticket
      </button>
      {tickets.error && !rows.length ? (
        <OffAir compact onRetry={tickets.reload}>Couldn’t load your tickets. They’re still there.</OffAir>
      ) : rows.length ? (
        <>
          <h3 className="need-help__list">Your tickets</h3>
          <ul className="tickets">
            {rows.map((t) => (
              <li key={t.id}>
                <button type="button" className={`ticket-row${t.status === "resolved" ? " is-solved" : live(t).waiting ? " is-waiting" : ""}`}
                  onClick={() => setOpen(t.id)}>
                  <span className="ticket-row__about">{ticketAbout(t)}</span>
                  <span className="ticket-row__reason">{t.reason}</span>
                  <span className="ticket-row__state">
                    {t.status === "resolved" ? "Solved" : live(t).waiting ? "An admin asked you something" : "Open"} · {since(t.updatedAt)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {createPortal(
        <AnimatePresence>
          {making ? (
            <NewTicketSheet key="new" about={null} onClose={() => setMaking(false)}
              onSent={(t) => { setAdded((a) => [t, ...a]); setMaking(false); tickets.refresh(); }} />
          ) : shown ? (
            <TicketSheet key={shown.id} ticket={live(shown)}
              heading={ticketHeading(shown.subject, shown.title, shown.slot)} about={shown.subject === "request" ? shown.title : null}
              onClose={close} onAnswered={(t) => { setAnswered((a) => ({ ...a, [t.id]: t })); tickets.refresh(); }} />
          ) : null}
        </AnimatePresence>,
        document.body,
      )}
    </Section>
  );
}

function LibraryTeaser() {
  const { status } = useSession();
  if (!status) return null;
  return (
    <Link to="/library" className="teaser">
      <span className="teaser__copy">
        <span className="display teaser__title">Browse the library</span>
        <span className="muted">Everything that is already on Plex, by shelf.</span>
      </span>
      <span className="counts">
        {status.libraries.map((l) => (
          <span className="count" key={l.title}><b>{l.count.toLocaleString()}</b><span>{l.title}</span></span>
        ))}
      </span>
    </Link>
  );
}

/** Who's watching. The page loads `community` once and shares it with its other parts. */
export function OnAirNow({ community, limit }: { community: Loaded<Community>; limit?: number }) {
  const rows = community.data?.onAir.slice(0, limit) ?? [];
  return (
    <Section id="air-h" title="On air now" action={limit ? <Link to="/channel">Channel</Link> : null}>
      {community.loading ? (
        <div className="onair" aria-hidden>{Array.from({ length: Math.min(limit ?? 3, 3) }, (_, i) => <div key={i} className="skeleton onair-skeleton" />)}</div>
      ) : community.error ? (
        <OffAir compact onRetry={community.reload}>Couldn’t see who’s watching right now.</OffAir>
      ) : rows.length ? (
        <div className="onair">
          {rows.map((o) => (
            <div className="onair__row" key={o.member + o.title}>
              <Art title={{ poster: o.poster, title: o.title, kind: "movie" }} size="w185" />
              <div>
                <div className="onair__who">{o.member}, on {o.device}</div>
                <div className="onair__title">{o.title}</div>
                {o.subtitle ? <div className="onair__sub">{o.subtitle}</div> : null}
                <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(o.progress * 100)} aria-label={`${o.member} is ${Math.round(o.progress * 100)}% through ${o.title}`}><i style={{ width: `${o.progress * 100}%` }} /></div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="muted">Nobody’s watching right now.</p>
      )}
    </Section>
  );
}

function YourStanding({ community }: { community: Loaded<Community> }) {
  const you = community.data?.you;
  if (community.error) {
    return (
      <Section id="you-h" title="Your standing">
        <OffAir compact onRetry={community.reload}>Couldn’t load your standing.</OffAir>
      </Section>
    );
  }
  if (!you) return null;
  return (
    <Section id="you-h" title="Your standing">
      <div className="figures">
        <div className="figure"><span className="figure__v">{you.rank ? `#${you.rank}` : "Unranked"}</span><span className="figure__l">most watched, {Math.round(you.hours).toLocaleString()} h</span></div>
        <div className="figure"><span className="figure__v">{you.streak} days</span><span className="figure__l">watch streak</span></div>
      </div>
      {you.topThree ? (
        <p className="muted text-small">You’re in the top three, so inactivity cleanup skips you.</p>
      ) : (
        <p className="muted text-small">
          Last watched {you.daysIdle === 0 ? "today" : `${you.daysIdle} days ago`}. Accounts idle for {you.removalAfterDays} days are removed from Plex.
        </p>
      )}
    </Section>
  );
}

export function Home() {
  useTitle("Home");
  const arrivals = useLoad(() => api.arrivals());
  const community = useLoad(() => api.community());
  const { status, session } = useSession();
  // The rest of the page waits for the channel to know its height (an empty one is shorter than its
  // skeleton), but not for long: 100ms after the page is up it shows anyway, and the channel then
  // keeps its skeleton's height for this visit (filling it, its words in the middle), so nothing
  // under it moves whatever comes back.
  const settled = !!arrivals.data || !!arrivals.error;
  const settledNow = useRef(settled);
  useEffect(() => { settledNow.current = settled; });
  const stageBox = useRef<HTMLDivElement>(null);
  const [waited, setWaited] = useState(false);
  const [holdHeight, setHoldHeight] = useState<number | null>(null);
  useEffect(() => {
    const t = window.setTimeout(() => {
      setWaited(true);
      if (!settledNow.current) setHoldHeight(stageBox.current?.offsetHeight ?? null);
    }, 100);
    return () => window.clearTimeout(t);
  }, []);
  const crawl = [
    ...(status ? [`${status.streams} watching right now`] : []),
    ...(community.data?.onAir ?? []).map((o) => `${o.member.split(" ")[0]} is watching ${o.title}`),
    ...(arrivals.data ?? []).slice(0, 5).map((a) => `New: ${a.title.title}${a.detail ? `, ${a.detail}` : ""}`),
  ];
  return (
    <>
      <div className="shell invite__landed"><InviteOutcome /></div>
      {session?.user.via === "plex" ? <div className="shell home-alerts"><AlertsPanel compact /></div> : null}
      <div ref={stageBox} className={`home__stage${holdHeight ? " is-held" : ""}`} style={holdHeight ? { minHeight: holdHeight } : undefined}>
      {arrivals.data ? <Stage arrivals={arrivals.data} /> : arrivals.error ? (
        <section className="stage stage--compact stage--bare" aria-label="Just arrived on Plex">
          <div className="shell stage__inner">
            <OffAir tab="Channel" onRetry={arrivals.reload}>Couldn’t get what’s new on Plex just now. Nothing is lost.</OffAir>
          </div>
        </section>
      ) : <StageSkeleton ticker={false} />}
      </div>
      {/* It loads meanwhile, and arrives in place, the ticker with it (above a channel that may yet
          shrink, a ticker on show would move). */}
      <div className="home__rest" hidden={!settled && !waited}>
      {/* The ticker's 40px are held while what it says loads, so the page under it doesn't drop. */}
      {crawl.length || !(community.loading || arrivals.loading) ? <Ticker items={crawl} label="Now" /> : <div className="ticker" aria-hidden />}
      <div className="shell page">
        <section className="console" aria-label="Request something">
          <h1>What should be on Plexbie?</h1>
          <Finder heading={false} />
        </section>
        <div className="split">
          <div className="stack stack--12">
            <YourSchedule />
            {session?.member ? <NeedHelp /> : null}
            <LibraryTeaser />
          </div>
          <div className="stack stack--12">
            <OnAirNow community={community} limit={3} />
            <YourStanding community={community} />
          </div>
        </div>
      </div>
      </div>
    </>
  );
}
