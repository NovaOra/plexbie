import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { MediaRequest } from "../api/types";
import { LiveProgress, RequestSlot, Stagger } from "../components/motion";
import { PopularNow } from "../components/Popular";
import { HelpButton } from "../components/HelpSheet";
import { KIND_LABEL, OffAir, seasonsLabel, SlotSkeletons, since, stageLabel, stageHelp, useLoad, whileActive, useTitle, PageHead, Moment } from "../components/ui";

function detail(r: MediaRequest) {
  const bits: string[] = [KIND_LABEL[r.title.kind]];
  const seasons = seasonsLabel(r.seasons);
  if (seasons) bits.push(seasons);
  // The format only when it adds something: not "Audiobook, Audiobook".
  if (r.format && r.format !== r.title.kind) bits.push(r.format === "both" ? "Audiobook + ebook" : KIND_LABEL[r.format]);
  return `${bits.join(", ")}. Asked ${since(r.requestedAt)}.`;
}

/** One group of requests. Finished ones (`fold`) show the newest few; the rest wait behind "Show all". */
function Group({ heading, items, fold }: { heading: string; items: MediaRequest[]; fold?: number }) {
  const [all, setAll] = useState(false);
  if (!items.length) return null;
  const shown = fold && !all ? items.slice(0, fold) : items;
  return (
    <section className="section">
      <h2>{heading}</h2>
      <Stagger className="schedule">
        {shown.map((r) => (
          <RequestSlot key={r.slot} r={r} before={<span className="slot__meta">{detail(r)}</span>}
            after={r.stage !== "declined" && r.stage !== "closed" ? <div className="slot__help"><HelpButton request={r} /></div> : null}>
            <span className="slot__meta"><b className="slot__stage">{stageLabel(r.stage, r.title.kind)}.</b> {stageHelp(r.stage, r.title.kind, r.format)}</span>
            <LiveProgress stage={r.stage} progress={r.progress} />
            {r.note ? <span className="slot__note">“{r.note}”</span> : null}
          </RequestSlot>
        ))}
      </Stagger>
      {shown.length < items.length ? (
        <button type="button" className="btn btn--quiet" style={{ justifySelf: "start" }} onClick={() => setAll(true)}>
          Show all {items.length}
        </button>
      ) : null}
    </section>
  );
}

export function Schedule() {
  useTitle("My requests");
  const requests = useLoad(() => api.myRequests(), [], whileActive);
  const all = requests.data ?? [];
  const active = all.filter((r) => ["requested", "approved", "searching", "downloading", "unpacking", "importing"].includes(r.stage));
  // Not out yet: soonest first, those with no date at the end.
  const upcoming = all.filter((r) => r.stage === "upcoming")
    .sort((a, b) => (a.progress?.releaseDate ?? "9999").localeCompare(b.progress?.releaseDate ?? "9999"));
  const arrived = all.filter((r) => r.stage === "available");
  const declined = all.filter((r) => r.stage === "declined");

  return (
    <div className="shell page">
      <PageHead title="My requests">
        <p className="muted" style={{ maxWidth: "52ch" }}>
          Everything you’ve asked for, from here or with /request in Discord. Each one keeps its number from start to finish.
        </p>
      </PageHead>
      {requests.loading ? (
        <SlotSkeletons count={3} />
      ) : requests.error ? (
        <OffAir onRetry={requests.reload}>Couldn’t load your requests. They’re still there, and still moving.</OffAir>
      ) : all.length ? (
        <>
          <Group heading="In progress" items={active} />
          <Group heading="Coming soon" items={upcoming} />
          <Group heading="Arrived" items={arrived} fold={5} />
          <Group heading="Not added" items={declined} fold={5} />
          <PopularNow heading="Trending right now: want any of these?" />
        </>
      ) : (
        <Moment>
          <p>You haven’t asked for anything yet.</p>
          <Link className="btn btn--primary" to="/search">Find something</Link>
        </Moment>
      )}
      {all.length || requests.loading || requests.error ? null : (
        <PopularNow heading="Not sure what to ask for? These are trending" />
      )}
    </div>
  );
}
