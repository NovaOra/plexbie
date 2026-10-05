import { useRef } from "react";
import { ChevronLeft, ChevronRight, Flame } from "./icons";
import { api } from "../api/client";
import type { Title } from "../api/types";
import { PosterCard } from "./motion";
import { scrollBehavior, useLoad, Section } from "./ui";

/** A row of posters to scroll sideways. ranked: the big 1, 2, 3 beside each (trending).
 *  onMore: a last tile that loads the next page. */
export function Row({ heading, titles, ranked = true, onMore, busy }: {
  heading: string; titles: Title[]; ranked?: boolean; onMore?: () => void; busy?: boolean;
}) {
  const list = useRef<HTMLOListElement>(null);
  if (!titles.length) return null;
  const nudge = (dir: 1 | -1) => list.current?.scrollBy({ left: dir * list.current.clientWidth * 0.8, behavior: scrollBehavior() });
  return (
    <div className="popular__row">
      <div className="popular__rowhead">
        <h3 className="popular__kind">{heading}</h3>
        <span className="popular__arrows">
          <button type="button" className="popular__arrow" aria-label={`Scroll ${heading.toLowerCase()} back`} onClick={() => nudge(-1)}><ChevronLeft size={20} aria-hidden /></button>
          <button type="button" className="popular__arrow" aria-label={`Scroll ${heading.toLowerCase()} on`} onClick={() => nudge(1)}><ChevronRight size={20} aria-hidden /></button>
        </span>
      </div>
      <ol className="popular__list" ref={list}>
        {titles.map((t, i) => (
          <li key={t.kind + t.id} className={`popular__item${ranked ? "" : " popular__item--plain"}`}>
            {ranked ? <span className="popular__rank" aria-hidden>{i + 1}</span> : null}
            <PosterCard
              title={t}
              meta={t.availability === "blocked" ? "Not available" : t.year}
            />
          </li>
        ))}
        {onMore ? (
          <li className="popular__item popular__item--plain">
            <button type="button" className="popular__more" onClick={onMore} disabled={busy} aria-label={`More ${heading.toLowerCase()}`}>
              {busy ? "Loading…" : "More"}
            </button>
          </li>
        ) : null}
      </ol>
    </div>
  );
}

/** Top 5 trending films and shows right now, badged when they're already on Plex or requested. */
export function PopularNow({ heading = "Trending right now" }: { heading?: string }) {
  const popular = useLoad(() => api.popular());
  if (popular.error) return null;
  return (
    <Section id="popular-h" title={<><Flame size={20} aria-hidden className="popular__flame" /> {heading}</>} className="popular" busy={popular.loading}>
      <p className="muted popular__note">What people everywhere are watching this week. Tap one to see it or ask for it.</p>
      {popular.loading ? (
        <div className="popular__list">{Array.from({ length: 5 }, (_, i) => <div key={i} className="skeleton popular__skeleton" />)}</div>
      ) : (
        <>
          <Row heading="Films" titles={popular.data?.movies ?? []} />
          <Row heading="Shows" titles={popular.data?.tv ?? []} />
        </>
      )}
    </Section>
  );
}
