# path: portal/data.py
"""What the portal's pages show, assembled from the bot's services.

Plex, Seerr, Tautulli and the bot's own database are read here and turned
into the plain shapes in web/src/api/types.ts. Nothing in this module writes.
Blocking work (Plex, files) goes through run_blocking; network reads share the
portal cache so many viewers cost no more than one.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlencode

from sqlalchemy import select

from core.blocking import run_blocking
from core.clients import ServiceError
from core.logging import get_logger
from database.kv_store import kv_get
from database.request_store import all_requests
from database.session import get_session
from portal import books as shelf
from portal import cleanup as cleanupdata
from portal import plex as plexdata
from portal.cache import TTLCache
from portal.progress import Progress
from utils.formatting import parse_utc
from utils.standings import load_aliases, load_streaks, resolve_alias, standings

logger = get_logger(__name__)


#: Outcome recording started on 2026-09-30.
#: A record older than this still says "pending" whatever really happened to it,
#: so it is never shown as waiting; its real state comes from Radarr/Sonarr/SABnzbd.
OUTCOMES_SINCE = datetime(2026, 9, 30, tzinfo=timezone.utc)


def predates_outcomes(record: dict) -> bool:
    dt = parse_utc(record.get("timestamp"))
    return dt is None or dt < OUTCOMES_SINCE
TMDB_SIZES = {"w185", "w342", "w780", "w1280"}


def tmdb_art(path: Optional[str], size: str = "w342") -> Optional[str]:
    if not path or not path.startswith("/") or size not in TMDB_SIZES:
        return None
    return f"/img/tmdb/{size}{path}?v=2"


def ol_art(cover_id: Any) -> Optional[str]:
    return f"/img/ol/{int(cover_id)}-L.jpg?v=2" if str(cover_id or "").isdigit() else None


def _iso(epoch_or_iso: Any) -> str:
    return (parse_utc(epoch_or_iso) or datetime.now(timezone.utc)).isoformat()


class Data:
    def __init__(self, services, cache: Optional[TTLCache] = None):
        self.services = services
        self.config = services.config
        self.cache = cache or TTLCache()
        self.progress = Progress(services, self.cache)

    # ----------------------------------------------------------- helpers
    async def _seerr(self, path: str, ttl: float) -> Any:
        """GET an Seerr path (query string included), cached for `ttl` seconds."""
        return await self.cache.get(f"seerr:{path}", ttl, lambda: self.services.seerr.get(path))

    async def _plex(self, fn, *args, key: str, ttl: float):
        server = self.services.plex_server
        if server is None:
            raise RuntimeError("Plex is not connected")
        return await self.cache.get(key, ttl, lambda: run_blocking(fn, server, *args))

    # ---------------------------------------------------------- cleanup
    async def cleanup_config(self) -> dict:
        from plugins.media_cleanup.cog import CLEANUP_NAMESPACE, DEFAULT_CONFIG
        saved = await kv_get(CLEANUP_NAMESPACE, "config", {}) or {}
        return {**DEFAULT_CONFIG, **saved}

    async def countdown(self) -> Dict[str, dict]:
        """{ratingKey: countdown} matching media_cleanup's own schedule; {} when cleanup is off."""
        config = await self.cleanup_config()
        if not config.get("enabled", True):
            return {}

        async def load():
            from plugins.media_cleanup.cog import CLEANUP_NAMESPACE
            requests = await run_blocking(cleanupdata.request_times)
            server = self.services.plex_server
            if server is None:
                raise RuntimeError("Plex is not connected")
            # When the bot first warned about each title: none goes before its whole warning.
            warned = cleanupdata.current_warnings(await kv_get(CLEANUP_NAMESPACE, "warned", {}),
                                                  await kv_get(CLEANUP_NAMESPACE, "warned_checked"),
                                                  datetime.now(timezone.utc))
            return await run_blocking(cleanupdata.compute, server, config, requests, warned)
        result = await self.cache.get("cleanup:countdown", 1800, load)
        practice = bool(config.get("dry_run"))
        return {rk: {**c, "practice": practice} for rk, c in result.items()}

    @staticmethod
    def _leaving(c: Optional[dict]) -> Optional[dict]:
        if not c:
            return None
        if c.get("exempt"):
            return {"exempt": True}
        return {"daysLeft": c["daysLeft"], "warning": c["warning"], "reason": c["reason"], "practice": c.get("practice", False)}

    async def _now_playing(self) -> List[dict]:
        """From the bot's shared sessions snapshot, so the site adds no Plex polling of its own."""
        items = await self.services.plex_sessions()
        return await run_blocking(plexdata.now_playing, items)

    async def _shelf(self) -> Dict[str, dict]:
        return await self.cache.get(
            "shelf", 900,
            lambda: run_blocking(shelf.scan, self.config.bookshelf_audiobook_library, self.config.bookshelf_ebook_library),
        )

    # ------------------------------------------------------------ status
    async def status(self) -> dict:
        books = await self._shelf()
        book_libs = [
            {"title": "Audiobooks", "kind": "book", "count": sum(1 for b in books.values() if b["kind"] == "audiobook")},
            {"title": "Ebooks", "kind": "book", "count": sum(1 for b in books.values() if b["kind"] == "ebook")},
        ]
        try:
            sections = await self._plex(plexdata.sections, key="plex:sections", ttl=600)
            playing = await self._now_playing()
        except Exception as e:
            logger.info(f"portal status: Plex unavailable ({e})")
            return {"online": False, "streams": 0, "libraries": book_libs, "checkedAt": _iso(None)}
        names = {"movie": "Films", "show": "TV", "artist": "Music"}
        libs = []
        for kind in ("movie", "show", "artist"):
            count = sum(s["count"] for s in sections if s["kind"] == kind)
            if count:
                libs.append({"title": names[kind], "kind": kind, "count": count})
        return {"online": True, "streams": len(playing), "libraries": libs + [b for b in book_libs if b["count"]], "checkedAt": _iso(None)}

    # ----------------------------------------------------------- library
    async def arrivals(self) -> List[dict]:
        recent = await self._plex(plexdata.recent, key="plex:recent", ttl=120)
        out = [{"title": a["title"], "addedAt": _iso(a["addedAt"]), "detail": a["detail"]} for a in recent[:16]]
        books = sorted((await self._shelf()).values(), key=lambda b: b["addedAt"], reverse=True)[:4]
        out += [{"title": shelf.public(b), "addedAt": _iso(b["addedAt"]), "detail": "Audiobook" if b["kind"] == "audiobook" else "Ebook"} for b in books]
        out.sort(key=lambda a: a["addedAt"], reverse=True)
        return out[:16]

    async def library(self, kind: str) -> List[dict]:
        if kind == "book":
            items = sorted((await self._shelf()).values(), key=lambda b: b["addedAt"], reverse=True)
            return [{**shelf.public(b), "addedAt": _iso(b["addedAt"])} for b in items]
        items = await self._plex(plexdata.library, kind, key=f"plex:library:{kind}", ttl=600)
        try:
            clock = await self.countdown()
        except Exception as e:
            logger.info(f"portal: cleanup countdown unavailable ({e})")
            clock = {}
        server = self.services.plex_server
        return [{**t, "addedAt": _iso(t["addedAt"]), "leaving": self._leaving(clock.get(t.get("ratingKey"))),
                 "plexUrl": plexdata.watch_url(server, t.get("ratingKey"))} for t in items]

    # ------------------------------------------------------------ search
    @staticmethod
    def _availability(media_info: Optional[dict]) -> str:
        status = (media_info or {}).get("status")
        if status in (4, 5):
            return "available"
        if status in (2, 3):
            return "requested"
        if status == 6:
            return "blocked"            # Seerr's blocklist: an admin ruled it out
        return "none"

    async def _waiting_seasons(self, tmdb: int) -> tuple:
        """Seasons of a show still waiting for an admin's decision: (numbers, all, latest)."""
        numbers, every, latest = set(), False, False
        for rec in (await all_requests()).values():
            media = rec.get("media") or {}
            if rec.get("status", "pending") != "pending" or media.get("media_type") != "tv" or media.get("id") != tmdb:
                continue
            seasons = rec.get("seasons")
            if isinstance(seasons, list) and rec.get("monitor") and len(seasons) == 1:
                numbers.update(seasons)
            elif isinstance(seasons, list):
                numbers.update(int(n) for n in seasons)
            elif seasons == "latest":
                latest = True
            else:
                every = True
        return numbers, every, latest

    async def tv_seasons(self, tid: str, r: dict) -> List[dict]:
        """Every season of a show and where it stands, so people can ask for the missing ones.

        status: "available" (all episodes on Plex), "partial" (some), "requested"
        (waiting for an admin, or with Seerr/Sonarr already), "upcoming" (not
        aired yet) or "none". Sonarr counts what is on disk; Seerr knows what
        is requested; Plexbie's own store knows what an admin hasn't decided yet.
        """
        ov = {x.get("seasonNumber"): x.get("status") for x in (r.get("mediaInfo") or {}).get("seasons") or []}
        try:
            series = (await self.progress._sonarr_series()).get(int(tid))
        except Exception as e:
            logger.info(f"portal: Sonarr unavailable for season status ({type(e).__name__})")
            series = None
        stats = {x.get("seasonNumber"): (x.get("statistics") or {}) for x in (series or {}).get("seasons", [])}
        numbers, every, latest = await self._waiting_seasons(int(tid))
        today = datetime.now(timezone.utc).date().isoformat()
        aired = [x["seasonNumber"] for x in r.get("seasons", [])
                 if x.get("seasonNumber") and not (x.get("airDate") and x["airDate"] > today)]
        newest = max(aired) if aired else None
        out = []
        for x in r.get("seasons", []):
            n = x.get("seasonNumber")
            if not n:
                continue
            eps = x.get("episodeCount") or 0
            files = (stats.get(n) or {}).get("episodeFileCount") or 0
            have = min(files, eps) if eps else files
            if (eps and have >= eps) or ov.get(n) == 5:
                status = "available"
            elif ov.get(n) in (2, 3) or every or n in numbers or (latest and n == newest):
                status = "requested"
            elif have or ov.get(n) == 4:
                status = "partial"
            elif x.get("airDate") and x["airDate"] > today:
                status = "upcoming"
            else:
                status = "none"
            out.append({"n": n, "episodes": eps, "have": have, "status": status})
        return out

    def _video_title(self, r: dict, kind: str) -> dict:
        date = r.get("releaseDate") or r.get("firstAirDate") or ""
        return {
            "kind": kind,
            "id": str(r.get("id")),
            "title": r.get("title") or r.get("name") or "Untitled",
            "year": date[:4],
            "poster": tmdb_art(r.get("posterPath")),
            "backdrop": tmdb_art(r.get("backdropPath"), "w1280"),
            "overview": r.get("overview") or "",
            "availability": self._availability(r.get("mediaInfo")),
        }

    async def search(self, q: str, kind: str) -> List[dict]:
        q = q.strip()[:120]
        if not q:
            return []
        if kind in ("audiobook", "ebook"):
            data = await self.cache.get(f"openlibrary:search:{q}", 600, lambda: self.services.openlibrary.get(
                "search.json", q=q, limit=20, fields="key,title,author_name,first_publish_year,cover_i"))
            return [{
                "kind": kind,
                "id": (d.get("key") or "").rsplit("/", 1)[-1],
                "title": d.get("title") or "Untitled",
                "author": ", ".join((d.get("author_name") or [])[:2]),
                "year": str(d.get("first_publish_year") or ""),
                "poster": ol_art(d.get("cover_i")),
                "availability": "none",
            } for d in data.get("docs", []) if d.get("key")]
        # quote, not quote_plus: Seerr's search reads "+" literally.
        data = await self._seerr("search?" + urlencode({"query": q, "page": 1}, quote_via=quote), ttl=120)
        want = "movie" if kind == "movie" else "tv"
        return [self._video_title(r, kind) for r in data.get("results", []) if r.get("mediaType") == want]

    async def search_all(self, q: str) -> Dict[str, List[dict]]:
        """One search box for everything: films and shows (one Seerr search, split) and
        books (Open Library, as audiobooks; the title page asks which format). A part
        that's off air comes back empty rather than failing the rest."""
        async def part(kind: str) -> List[dict]:
            try:
                return await self.search(q, kind)
            except Exception as e:
                logger.info(f"portal: {kind} search unavailable ({e})")
                return []
        movie, tv, book = await asyncio.gather(part("movie"), part("tv"), part("audiobook"))
        return {"movie": movie, "tv": tv, "book": book}

    async def popular(self, per_kind: int = 15) -> Dict[str, List[dict]]:
        """What's trending right now (TMDB, through Seerr): top films and shows, with
        Seerr's own "on Plex" / "requested" status on each. Topped up from the popular
        lists when trending is short of one kind. Cached for half an hour."""
        out: Dict[str, List[dict]] = {"movies": [], "tv": []}
        seen = set()

        def add(results: list) -> None:
            for r in results:
                kind = {"movie": "movie", "tv": "tv"}.get(r.get("mediaType"))
                key = "movies" if kind == "movie" else "tv"
                if not kind or (kind, r.get("id")) in seen or len(out[key]) >= per_kind or not r.get("posterPath"):
                    continue
                seen.add((kind, r.get("id")))
                out[key].append(self._video_title(r, kind))

        for page in (1, 2, 3, 4):
            add((await self._seerr(f"discover/trending?page={page}", ttl=1800)).get("results", []))
            if all(len(v) >= per_kind for v in out.values()):
                return out
        for path, kind in (("discover/movies?page=1", "movie"), ("discover/movies?page=2", "movie"),
                           ("discover/tv?page=1", "tv"), ("discover/tv?page=2", "tv")):
            add([{**r, "mediaType": r.get("mediaType") or kind} for r in (await self._seerr(path, ttl=1800)).get("results", [])])
        return out

    # ----------------------------------------------------------- discover
    #: The shelves on the Request page, per kind: (key, title, Seerr list). "trending" is
    #: Seerr's mixed list, filtered to the kind; top rated needs enough votes to mean it.
    SHELVES = {
        "movie": (("trending", "Trending this week", None), ("popular", "Popular", "discover/movies"),
                  ("upcoming", "Coming soon", "discover/movies/upcoming"),
                  ("top", "Top rated", "discover/movies?sortBy=vote_average.desc&voteCountGte=2000")),
        "tv": (("trending", "Trending this week", None), ("popular", "Popular", "discover/tv"),
               ("upcoming", "Coming soon", "discover/tv/upcoming"),
               ("top", "Top rated", "discover/tv?sortBy=vote_average.desc&voteCountGte=1000")),
    }
    DISCOVER_TTL = 1800

    def _titles(self, results: list, kind: str, seen: Optional[set] = None, langs: Optional[set] = None) -> List[dict]:
        out = []
        for r in results or []:
            mt = r.get("mediaType") or kind
            if mt != kind or not r.get("posterPath") or (seen is not None and r.get("id") in seen):
                continue
            if langs is not None and r.get("originalLanguage") not in langs:
                continue
            if seen is not None:
                seen.add(r.get("id"))
            out.append(self._video_title(r, kind))
        return out

    async def _shelf_page(self, kind: str, key: str, page: int, langs: Optional[set] = None) -> Dict[str, Any]:
        """One page of a shelf: {"titles", "more"}. key: a SHELVES key or genre-<id>.
        langs: only titles first made in these languages (the member's choice,
        portal/prefs), so a page reads a few more of Seerr's to stay full."""
        # Trending mixes films and shows, and a language choice leaves some out: more
        # of Seerr's pages make one of ours then.
        per = (2 if key == "trending" else 1) * (3 if langs else 1)
        if key == "trending":
            path = "discover/trending"
        elif key.startswith("genre-") and key[6:].isdigit():
            path = f"discover/{'movies' if kind == 'movie' else 'tv'}/genre/{int(key[6:])}"
        else:
            path = next((p for k, _, p in self.SHELVES[kind] if k == key and p), None)
            if path is None:
                raise LookupError("no such shelf")
        titles, more = [], True
        for p in range(page * per - per + 1, page * per + 1):
            data = await self._seerr(f"{path}{'&' if '?' in path else '?'}page={p}", ttl=self.DISCOVER_TTL)
            titles += self._titles(data.get("results"), kind, langs=langs)
            more = p < (data.get("totalPages") or 0)
            if not more:
                break
        return {"titles": titles, "more": more}

    async def discover(self, kind: str, langs: Optional[set] = None) -> Dict[str, Any]:
        """The Request page's shelves for films or shows, first page each, and the genres
        to browse. A title is on one shelf only (the first it's on). langs: see _shelf_page."""
        if kind not in self.SHELVES:
            raise LookupError("films or shows only")
        seen: set = set()
        shelves = []
        for key, title, _ in self.SHELVES[kind]:
            try:
                page = await self._shelf_page(kind, key, 1, langs)
            except Exception as e:
                logger.info(f"portal: discover shelf {kind}/{key} unavailable ({e})")
                continue
            titles = [t for t in page["titles"] if int(t["id"]) not in seen]
            seen.update(int(t["id"]) for t in titles)
            if titles:
                shelves.append({"key": key, "title": title, "titles": titles, "more": page["more"]})
        try:
            genres = [{"id": g["id"], "name": g["name"]} for g in
                      await self._seerr(f"discover/genreslider/{'movie' if kind == 'movie' else 'tv'}", ttl=86400) or []]
        except Exception as e:
            logger.info(f"portal: genres unavailable ({e})")
            genres = []
        return {"kind": kind, "shelves": shelves, "genres": genres}

    async def shelf(self, kind: str, key: str, page: int, langs: Optional[set] = None) -> Dict[str, Any]:
        """More of one shelf (or a genre): page 1, 2, ..."""
        if kind not in self.SHELVES or not 1 <= page <= 20:
            raise LookupError("no such page")
        return {"kind": kind, "key": key, "page": page, **await self._shelf_page(kind, key, page, langs)}

    async def similar(self, kind: str, tid: str) -> List[dict]:
        """"More like this" for a title page: Seerr's recommendations, else its similar list."""
        if kind not in ("movie", "tv") or not tid.isdigit():
            raise LookupError("unknown title")
        base = f"{'movie' if kind == 'movie' else 'tv'}/{tid}"
        for path in (f"{base}/recommendations", f"{base}/similar"):
            try:
                titles = self._titles((await self._seerr(path, ttl=self.DISCOVER_TTL)).get("results"), kind)
            except ServiceError:
                titles = []
            if titles:
                return titles[:20]
        return []

    async def title(self, kind: str, tid: str, user_id: Optional[int] = None, plex_account_id: Optional[str] = None) -> dict:
        if kind in ("movie", "tv"):
            if not tid.isdigit():
                raise LookupError("unknown title")
            try:
                r = await self._seerr(f"{'movie' if kind == 'movie' else 'tv'}/{tid}", ttl=300)
            except ServiceError as e:
                # Seerr answers an id TMDB doesn't know with a 404 or a 500
                # ("Unable to retrieve series"): that's "not found", not "down".
                if e.status in (404, 500):
                    raise LookupError("unknown title") from e
                raise
            t = self._video_title(r, kind)
            t["genres"] = [g.get("name") for g in r.get("genres", [])][:3]
            t["runtime"] = r.get("runtime")
            if kind == "tv":
                t["seasons"] = await self.tv_seasons(tid, r)
            try:
                tvdb = (r.get("externalIds") or {}).get("tvdbId")
                rk = await self.progress._plex_key("movie" if kind == "movie" else "show", f"tmdb://{tid}",
                                                   *([f"tvdb://{tvdb}"] if tvdb else []))
                t["plexUrl"] = plexdata.watch_url(self.services.plex_server, rk)
            except Exception as e:
                logger.info(f"portal: no Plex link for {t.get('title')} ({e})")
            if t["availability"] == "available":
                try:
                    clock = await self.countdown()
                    hit = next((c for c in clock.values() if str(c.get("tmdb")) == t["id"]), None)
                    t["leaving"] = self._leaving(hit)
                except Exception as e:
                    logger.info(f"portal: cleanup countdown unavailable ({e})")
        elif tid.startswith("shelf-"):
            book = (await self._shelf()).get(tid)
            if not book:
                raise LookupError("unknown title")
            t = shelf.public(book)
        else:
            work = await self.cache.get(f"openlibrary:work:{tid}", 3600,
                                        lambda: self.services.openlibrary.get(f"works/{quote(tid)}.json"))
            desc = work.get("description")
            t = {
                "kind": kind, "id": tid, "title": work.get("title") or "Untitled", "year": "",
                "poster": ol_art((work.get("covers") or [None])[0]),
                "overview": (desc.get("value") if isinstance(desc, dict) else desc or "")[:900],
                "availability": "none",
            }
            on_shelf = shelf.shelf_titles(await self._shelf())
            if t["title"].casefold() in on_shelf:
                t["availability"] = "available"
        if user_id or plex_account_id:
            mine = await self.my_requests(user_id, plex_account_id)
            ours = [m for m in mine if m["title"]["id"] == t["id"] and m["stage"] not in ("declined", "closed")]
            hit = max(ours, key=lambda m: m["slot"]) if ours else None
            if hit:
                t["yourRequest"] = {"slot": hit["slot"], "stage": hit["stage"]}
                if t["availability"] == "none":
                    t["availability"] = "requested"
        return t

    # ---------------------------------------------------------- requests
    async def _slots(self) -> Dict[str, dict]:
        records = await all_requests()
        ordered = sorted(records.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)
        return {key: {**rec, "_slot": n + 1} for n, (key, rec) in enumerate(ordered)}

    async def my_requests(self, user_id: Optional[int], plex_account_id: Optional[str] = None) -> List[dict]:
        """Requests made by this person: by Discord id, or by Plex account for people without Discord."""
        from portal import help as helpdesk
        records = await self._slots()
        open_help = await helpdesk.open_for()
        books: Dict[str, Any] = {}
        out = []
        for key, rec in records.items():
            mine = (user_id is not None and rec.get("user_id") == user_id) or (
                plex_account_id is not None and rec.get("plex_account_id") == plex_account_id)
            if mine:
                out.append(await self.request_row(key, rec, open_help, books))
        out.sort(key=lambda r: r["slot"], reverse=True)
        return out

    #: When Seerr can't pass a show on: its words for the request (detail, problem).
    NO_TVDB = ("Approved. It isn't on TheTVDB, so it can't be fetched automatically",
               "Not on TheTVDB, so it can't be fetched automatically. An admin will add it by hand.")
    SEERR_DROPPED = ("Approved, but Seerr no longer has the request",
                     "Seerr dropped this request before it reached Sonarr. An admin will look into it.")

    async def why_not_in_sonarr(self, rec: dict, media: dict) -> Optional[tuple]:
        """An approved show Sonarr doesn't have: whether Seerr can't pass it on (no
        TheTVDB entry) or has dropped the request. None when nothing's known to be wrong
        (it may simply be on its way)."""
        if rec.get("no_tvdb"):
            return self.NO_TVDB
        if not self.services.seerr.configured or not media.get("id"):
            return None
        try:
            tv = await self._seerr(f"tv/{int(media['id'])}", ttl=6 * 3600)
            if isinstance(tv, dict) and tv.get("name") and not (tv.get("externalIds") or {}).get("tvdbId"):
                return self.NO_TVDB
        except Exception as e:
            logger.info(f"portal: Seerr didn't say whether {media.get('name')} is on TheTVDB: {e}")
        rid = rec.get("overseerr_request_id")
        if rid:
            async def exists():
                try:
                    await self.services.seerr.get(f"request/{int(rid)}")
                    return True
                except ServiceError as e:
                    if e.status == 404:
                        return False
                    raise
            try:
                if not await self.cache.get(f"seerr:request:{rid}", 600, exists):
                    return self.SEERR_DROPPED
            except Exception as e:
                logger.info(f"portal: Seerr didn't say whether request {rid} is still there: {e}")
        return None

    #: What of a request's live progress its member sees (none of Plexbie's own marks).
    MEMBER_PROGRESS = ("percent", "eta", "detail", "problem", "partial", "seasons", "releaseDate", "releaseKind")

    async def request_row(self, key: str, rec: dict, open_help: Dict[str, dict], books: Dict[str, Any],
                          admin: bool = False) -> dict:
        """One request as members see it: its title, live stage and progress. Shared by
        My requests and the admins' All requests. `books` caches the shelf between rows.
        With `admin`, a problem is in Sonarr's or Radarr's own words."""
        from portal import help as helpdesk
        media = rec.get("media") or {}
        is_book = rec.get("media_type") in ("ebook", "audiobook", "both") or "open_library_key" in media
        if is_book:
            fmt = media.get("request_format") or rec.get("media_type") or "ebook"
            title = {
                "kind": "audiobook" if fmt == "audiobook" else "ebook",
                "id": (media.get("open_library_key") or "").rsplit("/", 1)[-1] or f"req-{key}",
                "title": media.get("title") or "Untitled",
                "author": media.get("author") or "",
                "year": str(media.get("year") or ""),
                "poster": None,
                "availability": "requested",
            }
        else:
            kind = "movie" if media.get("media_type") == "movie" else "tv"
            title = {
                "kind": kind,
                "id": str(media.get("id")),
                "title": media.get("title") or media.get("name") or "Untitled",
                "year": (media.get("release_date") or media.get("first_air_date") or "")[:4],
                "poster": tmdb_art(media.get("poster_path")),
                "availability": "requested",
            }
        status = rec.get("status", "pending")
        legacy = status == "pending" and predates_outcomes(rec)
        live: Dict[str, Any] = {}
        if status == "pending" and not legacy:
            stage = "requested"
        elif status == "declined":
            stage = "declined"
        elif is_book:
            if "titles" not in books:
                books["titles"] = shelf.shelf_titles(await self._shelf())
            live = await self.progress.book(media, books["titles"])
            stage = live.get("stage", "approved")
        else:
            live = await self.progress.video(media, rec.get("seasons"))
            if live.pop("notInSonarr", False):
                why = await self.why_not_in_sonarr(rec, media)
                if why:
                    live = {**live, "detail": why[0], "problem": why[1]}
            stage = live.get("stage", "approved")
        if status == "closed" and stage in ("approved", "searching", "upcoming", "requested"):
            # Cleared in bulk on 2026-10-02: old, unrecorded, and not pending in
            # Seerr. Unless it actually reached Plex, it's simply over.
            stage, live = "closed", {"detail": "Closed with the old backlog. Ask again if you still want it."}
        if legacy and stage in ("approved", "searching", "upcoming"):
            # Nothing downstream knows it: we can't tell approved-and-lost from
            # never decided, so say so rather than claim it is waiting.
            stage, live = "requested", {"detail": "Older request; its outcome wasn't recorded"}
        seasons = rec.get("seasons")
        if rec.get("monitor") and isinstance(seasons, list) and len(seasons) == 1:
            seasons = "latest"
        progress = {k: v for k, v in live.items() if k != "stage"}
        if admin:
            if progress.get("adminProblem"):
                progress["problem"] = progress.pop("adminProblem")
        else:
            progress = {k: v for k, v in progress.items() if k in self.MEMBER_PROGRESS}
        return {
            "id": key,
            "slot": rec["_slot"],
            # The open ticket on it, as its member sees it (no admins' notes), so they can answer.
            "help": {"id": open_help[key]["id"], "reason": open_help[key]["reason"], **helpdesk.member_view(open_help[key])}
            if key in open_help else None,
            "title": title,
            "stage": stage,
            "progress": progress or None,
            "requestedAt": _iso(rec.get("timestamp")),
            "updatedAt": _iso(rec.get("resolved_at") or rec.get("timestamp")),
            "seasons": seasons if not is_book else None,
            "format": (media.get("request_format") or rec.get("media_type")) if is_book else None,
        }

    # --------------------------------------------------------- community
    async def _credits(self) -> Dict[str, int]:
        from plugins.watch_party.models import WatchPartyCredit
        try:
            async with get_session() as session:
                rows = (await session.execute(select(WatchPartyCredit))).scalars().all()
            return {r.plex_username: r.total_duration for r in rows}
        except Exception as e:
            logger.info(f"portal: watch party credits unavailable ({e})")
            return {}

    async def _leaderboard(self) -> List[tuple]:
        async def load():
            users = []
            if self.services.tautulli.configured:
                users = await self.services.tautulli.users_table(length=200)
            aliases = await run_blocking(load_aliases)
            return standings(users, aliases, await self._credits()), aliases
        return await self.cache.get("leaderboard", 300, load)

    async def community(self, user_id: Optional[int], plex_name: Optional[str] = None) -> dict:
        board, aliases = await self._leaderboard()
        streaks = await run_blocking(load_streaks)
        try:
            playing = await self._now_playing()
        except Exception:
            playing = []
        on_air = [{
            "member": resolve_alias(s["plexUser"], aliases),
            "title": s["title"], "subtitle": s["subtitle"], "poster": s["poster"],
            "progress": s["progress"], "device": s["device"],
        } for s in playing]
        leaderboard = [{
            "name": name, "hours": round(seconds / 3600, 1),
            "streak": (streaks.get(name) or {}).get("current", 0),
        } for name, seconds in board[:10]]

        you = None
        if user_id or plex_name:
            from database.people import person_by_discord, person_by_name
            async with get_session() as session:
                row = await (person_by_discord(session, user_id) if user_id else person_by_name(session, plex_name))
            credits = await self._credits()
            if row:
                primary = resolve_alias(row.plex_username, aliases)
                names = [n for n, _ in board]
                rank = names.index(primary) + 1 if primary in names else None
                seconds = dict(board).get(primary, 0)
                st = streaks.get(row.plex_username) or streaks.get(primary) or {}
                you = {
                    "rank": rank, "hours": round(seconds / 3600, 1),
                    "streak": st.get("current", 0), "longestStreak": st.get("longest", 0),
                    "daysIdle": row.days_inactive or 0,
                    "removalAfterDays": self.config.inactivity_removal_days,
                    "topThree": bool(row.is_top_watcher) or (rank is not None and rank <= 3),
                    "watchPartyMinutes": round(credits.get(row.plex_username, 0) / 60),
                    "plexName": row.plex_username,
                }
        return {"onAir": on_air, "leaderboard": leaderboard, "you": you}
