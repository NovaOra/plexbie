// The Request page under the search box: three small dropdowns (Type, Language, Genre),
// then either the search's results, grouped (films, shows, books), or things to browse
// from Seerr (TMDB): Trending, Popular, Coming soon and Top rated, films and shows
// together. Each title says whether it's already on Plex or requested; nothing is asked
// for until someone does. Also "More like this" for a title page.
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { Discover, DiscoverShelf, Title } from "../api/types";
import { Row } from "./Popular";
import { Moment, OffAir, Section, useLoad } from "./ui";

export type RequestType = "all" | "movie" | "tv" | "book";
export const TYPES: [RequestType, string][] = [["all", "Everything"], ["movie", "Films"], ["tv", "Shows"], ["book", "Books"]];
const NOUN = { movie: "films", tv: "shows" } as const;

/** "Trending films", "Shows coming soon"…: the shelf's name, saying which kind it is. */
function shelfHeading(key: string, kind: "movie" | "tv") {
  const n = NOUN[kind];
  const by: Record<string, string> = {
    trending: `Trending ${n}`, popular: `Popular ${n}`, top: `Top rated ${n}`,
    upcoming: `${n[0].toUpperCase()}${n.slice(1)} coming soon`,
  };
  return by[key] ?? key;
}

/** One shelf, which can load more of itself. */
function Shelf({ kind, shelf }: { kind: "movie" | "tv"; shelf: DiscoverShelf }) {
  const [titles, setTitles] = useState<Title[]>(shelf.titles);
  const [page, setPage] = useState(1);
  const [more, setMore] = useState(shelf.more);
  const [busy, setBusy] = useState(false);
  // Starts over only when the shelf itself changes, not each time the page around it
  // re-renders (which hands it a new object), so the pages loaded with "More" stay.
  useEffect(() => { setTitles(shelf.titles); setPage(1); setMore(shelf.more); }, [shelf.key, shelf.titles, shelf.more]);
  const next = async () => {
    setBusy(true);
    try {
      const got = await api.shelf(kind, shelf.key, page + 1);
      setTitles((had) => [...had, ...got.titles.filter((t) => !had.some((h) => h.id === t.id))]);
      setPage(got.page);
      setMore(got.more);
    } catch {
      setMore(false);
    } finally {
      setBusy(false);
    }
  };
  return <Row heading={shelf.title} titles={titles} ranked={shelf.key === "trending"} onMore={more ? () => void next() : undefined} busy={busy} />;
}

/** A genre's shelf, loaded when it's picked. */
function GenreShelf({ kind, id, heading }: { kind: "movie" | "tv"; id: number; heading: string }) {
  const first = useLoad(() => api.shelf(kind, `genre-${id}`, 1), [kind, id]);
  if (first.loading) return <Skeletons />;
  if (first.error || !first.data) return <p className="muted">Couldn’t load {heading.toLowerCase()} just now.</p>;
  if (!first.data.titles.length) return <p className="muted">No {heading.toLowerCase()} to show right now.</p>;
  return <Shelf kind={kind} shelf={{ key: `genre-${id}`, title: heading, titles: first.data.titles, more: first.data.more }} />;
}

function Skeletons() {
  return <div className="popular__list">{Array.from({ length: 6 }, (_, i) => <div key={i} className="skeleton popular__skeleton" />)}</div>;
}

/** Language: a small dropdown of checkboxes, saved to their account (the app shows the same). */
function LanguagePick({ chosen, options, onChange }: {
  chosen: string[]; options: { code: string; name: string }[]; onChange: (next: string[]) => void;
}) {
  const menu = useRef<HTMLDetailsElement>(null);
  const label = !chosen.length ? "Every language"
    : chosen.length <= 2 ? chosen.map((c) => options.find((o) => o.code === c)?.name ?? c).join(", ") : `${chosen.length} languages`;
  // Closes on a click anywhere else, as a dropdown does.
  useEffect(() => {
    const away = (e: MouseEvent) => { if (menu.current?.open && !menu.current.contains(e.target as Node)) menu.current.open = false; };
    document.addEventListener("click", away);
    return () => document.removeEventListener("click", away);
  }, []);
  return (
    <details className="pick-menu" ref={menu}>
      <summary className="select" aria-label={`Language: ${label}`}>{label}</summary>
      <div className="pick-menu__panel" role="group" aria-label="Languages to show">
        <label className="pick-menu__opt">
          <input type="checkbox" checked={!chosen.length} onChange={() => onChange([])} /> Every language
        </label>
        {options.map((o) => (
          <label key={o.code} className="pick-menu__opt">
            <input type="checkbox" checked={chosen.includes(o.code)}
              onChange={() => onChange(chosen.includes(o.code) ? chosen.filter((c) => c !== o.code) : [...chosen, o.code])} />
            {o.name}
          </label>
        ))}
      </div>
    </details>
  );
}

export function RequestView({ q, type, setType }: { q: string; type: RequestType; setType: (t: RequestType) => void }) {
  const kinds: ("movie" | "tv")[] = type === "movie" ? ["movie"] : type === "tv" ? ["tv"] : type === "all" ? ["movie", "tv"] : [];
  const browsing = !q && kinds.length > 0;
  const prefs = useLoad(() => api.prefs(), []);
  const [langs, setLangs] = useState<string[] | null>(null);
  const [version, setVersion] = useState(0);
  const [genre, setGenre] = useState("");
  const chosen = langs ?? prefs.data?.languages ?? [];
  const films = useLoad<Discover | null>(() => (browsing && kinds.includes("movie") ? api.discover("movie") : Promise.resolve(null)), [browsing, type, version]);
  const shows = useLoad<Discover | null>(() => (browsing && kinds.includes("tv") ? api.discover("tv") : Promise.resolve(null)), [browsing, type, version]);
  const results = useLoad(() => (q ? api.searchAll(q) : Promise.resolve(null)), [q]);

  const pickLanguages = async (next: string[]) => {
    setLangs(next);
    try {
      await api.saveLanguages(next);
      setVersion((v) => v + 1);
    } catch {
      setLangs(null);
    }
  };
  const byKind = { movie: films.data, tv: shows.data };
  const genres = [...new Set(kinds.flatMap((k) => byKind[k]?.genres.map((g) => g.name) ?? []))].sort();
  useEffect(() => { if (genre && genres.length && !genres.includes(genre)) setGenre(""); }, [genre, genres]);

  const bar = (
    <div className="request-bar" role="group" aria-label="Filters">
      <select className="select" value={type} aria-label="Type" onChange={(e) => setType(e.target.value as RequestType)}>
        {TYPES.map(([t, label]) => <option key={t} value={t}>{label}</option>)}
      </select>
      {browsing && prefs.data?.languageOptions?.length ? (
        <LanguagePick chosen={chosen} options={prefs.data.languageOptions} onChange={(next) => void pickLanguages(next)} />
      ) : null}
      {browsing && genres.length ? (
        <select className="select" value={genre} aria-label="Genre" onChange={(e) => setGenre(e.target.value)}>
          <option value="">Any genre</option>
          {genres.map((g) => <option key={g} value={g}>{g}</option>)}
        </select>
      ) : null}
    </div>
  );

  if (q) {
    const r = results.data;
    const groups: [RequestType, string, Title[]][] = [["movie", "Films", r?.movie ?? []], ["tv", "Shows", r?.tv ?? []], ["book", "Books", r?.book ?? []]];
    const shown = groups.filter(([k]) => type === "all" || type === k);
    const total = shown.reduce((n, [, , t]) => n + t.length, 0);
    return (
      <>
        {bar}
        <section className="section popular" aria-busy={results.loading}>
          <h2 className="visually-hidden" aria-live="polite">
            {results.loading ? "Searching…" : results.error ? "Search is off air" : `${total} matching “${q}”`}
          </h2>
          {results.error ? (
            <OffAir onRetry={results.reload}>Search didn’t answer just now. Try again in a moment.</OffAir>
          ) : results.loading && !r ? <Skeletons />
          : total ? shown.map(([k, label, titles]) => <Row key={k} heading={`${label} (${titles.length})`} titles={titles} ranked={false} />)
          : (
            <Moment>
              <p>Nothing matched “{q}”{type === "all" ? "" : ` in ${TYPES.find(([t]) => t === type)?.[1].toLowerCase()}`}. Check the spelling{type === "all" ? "" : ", or set Type to Everything"}.</p>
            </Moment>
          )}
        </section>
      </>
    );
  }

  if (!kinds.length) {
    return (
      <>
        {bar}
        <Moment><p>Type a title or an author to find a book. You pick audiobook or ebook on its page.</p></Moment>
      </>
    );
  }

  const loading = kinds.some((k) => (k === "movie" ? films : shows).loading);
  const failed = kinds.every((k) => (k === "movie" ? films : shows).error);
  const keys = ["trending", "popular", "upcoming", "top"];
  return (
    <>
      {bar}
      <Section id="discover-h" title={genre || "Find something to ask for"} className="popular" busy={loading}>
        <p className="muted popular__note">
          From TMDB through Seerr{chosen.length ? ", in the languages you picked" : ", worldwide"}. Tap one to see it or ask for it.
        </p>
        {failed ? (
          <OffAir compact onRetry={() => setVersion((v) => v + 1)}>Couldn’t load what’s trending just now.</OffAir>
        ) : loading ? <Skeletons />
        : genre ? kinds.map((k) => {
          const g = byKind[k]?.genres.find((x) => x.name === genre);
          return g ? <GenreShelf key={k} kind={k} id={g.id} heading={`${genre} ${NOUN[k]}`} /> : null;
        })
        : keys.flatMap((key) => kinds.map((k) => {
          const s = byKind[k]?.shelves.find((x) => x.key === key);
          return s ? <Shelf key={`${k}:${key}`} kind={k} shelf={{ ...s, title: shelfHeading(key, k) }} /> : null;
        }))}
      </Section>
    </>
  );
}

/** On a title page: Seerr's recommendations for it, if it has any. */
export function MoreLikeThis({ kind, id }: { kind: "movie" | "tv"; id: string }) {
  const s = useLoad(() => api.similar(kind, id), [kind, id]);
  if (s.loading || s.error || !s.data?.length) return null;
  return (
    <Section id="similar-h" title="More like this" className="popular">
      <Row heading={kind === "movie" ? "Films" : "Shows"} titles={s.data} ranked={false} />
    </Section>
  );
}
