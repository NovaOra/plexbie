import { Flame, ShieldCheck } from "../components/icons";
import { api } from "../api/client";
import { useSession } from "../components/Layout";
import { OffAir, since, useLoad, useTitle, Section, Notice } from "../components/ui";
import { NowStrap, OnAirNow } from "./Home";

export function Channel() {
  useTitle("Channel");
  const { session, status } = useSession();
  const community = useLoad(() => api.community());
  const party = useLoad(() => api.watchparty());
  const you = community.data?.you;
  const board = community.data?.leaderboard ?? [];

  return (
    <div className="shell page">
      <div style={{ display: "grid", gap: 16 }}>
        <h1 className="display page-title">Channel</h1>
        <NowStrap />
      </div>

      <div className="split">
        <div style={{ display: "grid", gap: 40 }}>
          <OnAirNow community={community} />

          <Section id="lb-h" title="Most watched">
            <p className="muted" style={{ fontSize: "0.92rem", marginTop: -6 }}>All-time watch time, watch parties included. The top three are never removed for inactivity.</p>
            {community.loading ? (
              <div className="ranks" aria-busy="true" aria-label="Loading the board">
                {Array.from({ length: 5 }, (_, i) => <div key={i} className="skeleton" style={{ height: 48 }} />)}
              </div>
            ) : community.error ? (
              <OffAir compact onRetry={community.reload}>Couldn’t load the board just now.</OffAir>
            ) : !board.length ? (
              <p className="muted">No watch time recorded yet. It starts counting as soon as someone presses play.</p>
            ) : (
              <ol className="ranks" style={{ listStyle: "none", margin: 0, padding: 0 }}>
                {board.map((p, i) => (
                  <li key={p.name} className={`rank${i < 3 ? " rank--top" : ""}${p.name === session?.user.name ? " rank--you" : ""}`}>
                    <span className="rank__n">{i + 1}</span>
                    <span className="rank__name">
                      <span>{p.name}{p.name === session?.user.name ? " (you)" : ""}</span>
                      {i < 3 ? <ShieldCheck size={16} aria-label="Safe from cleanup" color="var(--screen)" /> : null}
                    </span>
                    <span className="rank__hours">{Math.round(p.hours).toLocaleString()} <small>h</small></span>
                  </li>
                ))}
              </ol>
            )}
          </Section>
        </div>

        <div style={{ display: "grid", gap: 40 }}>
          {you ? (
            <Section id="you-h" title="You">
              <div className="figures">
                <div className="figure"><span className="figure__v">{Math.round(you.hours).toLocaleString()} h</span><span className="figure__l">watched in total</span></div>
                <div className="figure"><span className="figure__v">{you.rank ? `#${you.rank}` : "Unranked"}</span><span className="figure__l">on the board</span></div>
                <div className="figure">
                  <span className="figure__v" style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    {you.streak > 0 ? <Flame size={20} color="var(--tally)" aria-hidden /> : null}{you.streak}
                  </span>
                  <span className="figure__l">day streak, best {you.longestStreak}</span>
                </div>
                <div className="figure">
                  <span className="figure__v">{Math.round(you.watchPartyMinutes / 60 * 10) / 10} h</span>
                  <span className="figure__l">
                    watch-party credit{party.data?.sessions ? `, ${party.data.sessions} session${party.data.sessions === 1 ? "" : "s"}` : ""}
                    {party.data?.last ? `, last ${since(party.data.last)}` : ""}
                  </span>
                </div>
              </div>
              <Notice icon={ShieldCheck} level={3} title={you.topThree ? "Safe from cleanup" : you.daysIdle === 0 ? "Active today" : `Idle for ${you.daysIdle} days`}>
                {you.topThree
                  ? "Top-three watchers are skipped by the inactivity check."
                  : `Accounts with nothing watched for ${you.removalAfterDays} days are removed from Plex. Watching anything resets it.`}
              </Notice>
            </Section>
          ) : null}

          {status ? (
            <Section id="lib-h" title="On the server">
              <div className="figures">
                {status.libraries.map((l) => (
                  <div className="figure" key={l.title}>
                    <span className="figure__v">{l.count.toLocaleString()}</span>
                    <span className="figure__l">{l.title}</span>
                  </div>
                ))}
              </div>
            </Section>
          ) : null}
        </div>
      </div>
    </div>
  );
}
