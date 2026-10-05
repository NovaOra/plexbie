# path: portal/help.py
"""Help requests: "something's wrong with my request" from the person who made it.

Downloads get stuck in Sonarr or Radarr, the wrong version arrives, a season
is missing. Asking for help on the request itself gives the admins everything
at once: the request number and title, who asked, what they say is wrong, and
what Plexbie can see right now. It goes to the Discord admin channel and, as a
phone/browser alert, to admins who turned alerts on. Admins can search again
(Sonarr/Radarr, by the title's IDs), search by name (NZBHydra; see
core/verified_search) or mark it resolved with a reply, which reaches the person
the usual way (Discord DM, else alert or email).
"""
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.logging import get_logger
from database.kv_store import kv_get, kv_get_all, kv_set
from database.request_store import get_request

logger = get_logger(__name__)

NAMESPACE = "help_requests"

#: What can be wrong, as the person picks it.
REASONS = {
    "stuck": "Stuck downloading",
    "notfound": "Can't be found",
    "notonplex": "Downloaded, but not on Plex",
    "wrongfilm": "Plex says it's a different film",
    "quality": "Wrong version or quality",
    "episodes": "Wrong or missing episodes",
    "playback": "Won't play on Plex",
    "other": "Something else",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def owns(rec: dict, user: dict) -> bool:
    """Whether this signed-in person made this request."""
    if user.get("discordId") and str(rec.get("user_id") or "") == str(user["discordId"]):
        return True
    return bool(user.get("plexAccountId")) and str(rec.get("plex_account_id") or "") == str(user["plexAccountId"])


async def open_for(request_keys: Optional[set] = None) -> Dict[str, dict]:
    """Open help requests by request key."""
    out = {}
    for hid, h in (await kv_get_all(NAMESPACE)).items():
        if isinstance(h, dict) and h.get("status") == "open" and (request_keys is None or h.get("request") in request_keys):
            out[h["request"]] = {**h, "id": hid}
    return out


async def all_help() -> List[dict]:
    rows = [{**h, "id": hid} for hid, h in (await kv_get_all(NAMESPACE)).items() if isinstance(h, dict)]
    rows.sort(key=lambda h: (h.get("status") != "open", h.get("created_at") or ""), reverse=False)
    opened = sorted((h for h in rows if h["status"] == "open"), key=lambda h: h["created_at"], reverse=True)
    closed = sorted((h for h in rows if h["status"] != "open"), key=lambda h: h.get("resolved_at") or "", reverse=True)[:20]
    return opened + closed


async def create(*, request_key: str, slot: int, title: str, kind: str, seasons: Any, user: dict,
                 reason: str, note: str, status_now: str, offer: Optional[str] = None,
                 opened_by: Optional[str] = None, quiet: bool = False) -> dict:
    hid = secrets.token_hex(6)
    rec = {
        "request": request_key, "slot": slot, "title": title, "kind": kind, "seasons": seasons,
        "who": user["user"].get("name") or "Someone",
        "discord_id": user.get("discordId"), "plex_account_id": user.get("plexAccountId"), "plex_name": user.get("plexName"),
        "reason": reason, "note": note[:600], "status_then": status_now,
        "status": "open", "created_at": _now(),
    }
    if offer:
        rec["offer"] = offer          # "name": Plexbie asks whether to search by name
    if opened_by:
        rec["opened_by"] = opened_by  # an admin opened it from Manage → All requests
    if quiet:
        rec["quiet"] = True           # ...without telling the person who asked
    await kv_set(NAMESPACE, hid, rec)
    logger.info(f"Help request {hid} on No. {slot} ({title}) from {rec['who']}: {reason}")
    return {**rec, "id": hid}


async def resolve(hid: str, actor: str, reply: str) -> Optional[dict]:
    rec = await kv_get(NAMESPACE, hid)
    if not isinstance(rec, dict):
        return None
    if rec.get("status") != "open":
        return {**rec, "id": hid, "already": True}
    rec.update(status="resolved", resolved_by=actor, resolved_at=_now(), reply=(reply or "")[:600] or None)
    await kv_set(NAMESPACE, hid, rec)
    return {**rec, "id": hid}


async def note_search(hid: str, actor: str, what: str) -> None:
    rec = await kv_get(NAMESPACE, hid)
    if isinstance(rec, dict):
        rec.setdefault("actions", []).append({"at": _now(), "by": actor, "did": what})
        await kv_set(NAMESPACE, hid, rec)


async def _series_for(services, rec: dict) -> dict:
    media = (rec or {}).get("media") or {}
    if not services.sonarr.configured:
        raise LookupError("Sonarr isn't set up")
    series = next((s for s in await services.sonarr.series() if s.get("tmdbId") == media.get("id")), None)
    if not series:
        raise LookupError("It isn't in Sonarr yet")
    return series


async def _seasons_for(services, rec: dict, series: dict) -> list:
    from core.season_search import seasons_with_missing
    seasons = rec.get("seasons")
    if isinstance(seasons, list) and seasons:
        return [int(n) for n in seasons]
    return await seasons_with_missing(services.sonarr, series["id"])


async def _movie_for(services, rec: dict) -> dict:
    from core.verified_search import find_movie
    if not services.radarr.configured:
        raise LookupError("Radarr isn't set up")
    movie = await find_movie(services.radarr, ((rec or {}).get("media") or {}).get("id"))
    if not movie:
        raise LookupError("It isn't in Radarr yet")
    return movie


async def search_again(services, request_key: str, on_nothing=None, on_movie=None) -> str:
    """Search again for this request, by its IDs. Returns what was started.

    Films: Radarr's search, keeping only releases that really are the film
    (core/verified_search); on_movie hears what happened, as a sentence and
    whether something was grabbed. Shows search season by season, falling back
    to single episodes when no whole-season release exists (core/season_search);
    on_nothing hears which seasons turned up nothing at all.
    """
    from core import season_search, verified_search
    rec = await get_request(int(request_key))
    media = (rec or {}).get("media") or {}
    if media.get("media_type") == "movie":
        movie = await _movie_for(services, rec)
        title = movie.get("title") or media.get("title") or "It"

        async def run():
            try:
                result = await verified_search.verify_movie(services.radarr, movie)
                text, found = verified_search.describe_by_id(title, result), result["state"] != "searched" or bool(result.get("grabbed"))
            except Exception as e:
                logger.warning(f"Searching for {title} by its IDs failed: {e}")
                text, found = f"Searching by its IDs failed: {e}", False
            if on_movie:
                await on_movie(text, found)
        verified_search.background(run())
        return "Radarr is searching for it again by its IDs. Plexbie checks what it finds really is this film."
    if media.get("media_type") == "tv":
        series = await _series_for(services, rec)
        seasons = await _seasons_for(services, rec, series)
        if not seasons:
            return "Sonarr already has every aired episode of what was asked for."
        season_search.start(services, series["id"], seasons, title=media.get("name") or "", on_nothing=on_nothing)
        return (f"Sonarr is searching season {', '.join(map(str, seasons))} again. If no whole-season "
                "release turns up, Plexbie tries it episode by episode.")
    raise LookupError("Books are searched by an admin in SABnzbd")


async def search_episodes(services, request_key: str, on_nothing=None) -> str:
    """Skip the season search: look for every missing episode on its own."""
    from core import season_search
    rec = await get_request(int(request_key))
    media = (rec or {}).get("media") or {}
    if media.get("media_type") != "tv":
        raise LookupError("That's for shows only")
    series = await _series_for(services, rec)
    seasons = await _seasons_for(services, rec, series)
    if not seasons:
        return "Sonarr already has every aired episode of what was asked for."
    season_search.start(services, series["id"], seasons, title=media.get("name") or "",
                        on_nothing=on_nothing, season_first=False)
    return f"Sonarr is searching season {', '.join(map(str, seasons))} one episode at a time."


async def search_by_name(services, request_key: str, on_done) -> str:
    """Search NZBHydra by the title's name instead of its IDs, in the background.
    Returns what was started; on_done hears what happened, as a sentence and
    whether something was grabbed."""
    from core import verified_search
    rec = await get_request(int(request_key))
    media = (rec or {}).get("media") or {}
    if not services.hydra.configured:
        raise LookupError("NZBHydra isn't set up, so Plexbie can't search by name")
    if media.get("media_type") == "movie":
        movie = await _movie_for(services, rec)
        title = movie.get("title") or "It"
        query = f'"{title} {movie.get("year")}"' if movie.get("year") else f'"{title}"'

        async def search():
            return await verified_search.movie_by_name(services.radarr, services.hydra, movie)
    elif media.get("media_type") == "tv":
        series = await _series_for(services, rec)
        seasons = await _seasons_for(services, rec, series)
        if not seasons:
            return "Sonarr already has every aired episode of what was asked for."
        title = series.get("title") or "It"
        query = " and ".join(f'"{title} S{n:02d}"' for n in seasons)

        async def search():
            return await verified_search.show_by_name(services.sonarr, services.hydra, series, seasons)
    else:
        raise LookupError("Books are searched by an admin in SABnzbd")

    async def run():
        try:
            result = await search()
            text, found = verified_search.describe_by_name(title, result), bool(result.get("grabbed"))
        except Exception as e:
            logger.warning(f"Searching for {title} by name failed: {e}")
            text, found = f"Searching by name failed: {e}", False
        await on_done(text, found)
    verified_search.background(run())
    return f"Plexbie is searching NZBHydra for {query}. It reports back here, and closes this if it finds it."


async def name_search_for_help(services, hid: str, actor: str, tell=None) -> str:
    """An admin said yes to searching by name: start it, note it on the help request,
    and when it's done note what happened there (and tell(text) the admins). Found
    and grabbed: the help request is resolved."""
    h = await kv_get(NAMESPACE, hid)
    if not isinstance(h, dict) or h.get("status") != "open":
        raise LookupError("That help request isn't open any more")

    async def done(text: str, found: bool) -> None:
        await note_search(hid, "Plexbie", text)
        if found:
            await resolve(hid, "Plexbie", text)
        if tell:
            try:
                await tell(f"{'✅' if found else '🔎'} **{h.get('title')}**: {text}")
            except Exception as e:
                logger.warning(f"Could not report the name search for {h.get('title')}: {e}")
    message = await search_by_name(services, h["request"], done)
    await note_search(hid, actor, message)
    return message
