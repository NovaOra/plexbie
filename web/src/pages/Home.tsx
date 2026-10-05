import { useState, type FormEvent, useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Search } from "../components/icons";
import { api } from "../api/client";
import { useSession } from "../components/Layout";
import { LiveProgress, RequestSlot, Stage, Stagger, Ticker } from "../components/motion";
import { InviteOutcome } from "./InvitePage";
import { AlertsPanel } from "../components/Alerts";
import { Art, OffAir, SlotSkeletons, since, stageLabel, stageHelp, useLoad, whileActive, useTitle, Section, Moment } from "../components/ui";

export function NowStrap() {
  const { status, statusFailed, session } = useSession();
  const arrivals = useLoad(() => api.arrivals());
  // The server's status is for members; someone not on Plex yet just doesn't see this strip.
  if (!session?.member) return null;
  if (!status && statusFailed) {
    return (
      <div className="now" role="status">
        <span className="now__tab caps is-off">No signal</span>
        <span className="now__item">Couldn’t reach Plexbie just now.</span>
      </div>
    );
  }
  if (!status) return <div className="skeleton" style={{ height: 38, width: 280 }} />;
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
            placeholder="Films, shows and books"
            enterKeyHint="search"
            autoComplete="off"
            type="search"
            aria-label="Search films, shows and books"
          />
          <button className="btn btn--primary" type="submit" disabled={!q.trim()}>Search</button>
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

export function OnAirNow({ limit }: { limit?: number }) {
  const community = useLoad(() => api.community());
  const rows = community.data?.onAir.slice(0, limit) ?? [];
  return (
    <Section id="air-h" title="On air now" action={limit ? <Link to="/channel">Channel</Link> : null}>
      {community.loading ? (
        <div className="skeleton" style={{ height: 200 }} />
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

function YourStanding() {
  const community = useLoad(() => api.community());
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
        <p className="muted" style={{ fontSize: "0.9rem" }}>You’re in the top three, so inactivity cleanup skips you.</p>
      ) : (
        <p className="muted" style={{ fontSize: "0.9rem" }}>
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
  const crawl = [
    ...(status ? [`${status.streams} watching right now`] : []),
    ...(community.data?.onAir ?? []).map((o) => `${o.member.split(" ")[0]} is watching ${o.title}`),
    ...(arrivals.data ?? []).slice(0, 5).map((a) => `New: ${a.title.title}${a.detail ? `, ${a.detail}` : ""}`),
  ];
  return (
    <>
      <div className="shell invite__landed"><InviteOutcome /></div>
      {session?.user.via === "plex" ? <div className="shell home-alerts"><AlertsPanel compact /></div> : null}
      {arrivals.data ? <Stage arrivals={arrivals.data} /> : arrivals.error ? (
        <section className="stage stage--compact stage--bare" aria-label="Just arrived on Plex">
          <div className="shell stage__inner">
            <OffAir onRetry={arrivals.reload}>Couldn’t get what’s new on Plex. Nothing is lost.</OffAir>
          </div>
        </section>
      ) : (
        <section className="stage stage--compact stage--bare" aria-busy="true" aria-label="Just arrived on Plex">
          <div className="shell stage__inner">
            <div className="stage__feature">
              <div className="skeleton" style={{ height: 34, width: 220 }} />
              <div className="skeleton" style={{ height: "3.2rem", width: "60%" }} />
            </div>
          </div>
        </section>
      )}
      <Ticker items={crawl} label="Now" />
      <div className="shell page">
        <section className="console" aria-label="Request something">
          <h1>What should be on Plexbie?</h1>
          <Finder heading={false} />
        </section>
        <div className="split">
          <div style={{ display: "grid", gap: 40 }}>
            <YourSchedule />
            <LibraryTeaser />
          </div>
          <div style={{ display: "grid", gap: 40 }}>
            <OnAirNow limit={3} />
            <YourStanding />
          </div>
        </div>
      </div>
    </>
  );
}
