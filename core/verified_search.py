# path: core/verified_search.py
"""Searching that checks a release really is the film or show that was asked for.

Indexers answer an ID search for a title they don't know yet with whatever is
new (Obsession, 2026: some 300 unrelated films), and some label a namesake's
release with the requested ID. Radarr takes that label, or the title with
either of the film's years, as proof: it grabbed "Obsession - Du sollst mich
lieben" (another film) for Obsession. The release's own title gives it away.

So, for a film:
  1. By ID: Radarr's search for it, keeping only releases whose title (as
     Radarr reads it) is the film's own title or one of its alternative titles.
     Radarr's favourite of those is grabbed.
  2. Nothing? The admins are asked, on a help request, whether to search by name.
  3. By name (an admin said yes): NZBHydra, "<title> <year>". Releases whose
     title and year are the film's are offered to Radarr, best first, until it
     takes one; what happened is reported on the help request.
For a show, Sonarr's season search (core/season_search) is the ID step; by name
searches "<show> S01" and offers season packs before single episodes.

Downloads of requested films are checked as well (wrong_movie_grabs): one
whose title isn't the film's is removed and blocklisted, whoever grabbed it.
"""
import asyncio
import re
import unicodedata
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

from core.logging import get_logger
from core.releases import movie_upcoming

logger = get_logger(__name__)

PARSE_LIMIT = 40          # releases read (Radarr/Sonarr "parse") per name search
PUSH_LIMIT = 6            # releases offered to Radarr/Sonarr per film, season or episode
SETTLE_AFTER_ADD = 180    # Radarr's own search-on-add, before Plexbie checks what it grabbed
POLL_SECONDS = 30

_FOLD = str.maketrans({"ı": "i", "ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "đ": "d", "ł": "l", "&": "and"})
_tasks: Set[asyncio.Task] = set()


def norm(text: Any) -> str:
    """A title reduced to letters and digits: case, accents, spaces and dots don't count."""
    text = unicodedata.normalize("NFKD", str(text or "").casefold().translate(_FOLD))
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c))


def own_titles(item: Dict[str, Any]) -> Set[str]:
    """A film's or show's titles (Radarr/Sonarr record), normalised."""
    names = [item.get("title"), item.get("originalTitle"), item.get("sortTitle")]
    names += [a.get("title") for a in item.get("alternateTitles") or []]
    return {n for n in map(norm, names) if n}


def is_own_title(titles: Iterable[str], item: Dict[str, Any]) -> bool:
    """Whether a release's title, as Radarr/Sonarr read it, is this one's. Two of
    its titles run together count ("Saplantı Obsession"); a longer title that
    merely starts with it doesn't ("Obsession Du sollst mich lieben")."""
    own = own_titles(item)
    for title in titles or []:
        n = norm(title)
        if n and (n in own or any(n.startswith(a) and n[len(a):] in own for a in own)):
            return True
    return False


def quality_ranks(profile: Dict[str, Any]) -> Dict[int, int]:
    """Quality id -> its place in the profile (higher is better), allowed qualities only."""
    ranks: Dict[int, int] = {}
    for i, item in enumerate((profile or {}).get("items") or []):
        if not item.get("allowed"):
            continue
        for q in [item] + list(item.get("items") or []):
            if q.get("quality"):
                ranks[int(q["quality"]["id"])] = i
    return ranks


def _rank_key(parsed: Dict[str, Any], info: Dict[str, Any], release: Dict[str, Any],
              ranks: Dict[int, int], floor: int) -> Optional[tuple]:
    """How good a parsed release is, to sort best first: (quality rank, custom format
    score, size). None when the profile doesn't allow its quality or it scores under
    the profile's floor."""
    quality = ((info.get("quality") or {}).get("quality") or {}).get("id")
    score = parsed.get("customFormatScore") or 0
    if quality not in ranks or score < floor:
        return None
    return ranks[quality], score, release.get("size") or 0


def _iso(date: Optional[str]) -> Optional[str]:
    try:
        return parsedate_to_datetime(date).isoformat() if date else None
    except (TypeError, ValueError):
        return None


def _taken(verdict: Dict[str, Any]) -> bool:
    """Grabbed, or held back by a delay profile (it is grabbed when that runs out)."""
    return bool(verdict.get("approved") or verdict.get("temporarilyRejected"))


def _push_body(release: Dict[str, Any], **ids) -> Dict[str, Any]:
    return {"title": release["title"], "downloadUrl": release["link"], "protocol": "usenet",
            "publishDate": _iso(release.get("pubDate")), "size": release.get("size") or 0,
            "indexer": "NZBHydra", **ids}


def _imdb_number(imdb: Any) -> Optional[int]:
    digits = re.sub(r"\D", "", str(imdb or ""))
    return int(digits) if digits else None


def _reasons(rejections: Iterable[str], limit: int = 2) -> str:
    seen: List[str] = []
    for r in rejections:
        if r and r not in seen:
            seen.append(r)
    return "; ".join(seen[:limit])


# ----------------------------------------------------------------- films

async def movie_by_id(radarr, movie: Dict[str, Any]) -> Dict[str, Any]:
    """Radarr's search for the film, its favourite release that really is the film.
    Returns grabbed (the release title, or None), seen, and namesakes: releases
    Radarr would have taken that are another film's."""
    releases = await radarr.releases(movieId=movie["id"])
    approved = [r for r in releases if r.get("approved")]
    own = [r for r in approved if is_own_title(r.get("movieTitles") or [], movie)]
    namesakes = sorted({r["title"] for r in approved if r not in own})
    if namesakes:
        logger.info(f"{movie.get('title')}: skipped {len(namesakes)} release(s) of another film, e.g. {namesakes[0]}")
    if own:
        await radarr.post("release", {"guid": own[0]["guid"], "indexerId": own[0]["indexerId"]})
        logger.info(f"{movie.get('title')}: grabbed {own[0]['title']} (found by its IDs)")
        return {"grabbed": own[0]["title"], "seen": len(releases), "namesakes": namesakes}
    turned_down = [x for r in releases if is_own_title(r.get("movieTitles") or [], movie) for x in r.get("rejections") or []]
    return {"grabbed": None, "seen": len(releases), "namesakes": namesakes, "why": _reasons(turned_down)}


async def wrong_movie_grabs(radarr, movie: Dict[str, Any], queue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Downloads in Radarr's queue for this film whose title is another film's."""
    wrong = []
    for item in queue:
        if item.get("movieId") != movie.get("id") or not item.get("title"):
            continue
        parsed = await radarr.get("parse", title=item["title"])
        titles = ((parsed or {}).get("parsedMovieInfo") or {}).get("movieTitles") or []
        if titles and not is_own_title(titles, movie):
            wrong.append(item)
    return wrong


async def remove_grab(radarr, item: Dict[str, Any]) -> None:
    """Take a download out of the queue and the download client, and blocklist the
    release so no search picks it again."""
    await radarr.delete(f"queue/{item['id']}", removeFromClient="true", blocklist="true", skipRedownload="true")
    logger.info(f"Removed and blocklisted {item.get('title')}: it isn't the film it was grabbed for")


async def verify_movie(radarr, movie: Dict[str, Any]) -> Dict[str, Any]:
    """Make sure the film is on its way: wrong downloads out (upgrades of a file on
    disk too), then, with nothing on disk or downloading, the search by ID. Returns
    state: "on disk", "downloading" or "searched" (with movie_by_id's result), and
    the titles removed."""
    movie = await radarr.get(f"movie/{movie['id']}") or movie
    queue = [q for q in await radarr.queue() if q.get("movieId") == movie["id"]]
    wrong = await wrong_movie_grabs(radarr, movie, queue)
    for item in wrong:
        await remove_grab(radarr, item)
    removed = [w["title"] for w in wrong]
    if movie.get("hasFile"):
        return {"state": "on disk", "removed": removed}
    if len(queue) > len(wrong):
        return {"state": "downloading", "removed": removed}
    upcoming = movie_upcoming(movie)
    if upcoming:                    # nothing real to find yet: anything posted now is fake
        return {"state": "upcoming", "removed": removed, "when": upcoming["detail"]}
    return {"state": "searched", "removed": removed, **await movie_by_id(radarr, movie)}


async def movie_by_name(radarr, hydra, movie: Dict[str, Any]) -> Dict[str, Any]:
    """NZBHydra by title and year; releases that are the film, best first, offered
    to Radarr until it takes one. The release year has to be one of the film's;
    its main year is tried first. Returns grabbed, queries, matching and why."""
    profile = await radarr.get(f"qualityprofile/{movie.get('qualityProfileId')}")
    ranks, floor = quality_ranks(profile), (profile or {}).get("minFormatScore") or 0
    own = own_titles(movie)
    names = list(dict.fromkeys(n for n in (movie.get("title"), movie.get("originalTitle")) if n))
    years = [y for y in dict.fromkeys((movie.get("year"), movie.get("secondaryYear"))) if y]
    ids = {k: v for k, v in (("tmdbId", movie.get("tmdbId")), ("imdbId", _imdb_number(movie.get("imdbId")))) if v}
    queries, matching, rejections = [], 0, []
    for year in years or [None]:
        candidates, parsed_titles = [], set()
        for name in names:
            query = f"{name} {year}" if year else name
            queries.append(query)
            for release in await hydra.search(query, ["2000"]):
                key = norm(release["title"])
                if key in parsed_titles or not any(key.startswith(t) for t in own) or len(parsed_titles) >= PARSE_LIMIT:
                    continue
                parsed_titles.add(key)
                parsed = await radarr.get("parse", title=release["title"]) or {}
                info = parsed.get("parsedMovieInfo") or {}
                if not is_own_title(info.get("movieTitles") or [], movie) or (years and info.get("year") not in years):
                    continue
                matching += 1
                rank = _rank_key(parsed, info, release, ranks, floor)
                if rank:
                    candidates.append((*rank, release))
        candidates.sort(key=lambda c: c[:3], reverse=True)
        for *_, release in candidates[:PUSH_LIMIT]:
            verdict = await radarr.push(_push_body(release, **ids))
            if _taken(verdict):
                logger.info(f"{movie.get('title')}: grabbed {release['title']} (found by name)")
                return {"grabbed": release["title"], "queries": queries, "matching": matching}
            rejections += verdict.get("rejections") or []
        if candidates:
            break                       # the main year had the film; don't reach for a namesake's year
    return {"grabbed": None, "queries": queries, "matching": matching, "why": _reasons(rejections)}


# ----------------------------------------------------------------- shows

async def show_by_name(sonarr, hydra, series: Dict[str, Any], seasons: Iterable[int]) -> Dict[str, Any]:
    """NZBHydra, "<show> S01" per season: a season pack if Sonarr takes one, else the
    missing episodes one by one. Returns grabbed (titles), queries, matching, nothing
    (seasons none of which was found) and why."""
    from core.season_search import missing_episodes
    profile = await sonarr.get(f"qualityprofile/{series.get('qualityProfileId')}")
    ranks, floor = quality_ranks(profile), (profile or {}).get("minFormatScore") or 0
    own = own_titles(series)
    name = re.sub(r"[()]", "", series.get("title") or "").strip()
    ids = {"tvdbId": series["tvdbId"]} if series.get("tvdbId") else {}
    grabbed, queries, nothing, rejections, matching = [], [], [], [], 0
    for season in [int(s) for s in seasons]:
        missing = {int(e["episodeNumber"]) for e in await missing_episodes(sonarr, series["id"], season)}
        if not missing:
            continue
        query = f"{name} S{season:02d}"
        queries.append(query)
        packs, singles, parsed_titles = [], [], set()
        for release in await hydra.search(query, ["5000"]):
            key = norm(release["title"])
            if key in parsed_titles or not any(key.startswith(t) for t in own) or len(parsed_titles) >= PARSE_LIMIT:
                continue
            parsed_titles.add(key)
            parsed = await sonarr.get("parse", title=release["title"]) or {}
            info = parsed.get("parsedEpisodeInfo") or {}
            if not is_own_title([info.get("seriesTitle") or ""], series) or info.get("seasonNumber") != season:
                continue
            episodes = {int(n) for n in info.get("episodeNumbers") or []}
            if not info.get("fullSeason") and not episodes & missing:
                continue
            matching += 1
            rank = _rank_key(parsed, info, release, ranks, floor)
            if rank:
                (packs if info.get("fullSeason") else singles).append((*rank, release, episodes))
        for group in (packs, singles):
            group.sort(key=lambda c: c[:3], reverse=True)
        got = None
        for *_, release, _eps in packs[:PUSH_LIMIT]:
            verdict = await sonarr.push(_push_body(release, **ids))
            if _taken(verdict):
                got = release["title"]
                break
            rejections += verdict.get("rejections") or []
        if got:
            grabbed.append(got)
            continue
        found_any = False
        for episode in sorted(missing):
            for *_, release, eps in [s for s in singles if episode in s[4]][:PUSH_LIMIT]:
                verdict = await sonarr.push(_push_body(release, **ids))
                if _taken(verdict):
                    grabbed.append(release["title"])
                    missing -= eps
                    found_any = True
                    break
                rejections += verdict.get("rejections") or []
        if not found_any:
            nothing.append(season)
    if grabbed:
        logger.info(f"{series.get('title')}: grabbed {len(grabbed)} release(s) found by name")
    return {"grabbed": grabbed, "queries": queries, "matching": matching, "nothing": nothing, "why": _reasons(rejections)}


# ----------------------------------------------------------------- reports

def describe_by_id(title: str, result: Dict[str, Any]) -> str:
    """What the search by ID did, for a help request or the admin channel."""
    removed = result.get("removed") or []
    stopped = f"Stopped {removed[0]}{' and more' if len(removed) > 1 else ''}: it isn't {title}. " if removed else ""
    if result.get("grabbed"):
        return f"{stopped}Found {title} by its IDs and grabbed {result['grabbed']}."
    if result.get("state") == "downloading":
        return f"{stopped}{title} is downloading."
    if result.get("state") == "on disk":
        return f"{stopped}{title} is already on disk."
    if result.get("state") == "upcoming":
        return f"{stopped}{title} isn't out yet, so there's nothing real to find. {result.get('when', '')}".strip()
    namesakes = result.get("namesakes") or []
    other = f" {len(namesakes)} were another film with the same name (e.g. {namesakes[0]})." if namesakes else ""
    why = f" Releases of it were turned down: {result['why']}." if result.get("why") else ""
    return (f"{stopped}Searching by its IDs found nothing Plexbie could grab for {title}: "
            f"{result.get('seen', 0)} releases came back.{other}{why}")


def describe_by_name(title: str, result: Dict[str, Any]) -> str:
    queries = " and ".join(f'"{q}"' for q in result.get("queries") or []) or "its name"
    grabbed = result.get("grabbed")
    if grabbed:
        got = grabbed if isinstance(grabbed, str) else (grabbed[0] if len(grabbed) == 1 else f"{len(grabbed)} releases")
        rest = f" Season {', '.join(map(str, result['nothing']))} had nothing." if result.get("nothing") else ""
        return f"Searched NZBHydra for {queries}: found {title} and grabbed {got}.{rest}"
    why = f" Turned down: {result['why']}." if result.get("why") else ""
    seen = (f"{result['matching']} release(s) were {title}, none of them acceptable." if result.get("matching")
            else f"nothing there was {title}.")
    return f"Searched NZBHydra for {queries}: {seen}{why}"


# ----------------------------------------------------------------- background

def background(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def find_movie(radarr, tmdb_id: Any) -> Optional[Dict[str, Any]]:
    found = await radarr.get("movie", tmdbId=tmdb_id)
    return (found[0] if found else None) if isinstance(found, list) else found


def follow_up_new_movie(services, *, tmdb_id: int, title: str,
                        on_nothing: Optional[Callable[[str], Awaitable[None]]] = None,
                        wait_for_radarr: float = 600, settle: float = SETTLE_AFTER_ADD, on_missing=None) -> None:
    """After a film is approved: once Radarr has it and its own search has run,
    check what that grabbed and search by ID if there's nothing (right). Nothing
    found: on_nothing hears why, to ask the admins about searching by name."""
    radarr = services.radarr

    async def run():
        movie, waited = None, 0.0
        while movie is None and waited <= wait_for_radarr:
            try:
                movie = await find_movie(radarr, tmdb_id)
            except Exception as e:
                logger.info(f"Waiting for {title} in Radarr: {e}")
            if movie is None:
                await asyncio.sleep(POLL_SECONDS)
                waited += POLL_SECONDS
        if movie is None:
            logger.info(f"{title} didn't show up in Radarr after Seerr had it; the admins are told")
            if on_missing:
                await on_missing()
            return
        await asyncio.sleep(settle)
        try:
            result = await verify_movie(radarr, movie)
        except Exception as e:
            logger.warning(f"Checking {title} in Radarr failed: {e}")
            return
        if result["state"] == "searched" and not result.get("grabbed") and on_nothing:
            try:
                await on_nothing(describe_by_id(title, result))
            except Exception as e:
                logger.warning(f"Reporting that {title} can't be found failed: {e}")

    background(run())
