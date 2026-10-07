import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { api } from "../api/client";
import type { LibraryKind } from "../api/types";
import { useSession } from "../components/Layout";
import { EASE_OUT, PosterCard } from "../components/motion";
import { OffAir, since, useLoad, useTitle, PageHead, Moment } from "../components/ui";

const TABS: { id: LibraryKind; label: string; library: string }[] = [
  { id: "movie", label: "Films", library: "movie" },
  { id: "tv", label: "TV", library: "show" },
  { id: "book", label: "Books", library: "book" },
];

type Sort = "added" | "az" | "year";

/** Cards move into their new places on a sort or filter while the wall is short;
 *  past this many, they just appear there, which a phone can keep up with. */
const ANIMATED_UP_TO = 96;

export function Library() {
  useTitle("Library");
  const reduced = useReducedMotion();
  const { status } = useSession();
  const [kind, setKind] = useState<LibraryKind>("movie");
  const [genre, setGenre] = useState<string | null>(null);
  const [sort, setSort] = useState<Sort>("added");
  const items = useLoad(() => api.library(kind), [kind]);
  const [shown_n, setShownN] = useState(48);
  const more = useRef<HTMLDivElement>(null);
  // A new shelf, genre or sort starts again from the first 48, in the same render
  // (an effect would first lay out, and animate, every card shown so far).
  const view = `${kind}|${genre}|${sort}`;
  const [shownFor, setShownFor] = useState(view);
  if (shownFor !== view) {
    setShownFor(view);
    setShownN(48);
  }

  const genres = useMemo(() => {
    const all = new Set<string>();
    for (const t of items.data ?? []) for (const g of t.genres ?? []) all.add(g);
    return [...all].sort();
  }, [items.data]);

  const shown = useMemo(() => {
    const list = (items.data ?? []).filter((t) => !genre || t.genres?.includes(genre));
    if (sort === "az") list.sort((a, b) => a.title.localeCompare(b.title));
    else if (sort === "year") list.sort((a, b) => Number(b.year) - Number(a.year));
    else list.sort((a, b) => b.addedAt.localeCompare(a.addedAt));
    return list;
  }, [items.data, genre, sort]);
  // Cards on their way out keep what they last rendered with, so a long wall
  // also drops them at once instead of animating each one away.
  const animated = Math.min(shown_n, shown.length) <= ANIMATED_UP_TO;

  const counts = TABS.map((t) => ({
    ...t,
    count: status?.libraries.filter((l) => l.kind === t.library).reduce((n, l) => n + l.count, 0),
  }));

  return (
    <div className="shell page">
      <header className="library-head">
        <PageHead title="Library" lede="Everything already on Plex. Open anything to see its details." />
        <div className="counts">
          {counts.map((c) => c.count != null ? (
            <span className="count" key={c.id}><b>{c.count.toLocaleString()}</b><span>{c.label}</span></span>
          ) : null)}
        </div>
      </header>

      <div style={{ display: "grid", gap: 16 }}>
        {/* The same three small dropdowns as the Request page (and the app's Library). */}
        <div className="request-bar" role="group" aria-label="Filters">
          <select className="select" value={kind} aria-label="Shelf"
            onChange={(e) => { setKind(e.target.value as LibraryKind); setGenre(null); }}>
            {counts.map((c) => <option key={c.id} value={c.id}>{c.count ? `${c.label} · ${c.count.toLocaleString()}` : c.label}</option>)}
          </select>
          <select className="select" value={sort} aria-label="Sort" onChange={(e) => setSort(e.target.value as Sort)}>
            <option value="added">Recently added</option>
            <option value="az">A to Z</option>
            <option value="year">Newest release</option>
          </select>
          {genres.length > 1 ? (
            <select className="select" value={genre ?? ""} aria-label="Genre" onChange={(e) => setGenre(e.target.value || null)}>
              <option value="">Any genre</option>
              {genres.map((g) => <option key={g} value={g}>{g}</option>)}
            </select>
          ) : null}
        </div>
      </div>

      <LoadMore target={more} total={shown.length} count={shown_n} onMore={() => setShownN((n) => n + 48)} />
      {items.loading ? (
        <div className="wall">
          {Array.from({ length: 12 }, (_, i) => <div key={i} className="skeleton" style={{ aspectRatio: "2 / 3" }} />)}
        </div>
      ) : items.error ? (
        <OffAir onRetry={items.reload}>Couldn’t load this shelf from Plex. Nothing is lost.</OffAir>
      ) : shown.length ? (
        <motion.div
          key={kind}
          className="wall"
          initial={reduced ? false : "hidden"}
          animate="shown"
          variants={{ shown: { transition: { staggerChildren: 0.04 } } }}
        >
          <AnimatePresence mode="popLayout">
            {shown.slice(0, shown_n).map((t) => (
              <motion.div
                key={t.id}
                layout={animated}
                exit={animated ? { opacity: 0, scale: 0.96, transition: { duration: 0.12 } } : undefined}
                transition={{ layout: { duration: 0.25, ease: EASE_OUT } }}
              >
                <PosterCard title={t} link={!t.id.startsWith("plex:")} meta={sort === "added" ? `Added ${since(t.addedAt)}` : t.year} />
              </motion.div>
            ))}
          </AnimatePresence>
        </motion.div>
      ) : (
        <Moment>
          <p>{genre ? "Nothing on this shelf matches. Try another genre, or request it." : "Nothing on this shelf yet. Ask for something and it lands here."}</p>
        </Moment>
      )}
      <div ref={more} aria-hidden />
      {!items.loading && shown_n < shown.length ? (
        <button type="button" className="btn btn--quiet" style={{ justifySelf: "center" }} onClick={() => setShownN((n) => n + 48)}>
          Show more ({shown.length - shown_n} left)
        </button>
      ) : null}
    </div>
  );
}

/** Shows the next page when the reader nears the end of the wall. */
function LoadMore({ target, total, count, onMore }: { target: React.RefObject<HTMLDivElement | null>; total: number; count: number; onMore: () => void }) {
  useEffect(() => {
    const el = target.current;
    if (!el || count >= total) return;
    const io = new IntersectionObserver((e) => e[0].isIntersecting && onMore(), { rootMargin: "800px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, [target, total, count, onMore]);
  return null;
}
