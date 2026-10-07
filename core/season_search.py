# path: core/season_search.py
"""Search a season in Sonarr, falling back to one episode at a time.

Sonarr's season search asks indexers for the season as one release (a "season
pack"). Plenty of seasons only exist as single episodes, so the search finds
nothing and the request sits there, while searching episodes individually
would have worked. So:

  1. Season search, and wait for Sonarr to finish it.
  2. Nothing grabbed? Search the first PROBE missing episodes on their own.
  3. Those found? Search the rest of the season episode by episode.
  4. Still nothing? Report "nothing" so the admins hear about it
     (a "Can't be found" help request when there is a request to attach it to).

Runs in the background: approving a request or pressing "Search again" doesn't
wait for indexers.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set

from core.logging import get_logger

logger = get_logger(__name__)

PROBE = 2                  # episodes tried on their own before searching all of them
#: Episodes "airing" this soon are searched too. Streaming services drop at
#: midnight while TVDB/Sonarr often list a later time or the next day, and
#: Sonarr's own search skips anything it thinks hasn't aired - which is how a
#: released season sat unsearched until someone searched by hand.
AIR_GRACE = timedelta(hours=36)
POLL_SECONDS = 15          # how often to ask Sonarr whether a search has finished
COMMAND_TIMEOUT = 900      # give up waiting on one search after this long
SETTLE_SECONDS = 20        # grabs reach the queue a moment after the search ends

_tasks: Set[asyncio.Task] = set()


def _cutoff() -> str:
    return (datetime.now(timezone.utc) + AIR_GRACE).isoformat()


async def missing_episodes(sonarr, series_id: int, season: int) -> List[Dict[str, Any]]:
    """Aired (or about to air, see AIR_GRACE) episodes of the season with no file yet, in order."""
    now = _cutoff()
    eps = [e for e in await sonarr.episodes(series_id)
           if int(e.get("seasonNumber", -1)) == int(season) and not e.get("hasFile")
           and e.get("airDateUtc") and e["airDateUtc"] <= now]
    return sorted(eps, key=lambda e: int(e.get("episodeNumber", 0)))


async def seasons_with_missing(sonarr, series_id: int) -> List[int]:
    """Seasons (not specials) that have aired (or are about to) episodes without a file."""
    now = _cutoff()
    return sorted({int(e["seasonNumber"]) for e in await sonarr.episodes(series_id)
                   if int(e.get("seasonNumber", 0)) > 0 and not e.get("hasFile")
                   and e.get("airDateUtc") and e["airDateUtc"] <= now})


async def _wait(sonarr, command: Optional[dict]) -> None:
    cid = (command or {}).get("id")
    if cid:
        waited = 0
        while waited < COMMAND_TIMEOUT:
            state = await sonarr.get(f"command/{cid}")
            if (state or {}).get("status") in ("completed", "failed", "aborted", "cancelled", "orphaned"):
                break
            await asyncio.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
    await asyncio.sleep(SETTLE_SECONDS)


async def _grabbed(sonarr, series_id: int, episode_ids: Iterable[int]) -> Set[int]:
    """Which of these episodes are downloading now (in Sonarr's queue) or already on disk."""
    wanted = set(episode_ids)
    got = {int(r["episodeId"]) for r in await sonarr.queue(includeEpisode="true")
           if r.get("seriesId") == series_id and r.get("episodeId") in wanted}
    got |= {int(e["id"]) for e in await sonarr.episodes(series_id) if e.get("id") in wanted and e.get("hasFile")}
    return got


async def search_season(sonarr, series_id: int, season: int, *, season_first: bool = True) -> str:
    """Run the search; returns "complete", "unaired" (missing episodes, none out yet),
    "season", "episodes" or "nothing"."""
    missing = await missing_episodes(sonarr, series_id, season)
    if not missing:
        later = [e for e in await sonarr.episodes(series_id)
                 if int(e.get("seasonNumber", -1)) == int(season) and not e.get("hasFile")]
        return "unaired" if later else "complete"
    ids = [int(e["id"]) for e in missing]
    if season_first:
        await _wait(sonarr, await sonarr.command("SeasonSearch", seriesId=series_id, seasonNumber=int(season)))
        if await _grabbed(sonarr, series_id, ids):
            return "season"
        logger.info(f"Sonarr series {series_id} season {season}: no season release found, trying single episodes")
    probe, rest = ids[:PROBE], ids[PROBE:]
    await _wait(sonarr, await sonarr.command("EpisodeSearch", episodeIds=probe))
    if not await _grabbed(sonarr, series_id, probe):
        logger.info(f"Sonarr series {series_id} season {season}: single episodes not found either")
        return "nothing"
    if rest:
        await sonarr.command("EpisodeSearch", episodeIds=rest)
    logger.info(f"Sonarr series {series_id} season {season}: searching {len(ids)} episodes one by one")
    return "episodes"


async def search_seasons(services, series_id: int, seasons: Iterable[int], *, title: str = "",
                         season_first: bool = True, unaired: Optional[List[int]] = None) -> List[int]:
    """Search each season (see search_season); returns the seasons nobody has.
    Seasons with nothing out yet go in `unaired` when given, never in the result."""
    nothing = []
    for season in [int(s) for s in seasons]:
        try:
            outcome = await search_season(services.sonarr, series_id, season, season_first=season_first)
        except Exception as e:      # in the background: never let it die silently
            logger.warning(f"Searching {title or series_id} season {season} failed: {e}")
            continue
        if outcome == "nothing":
            nothing.append(season)
        elif outcome == "unaired" and unaired is not None:
            unaired.append(season)
    return nothing


def _background(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def start(services, series_id: int, seasons: Iterable[int], *, title: str = "",
          on_nothing: Optional[Callable[[List[int]], Awaitable[None]]] = None, season_first: bool = True) -> None:
    """Search these seasons in the background; call on_nothing with the seasons nobody has."""
    seasons = [int(s) for s in seasons]
    if not seasons:
        return

    async def run():
        nothing = await search_seasons(services, series_id, seasons, title=title, season_first=season_first)
        if nothing and on_nothing:
            try:
                await on_nothing(nothing)
            except Exception as e:
                logger.warning(f"Reporting that {title or series_id} can't be found failed: {e}")
    _background(run())


async def open_not_found_help(bot, request_key: Any, seasons: List[int]) -> None:
    """Open a "Can't be found" help request for the admins on this request (once),
    asking whether to search by name."""
    await open_help(bot, request_key, seasons=seasons, reason="notfound", status_now="Nothing found", offer="name",
                    note=f"Plexbie searched season {', '.join(map(str, seasons))} by the show's IDs, as a whole and "
                         "episode by episode; the indexers have nothing yet. Search by name instead?")


async def open_movie_not_found_help(bot, request_key: Any, report: str) -> None:
    """A film's search by ID found nothing that is the film: ask the admins (once)
    whether to search by name."""
    await open_help(bot, request_key, seasons=None, reason="notfound", status_now="Nothing found", offer="name",
                    note=f"{report} Search by name instead?")


async def open_help(bot, request_key: Any, *, seasons: Any, reason: str, note: str, status_now: str,
                    offer: Optional[str] = None, admin_note: Optional[str] = None) -> Optional[dict]:
    """Open a help request for the admins on this request, on Plexbie's own
    initiative (once: not while one is open), and tell them. `note` is the
    ticket's first entry, which the member sees; `admin_note`, if given, is what
    the admins are told instead."""
    from database.request_store import get_request
    from portal import help as helpdesk
    if request_key is None or await helpdesk.open_for({str(request_key)}):
        return None
    rec = await get_request(int(request_key))
    if not rec:
        return None
    actions = getattr(bot, "portal_actions", None)
    media = rec.get("media") or {}
    slot = 0
    if actions:
        try:
            slot = (await actions.data._slots()).get(str(request_key), {}).get("_slot", 0)
        except Exception:
            pass
    user = {"user": {"name": rec.get("requester_name") or "Plexbie"}, "discordId": rec.get("user_id"),
            "plexAccountId": rec.get("plex_account_id"), "plexName": rec.get("requester_name")}
    h = await helpdesk.create(
        request_key=str(request_key), slot=slot, title=media.get("title") or media.get("name") or "Untitled",
        kind=media.get("media_type") or "tv", seasons=seasons, user=user, reason=helpdesk.REASONS.get(reason) or helpdesk.PLEXBIE_REASONS[reason],
        note=note, status_now=status_now, offer=offer)
    if actions:
        await actions._tell_admins_about_help(h, note=admin_note)
    else:
        # No website running: the admin channel still hears about it.
        channel_id = bot.services.config.admin_channel_id if getattr(bot, "services", None) else None
        channel = bot.get_channel(int(channel_id)) if channel_id else None
        if channel:
            try:
                await channel.send(f"🆘 **{h['title']}**: {h['reason']}. {admin_note or note}")
            except Exception as e:
                logger.warning(f"Could not tell the admin channel about {h['title']}: {e}")
    return h



async def first_air(sonarr, series_id: int, seasons: Optional[Iterable[int]]) -> Optional[datetime]:
    """When the earliest missing episode of these seasons airs, if it hasn't yet."""
    wanted = None if seasons is None else {int(n) for n in seasons}
    times = []
    for e in await sonarr.episodes(series_id):
        n = int(e.get("seasonNumber", 0))
        if n > 0 and (wanted is None or n in wanted) and not e.get("hasFile") and e.get("airDateUtc"):
            try:
                times.append(datetime.fromisoformat(e["airDateUtc"].replace("Z", "+00:00")))
            except ValueError:
                continue
    future = [t for t in times if t > datetime.now(timezone.utc)]
    return min(future) if future else None


def follow_up_new_show(services, *, tmdb_id: int, seasons: Optional[List[int]], title: str,
                       on_nothing: Optional[Callable[[List[int]], Awaitable[None]]] = None,
                       wait_for_sonarr: float = 600, retries: int = 3,
                       on_missing: Optional[Callable[[], Awaitable[None]]] = None) -> None:
    """After Seerr adds a show to Sonarr: search it ourselves, with the
    episode-by-episode fallback. Sonarr's search-on-add skips episodes it thinks
    haven't aired; if they really haven't, try again a couple of hours after the
    first one airs (up to `retries` times) instead of reporting it can't be found."""
    sonarr = services.sonarr

    async def run():
        series = None
        waited = 0
        while waited <= wait_for_sonarr and series is None:
            try:
                series = next((x for x in await sonarr.series() if x.get("tmdbId") == tmdb_id), None)
            except Exception as e:
                logger.info(f"Waiting for {title} in Sonarr: {e}")
            if series is None:
                await asyncio.sleep(POLL_SECONDS * 2)
                waited += POLL_SECONDS * 2
        if series is None:
            logger.info(f"{title} didn't show up in Sonarr after Seerr had it; the admins are told")
            if on_missing:
                await on_missing()
            return
        for attempt in range(retries + 1):
            asked = list(seasons) if seasons else sorted({int(e["seasonNumber"]) for e in await sonarr.episodes(series["id"])
                                                         if int(e.get("seasonNumber", 0)) > 0 and not e.get("hasFile")})
            later: List[int] = []
            targets = await search_seasons(services, series["id"], asked, title=title, unaired=later) if asked else []
            if not targets and not later:
                return                         # found and grabbing, or nothing missing
            airs = await first_air(sonarr, series["id"], later or targets)
            if not later or airs is None or attempt == retries:
                if targets and on_nothing:
                    try:
                        await on_nothing(targets)
                    except Exception as e:
                        logger.warning(f"Reporting that {title} can't be found failed: {e}")
                return
            delay = max(60.0, (airs - datetime.now(timezone.utc)).total_seconds() + 2 * 3600)
            logger.info(f"{title} hasn't aired yet; searching again {airs:%Y-%m-%d %H:%M} UTC + 2 h")
            await asyncio.sleep(min(delay, 7 * 24 * 3600))

    _background(run())
