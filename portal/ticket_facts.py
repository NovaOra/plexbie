# path: portal/ticket_facts.py
"""What Plexbie can see for a ticket that isn't on a request ("Plexbie sees").

"The picture looks wrong" or "it won't play" is quicker to answer with the file and
the play in front of you: what Radarr or Sonarr have on disk (quality, HDR or Dolby
Vision, size), and how the member last played it, from Tautulli (on what, played
directly or converted, in what picture). On a general ticket, their last play of
anything. All best-effort: a service that isn't set up, can't be reached or doesn't
answer in time leaves its line out.
"""
import asyncio
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, List, Optional

from core.logging import get_logger

logger = get_logger(__name__)

#: The whole look holds up opening the ticket, so it gets this long, then goes without.
LOOK_SECONDS = 8
CALL_SECONDS = 5

_QUALITY = re.compile(r"^(?P<source>[A-Za-z-]+)-(?P<res>\d{3,4}p)(?P<remux> Remux)?$")
_SOURCES = {"webdl": "WEB-DL", "webrip": "WEBRip", "bluray": "Blu-ray", "remux": "Remux", "hdtv": "HDTV", "dvd": "DVD"}
_RANGES = {"dv": "Dolby Vision", "hdr10": "HDR10", "hdr10plus": "HDR10+", "hlg": "HLG", "pq": "PQ", "sdr": "SDR"}
#: Tautulli's transcode_decision, in plain words.
_DECISIONS = {"direct play": "direct play", "copy": "direct stream", "transcode": "converted"}


def quality_name(name: Any) -> str:
    """Radarr's or Sonarr's "WEBDL-2160p" as "4K WEB-DL" ("Bluray-1080p Remux" as
    "1080p Blu-ray Remux"); anything else as they put it."""
    m = _QUALITY.match(str(name or ""))
    if not m or m["source"].lower() not in _SOURCES:
        return str(name or "Unknown quality")
    res = "4K" if m["res"] == "2160p" else m["res"]
    return f"{res} {_SOURCES[m['source'].lower()]}{' Remux' if m['remux'] else ''}"


def dynamic_range(media_info: Any) -> str:
    """Radarr's or Sonarr's "DV HDR10" as "Dolby Vision + HDR10"; none (an SDR file) as "SDR"."""
    info = media_info if isinstance(media_info, dict) else {}
    kind = str(info.get("videoDynamicRangeType") or "").split()
    if not kind:
        return "HDR" if info.get("videoDynamicRange") == "HDR" else "SDR"
    return " + ".join(_RANGES.get(part.lower(), part) for part in kind)


def size_text(size: Any) -> Optional[str]:
    try:
        n = float(size)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _file_text(f: dict) -> str:
    parts = [quality_name(((f.get("quality") or {}).get("quality") or {}).get("name")), dynamic_range(f.get("mediaInfo")),
             size_text(f.get("size"))]
    return ", ".join(p for p in parts if p)


async def _movie_file(services, progress, tmdb: int) -> Optional[str]:
    movie = (await progress._radarr_movies()).get(int(tmdb))
    if not movie:
        return None
    if not movie.get("hasFile"):
        return "File: Radarr has none for it"
    found = movie.get("movieFile") or next(iter(await services.radarr.get("moviefile", movieId=movie["id"]) or []), None)
    return f"File: {_file_text(found)}" if isinstance(found, dict) else None


def _mix(counts: Counter, total: int) -> str:
    """One kind across every file ("1080p WEB-DL"), else the most common and how many."""
    if len(counts) == 1:
        return next(iter(counts))
    (top, n), = counts.most_common(1)
    return f"mostly {top} ({n} of {total})"


async def _show_files(services, progress, tmdb: int) -> Optional[str]:
    series = (await progress._sonarr_series()).get(int(tmdb))
    if not series:
        return None
    files = [f for f in await services.sonarr.get("episodefile", seriesId=series["id"]) or [] if isinstance(f, dict)]
    if not files:
        return "Files: Sonarr has none for it"
    qualities = Counter(quality_name(((f.get("quality") or {}).get("quality") or {}).get("name")) for f in files)
    ranges = Counter(dynamic_range(f.get("mediaInfo")) for f in files)
    if len(ranges) == 1:
        picture = next(iter(ranges))
    else:
        picture = " and ".join(f"{name} on {n}" for name, n in ranges.most_common())
    parts = [f"{len(files)} episode{'' if len(files) == 1 else 's'}", _mix(qualities, len(files)), picture,
             size_text(sum(f.get("size") or 0 for f in files))]
    return "Files: " + ", ".join(p for p in parts if p)


async def _tautulli_user(user: dict) -> Optional[dict]:
    """Who the member is in Tautulli: their Plex account id (a Plex sign-in, or the
    Plex account linked to their Discord), else their Plex username."""
    pid = str(user.get("plexAccountId") or "")
    if pid.isdigit():
        return {"user_id": int(pid)}
    did = str(user.get("discordId") or "")
    if did.isdigit():
        from database.people import person_by_discord
        from database.session import get_session
        async with get_session() as session:
            row = await person_by_discord(session, int(did))
        if row and row.plex_user_id:
            return {"user_id": int(row.plex_user_id)}
        if row and row.plex_username:
            return {"user": row.plex_username}
    return {"user": user["plexName"]} if user.get("plexName") else None


def _picture(resolution: Any, dynamic: Any) -> str:
    res = str(resolution or "")
    res = {"4k": "4K", "sd": "SD"}.get(res.lower(), res)
    return " ".join(x for x in (res, str(dynamic or "")) if x)


def play_text(row: dict, stream: dict, title: Optional[str] = None) -> str:
    """One play from Tautulli's history (and its stream data), as admins read it:
    "Last played Oct 9 on SHIELD (Plex for Android (TV)), direct play, 4K HDR"."""
    when = row.get("date") or row.get("started")
    day = ""
    try:
        # The household's own day (the host's zone): a late-evening play isn't "tomorrow".
        at = datetime.fromtimestamp(int(when), timezone.utc).astimezone()
        day = f" {at:%b} {at.day}"
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    what = ""
    if title:
        what = f" {title}"
    elif row.get("media_type") == "episode" and str(row.get("parent_media_index") or "").isdigit() \
            and str(row.get("media_index") or "").isdigit():
        what = f" S{int(row['parent_media_index']):02d}E{int(row['media_index']):02d}"
    device, app = row.get("player") or row.get("platform") or "", row.get("product") or ""
    on = f" on {device}" + (f" ({app})" if app and app != device else "") if device else ""
    decision = str(row.get("transcode_decision") or stream.get("transcode_decision") or "").lower()
    source = _picture(stream.get("video_full_resolution"), stream.get("video_dynamic_range"))
    shown = _picture(stream.get("stream_video_full_resolution"), stream.get("stream_video_dynamic_range"))
    if decision == "transcode" and stream.get("video_decision") == "transcode" and shown and source and shown != source:
        how = f"converted from {source} to {shown}"
    else:
        how = ", ".join(x for x in (_DECISIONS.get(decision, decision), source) if x)
    return f"Last played{what}{day}{on}" + (f", {how}" if how else "")


async def _last_play(services, user: dict, kind: Optional[str] = None, rating_key: Any = None) -> Optional[str]:
    """The member's last play of this title (a film's ratingKey, or a show's), or of
    anything when there's no title."""
    tautulli = services.tautulli
    if not tautulli.configured:
        return None
    who = await _tautulli_user(user)
    if not who:
        return None
    params: dict = {"length": 1, **who}
    if rating_key:
        params["rating_key" if kind == "movie" else "grandparent_rating_key"] = rating_key
    rows = ((await tautulli.call("get_history", timeout=CALL_SECONDS, **params)) or {}).get("data") or []
    if not rows:
        return "Tautulli has no play of it by them" if rating_key else "Tautulli has no plays by them"
    row = rows[0]
    stream: dict = {}
    row_id = row.get("row_id") or row.get("id")
    if row_id:
        try:
            stream = await tautulli.call("get_stream_data", timeout=CALL_SECONDS, row_id=row_id) or {}
        except Exception as e:
            logger.info(f"Tautulli had no stream details for a ticket's last play: {type(e).__name__}")
    return play_text(row, stream if isinstance(stream, dict) else {}, None if rating_key else row.get("full_title"))


async def _gather(jobs: list) -> List[str]:
    """Each line that came back in time, in order; a failed one is left out."""
    tasks = [asyncio.ensure_future(job) for job in jobs]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=LOOK_SECONDS)
    for task in pending:
        task.cancel()
    out = []
    for task in tasks:
        if task not in done or task.cancelled():
            continue
        if task.exception() is not None:
            logger.info(f"Plexbie sees: a line was left out ({type(task.exception()).__name__}: {task.exception()})")
        elif task.result():
            out.append(task.result())
    return out


async def title_facts(services, progress, user: dict, kind: str, tmdb: Any, rating_key: Any) -> List[str]:
    """The file on disk and the member's last play of a film or show."""
    jobs = []
    if str(tmdb or "").isdigit():
        jobs.append((_movie_file if kind == "movie" else _show_files)(services, progress, int(tmdb)))
    if rating_key:
        jobs.append(_last_play(services, user, kind, rating_key))
    return await _gather(jobs)


async def general_facts(services, user: dict) -> List[str]:
    """The member's last play of anything."""
    return await _gather([_last_play(services, user)])
