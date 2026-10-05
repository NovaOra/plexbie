# path: core/plex_match.py
"""Putting right a film Plex matched to the wrong title.

Plex matched Obsession (2026), Curry Barker's 109-minute film, to a 2-minute
short of the same name, twice. When the file is plainly the requested film
(its running time is the film's, and its release name is the film's title),
Plexbie matches it to the requested film itself, as Plex's Fix Match does.
Anything less certain stays a help request for the admins.
"""
import time
from typing import Any, Dict, Optional

from core.blocking import run_blocking
from core.logging import get_logger

logger = get_logger(__name__)

RUNTIME_SLACK = 0.06      # of the film's length (at least 3 minutes): cuts and credits differ a little
SETTLE_TRIES, SETTLE_SECONDS = 12, 5   # Plex applies a match in the background


def _minutes(item) -> int:
    ms = getattr(item, "duration", None) or next((m.duration for m in getattr(item, "media", None) or [] if m.duration), 0)
    return round((ms or 0) / 60000)


def same_length(minutes: int, runtime: Any) -> bool:
    """Whether a file of this many minutes is a film of this runtime."""
    try:
        runtime = int(runtime or 0)
    except (TypeError, ValueError):
        return False
    return bool(minutes and runtime) and abs(minutes - runtime) <= max(3, runtime * RUNTIME_SLACK)


def _fix(server, rating_key: str, tmdb_id: int, runtime: Any) -> Optional[int]:
    """Blocking: match the Plex item to TMDB tmdb_id if its file is that film's length.
    Returns the file's minutes once Plex has the new match, else None."""
    item = server.fetchItem(int(rating_key))
    minutes = _minutes(item)
    if not same_length(minutes, runtime):
        logger.info(f"Not fixing Plex's match for {item.title}: the file is {minutes} min, the film {runtime} min")
        return None
    found = item.matches(title=f"tmdb-{tmdb_id}")
    if len(found) != 1:
        logger.info(f"Not fixing Plex's match for {item.title}: Plex found {len(found)} matches for TMDB {tmdb_id}")
        return None
    item.fixMatch(searchResult=found[0])
    for _ in range(SETTLE_TRIES):
        item = server.fetchItem(int(rating_key))
        if f"tmdb://{tmdb_id}" in [g.id for g in getattr(item, "guids", None) or []]:
            return minutes
        time.sleep(SETTLE_SECONDS)
    logger.info(f"Plex hasn't applied the match for {item.title} yet")
    return None


async def fix_film_match(services, movie: Dict[str, Any], rating_key: str) -> Optional[str]:
    """Plex lists the requested film's file as another film: match it to the requested
    one when the file is plainly that film. Returns a sentence for the admins, or
    None (not certain, or Plex couldn't), leaving it to them."""
    from core.verified_search import is_own_title
    server = getattr(services, "plex_server", None)
    if server is None or not movie or not rating_key or not movie.get("tmdbId"):
        return None
    scene = (movie.get("movieFile") or {}).get("sceneName")
    if scene:
        parsed = await services.radarr.get("parse", title=scene)
        titles = ((parsed or {}).get("parsedMovieInfo") or {}).get("movieTitles") or []
        if titles and not is_own_title(titles, movie):
            return None             # the download itself is another film's: the admins decide
    try:
        minutes = await run_blocking(_fix, server, rating_key, int(movie["tmdbId"]), movie.get("runtime"))
    except Exception as e:
        logger.warning(f"Fixing Plex's match for {movie.get('title')} failed: {e}")
        return None
    if not minutes:
        return None
    name = f"{movie.get('title')} ({movie.get('year')})" if movie.get("year") else movie.get("title")
    logger.info(f"Fixed Plex's match for {name}")
    return (f"Plex had matched {name} to another film. Its file runs {minutes} minutes, the length of "
            f"{name}, so Plexbie matched it to the right one.")
