# path: portal/cleanup.py
"""How long each title has before media_cleanup removes it.

This mirrors MediaCleanupCog.check_item_for_cleanup step for step, so the
countdown on the site is the bot's own schedule, not an estimate:

  last activity = latest of
      when it was last watched (for a show, its most recently watched episode),
      when it was added (only if never watched),
      the newest request for it in media tracking;
  removal       = last activity + inactivity_days (90 by default);
  exempt titles and excluded libraries never count down.

Plex reports lastViewedAt for the account whose token the bot uses, exactly as
the cleanup task sees it. Synchronous Plex reads; call compute() via run_blocking.
"""
import json
from datetime import datetime, timezone
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


def compute(server, config: dict, requests: List[dict]) -> Dict[str, dict]:
    """{ratingKey: {...}} for every film and show the cleanup task would consider."""
    now = datetime.now(timezone.utc)
    days = int(config.get("inactivity_days", 90))
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
            if stype == "movie":
                last = _ts(el.get("lastViewedAt"))
            else:
                last = None
                for ep in server.query(f"/library/metadata/{rk}/allLeaves").findall("Video"):
                    seen = _ts(ep.get("lastViewedAt"))
                    if seen and (last is None or seen > last):
                        last = seen
            reason = "watched"
            if not last:
                last, reason = _ts(el.get("addedAt")), "added"
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
