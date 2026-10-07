# path: portal/cleanup.py
"""How long each title has before media_cleanup removes it.

This mirrors MediaCleanupCog.check_item_for_cleanup step for step, so the
countdown on the site is the bot's own schedule, not an estimate. One exception: a
title in Plex more than once is counted down per copy here, while the bot keeps
every copy as long as any one of them isn't due (see _judge_copies_together):

  last activity = latest of
      when anyone last watched it: every account's plays, from the server's own
      history (for a show, any episode: the whole show is one thing),
      when it was added (for a show, its newest episode, so a new season is fresh),
      the newest request for it in media tracking;
  removal       = last activity + inactivity_days (90 by default);
  exempt titles and excluded libraries never count down.

Plex's lastViewedAt on an item is only the bot's own account (the owner's), so the
household's watching comes from the server's history instead (everyones_views).
Synchronous Plex reads; call compute() via run_blocking.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from utils.guids import guid_number

TRACKING_FILE = Path("config/media_tracking.json")


def _ts(value) -> Optional[datetime]:
    try:
        v = int(value)
        return datetime.fromtimestamp(v, timezone.utc) if v else None
    except (TypeError, ValueError):
        return None


def _iso(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def request_times() -> List[dict]:
    """Request timestamps from media tracking, the cleanup task's grace source."""
    try:
        data = json.loads(TRACKING_FILE.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for rec in data.values():
        ts = _iso(rec.get("request_timestamp"))
        if ts:
            out.append({"tmdb": rec.get("tmdb_id"), "type": rec.get("media_type"), "title": rec.get("title"), "at": ts})
    return out


def _tmdb(el) -> Optional[int]:
    for g in el.findall("Guid"):
        tmdb = guid_number(g.get("id"), "tmdb")
        if tmdb is not None:
            return tmdb
    return None


def aware(when: datetime) -> datetime:
    """plexapi's datetimes are the server's local time, without a zone."""
    return when.astimezone(timezone.utc) if when.tzinfo is None else when


def everyones_views(server, days: int) -> Dict[str, datetime]:
    """The last time anyone (every account, managed users too) watched each film, and
    any episode of each show, by Plex key, over the last `days` (+2). Raises if Plex's
    history can't be read: then nothing should be judged unwatched."""
    since = datetime.now() - timedelta(days=int(days) + 2)
    latest: Dict[str, datetime] = {}
    for h in server.history(mindate=since):
        key = str(getattr(h, "grandparentRatingKey", None) or getattr(h, "ratingKey", None) or "")
        when = getattr(h, "viewedAt", None)
        if not key or not when:
            continue
        when = aware(when)
        if key not in latest or when > latest[key]:
            latest[key] = when
    return latest


def compute(server, config: dict, requests: List[dict]) -> Dict[str, dict]:
    """{ratingKey: {...}} for every film and show the cleanup task would consider."""
    now = datetime.now(timezone.utc)
    days = int(config.get("inactivity_days", 90))
    views = everyones_views(server, days)
    notify = days - int(config.get("notify_days_before", 7))
    exempt = config.get("exempt_items", {}) or {}
    excluded = set(config.get("exclude_libraries", []) or [])
    out: Dict[str, dict] = {}

    for sec in server.query("/library/sections").findall("Directory"):
        stype = sec.get("type")
        if stype not in ("movie", "show") or sec.get("title") in excluded:
            continue
        root = server.query(f"/library/sections/{sec.get('key')}/all?includeGuids=1")
        for el in root.findall("Video" if stype == "movie" else "Directory"):
            rk = el.get("ratingKey")
            tmdb = _tmdb(el)
            entry = {"ratingKey": rk, "tmdb": tmdb, "title": el.get("title"), "type": stype}
            if rk in exempt:
                out[rk] = {**entry, "exempt": True}
                continue
            last, newest = views.get(rk), None
            if stype == "movie":
                own = _ts(el.get("lastViewedAt"))
                if own and (last is None or own > last):
                    last = own
            else:
                for ep in server.query(f"/library/metadata/{rk}/allLeaves").findall("Video"):
                    seen = _ts(ep.get("lastViewedAt"))
                    if seen and (last is None or seen > last):
                        last = seen
                    came = _ts(ep.get("addedAt"))
                    if came and (newest is None or came > newest):
                        newest = came
            reason = "watched"
            added = newest or _ts(el.get("addedAt"))
            if added and (not last or added > last):
                last, reason = added, "added"
            kind = "tv" if stype == "show" else "movie"
            asked = [r["at"] for r in requests
                     if (tmdb is not None and r["tmdb"] == tmdb)
                     or (tmdb is None and r["title"] == el.get("title") and r["type"] == kind)]
            if asked and (not last or max(asked) > last):
                last, reason = max(asked), "requested"
            if not last:
                continue
            inactive = (now - last).days
            out[rk] = {
                **entry,
                "exempt": False,
                "lastActivity": last.isoformat(),
                "reason": reason,
                "daysLeft": max(0, days - inactive),
                "warning": inactive >= notify,
            }
    return out
