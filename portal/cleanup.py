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
  removal       = last activity + inactivity_days (90 by default), and never
                  sooner than notify_days_before (7) after the bot first warned
                  about it (days_left; the "warned" record in the cleanup store);
  exempt titles and excluded libraries never count down, and nothing does while
  the watch history comes back empty across a big library (EMPTY_HISTORY_TITLES).
  A title is exempt by its Plex key or by the ids kept with its exemption
  (kept_forever), and a library is skipped by its name or by the section key
  recorded with that name (skipped_library).

Plex's lastViewedAt on an item is only the bot's own account (the owner's), so the
household's watching comes from the server's history instead (everyones_views).
Synchronous Plex reads; call compute() via run_blocking.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from utils.formatting import parse_utc
from utils.guids import guid_number

TRACKING_FILE = Path("config/media_tracking.json")

#: Plex's watch history coming back empty across more titles than this is taken as
#: history it couldn't read (a fresh or restored Plex database, an account that can't
#: see the others' plays), not as months of nobody watching anything.
EMPTY_HISTORY_TITLES = 50
#: The record of when titles were first warned counts only while the bot keeps
#: checking (it's dated each check). After a longer gap - Plexbie down, cleanup
#: switched off, Plex unreachable - every title due gets a whole new warning.
WARNINGS_LAPSE = timedelta(days=3)


def _ts(value) -> Optional[datetime]:
    try:
        v = int(value)
        return datetime.fromtimestamp(v, timezone.utc) if v else None
    except (TypeError, ValueError):
        return None


def request_times() -> List[dict]:
    """Request timestamps from media tracking, the cleanup task's grace source."""
    try:
        data = json.loads(TRACKING_FILE.read_text())
    except (OSError, ValueError):
        return []
    out = []
    for rec in data.values():
        ts = parse_utc(rec.get("request_timestamp"))
        if ts:
            out.append({"tmdb": rec.get("tmdb_id"), "type": rec.get("media_type"), "title": rec.get("title"), "at": ts})
    return out


def _tmdb(el) -> Optional[int]:
    for g in el.findall("Guid"):
        tmdb = guid_number(g.get("id"), "tmdb")
        if tmdb is not None:
            return tmdb
    return None


def guid_ids(el) -> Dict[str, str]:
    """A listed film's or show's TMDB / TheTVDB / IMDb ids, from its Guid tags
    ("tmdb://1396"), read as the cleanup task reads them off plexapi's guids."""
    out: Dict[str, str] = {}
    for g in el.findall("Guid"):
        gid = g.get("id") or ""
        if "://" in gid:
            k, v = gid.split("://", 1)
            if k in ("tmdb", "tvdb", "imdb") and v:
                out.setdefault(k, v)
    return out


def kept_forever(exempt: dict, rating_key, kind: str, ids: Dict[str, str]) -> bool:
    """Whether a film or show (`kind` "movie" or "show") is on the keep-forever list:
    by its Plex key, or by an id stored with a kept title of the same type. So one Plex
    lists again under a new key (removed and added back, a rebuilt library) stays kept;
    a film's TMDB number and a show's are different titles."""
    if str(rating_key) in exempt:
        return True
    for kept in exempt.values():
        stored = kept.get("ids") if isinstance(kept, dict) and kept.get("type") == kind else None
        if isinstance(stored, dict) and any(v and str(stored.get(k) or "") == str(v) for k, v in ids.items()):
            return True
    return False


def _recorded_keys(config: dict) -> Dict[str, str]:
    keys = config.get("exclude_library_keys")
    return keys if isinstance(keys, dict) else {}


def skipped_library(config: dict, title, key) -> bool:
    """Whether cleanup skips the Plex library `title` (section `key`): by the name an
    admin picked, or by the section key recorded with that name, so a library renamed
    since is still skipped."""
    names = config.get("exclude_libraries") or []
    keys = _recorded_keys(config)
    return title in names or (key is not None and str(key) in {str(keys[n]) for n in names if keys.get(n)})


def missing_skipped(config: dict, libraries) -> List[str]:
    """The skipped libraries Plex has neither by name nor by recorded key, from its
    libraries as (title, section key) pairs: renamed before its key was recorded, or
    removed. Cleanup can't tell which, so it removes nothing while there are any."""
    keys = _recorded_keys(config)
    titles = {title for title, _ in libraries}
    present = {str(key) for _, key in libraries}
    return [n for n in config.get("exclude_libraries") or []
            if n not in titles and str(keys.get(n) or "") not in present]


def skipped_names(config: dict, libraries: Dict[str, str]) -> List[str]:
    """The skipped libraries by the names Plex gives them now (`libraries` is
    {title: section key}): a renamed one under its new name, one Plex no longer has
    under the name it was skipped by. A library since given a renamed one's old name
    is skipped by that name too, so both are listed."""
    keys = _recorded_keys(config)
    titles = {str(key): title for title, key in libraries.items()}
    out: List[str] = []
    for name in config.get("exclude_libraries") or []:
        renamed = titles.get(str(keys.get(name) or ""))
        now = [n for n in (name if name in libraries else None, renamed) if n] or [name]
        for n in now:
            if n not in out:
                out.append(n)
    return out


def library_keys(server) -> Dict[str, str]:
    """Blocking: {title: section key} of every Plex library."""
    return {s.title: str(s.key) for s in server.library.sections()}


def aware(when: datetime) -> datetime:
    """plexapi's datetimes are the server's local time, without a zone."""
    return when.astimezone(timezone.utc) if when.tzinfo is None else when


def days_left(inactive: int, config: dict, warned: Optional[str], now: datetime) -> int:
    """Days until media_cleanup removes a title idle for `inactive` days; 0 when it's due.

    Not before inactivity_days, and not before notify_days_before have passed since it
    was first warned about (`warned`, an ISO time; None while it hasn't been), so a
    title never goes without the whole warning first, whatever brought it past the
    threshold: lower inactivity days, a library added or no longer skipped, downtime."""
    first = parse_utc(warned)
    since = (now - first).days if first else 0
    return max(0, int(config.get("inactivity_days", 90)) - inactive,
               int(config.get("notify_days_before", 7)) - since)


def current_warnings(warned, checked, now: datetime) -> Dict[str, str]:
    """The "warned" record as it stands: {} when it isn't one, or when the check that
    last dated it (`checked`, an ISO time) is missing or more than WARNINGS_LAPSE ago."""
    last = parse_utc(checked)
    if not isinstance(warned, dict) or not last or now - last > WARNINGS_LAPSE:
        return {}
    return warned


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


def compute(server, config: dict, requests: List[dict],
            warned: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    """{ratingKey: {...}} for every film and show the cleanup task would consider.
    `warned` is the bot's record of when it first warned about each title."""
    now = datetime.now(timezone.utc)
    days = int(config.get("inactivity_days", 90))
    views = everyones_views(server, days)
    notify = days - int(config.get("notify_days_before", 7))
    exempt = config.get("exempt_items", {}) or {}
    warned = warned or {}
    out: Dict[str, dict] = {}
    judged = 0

    for sec in server.query("/library/sections").findall("Directory"):
        stype = sec.get("type")
        if stype not in ("movie", "show") or skipped_library(config, sec.get("title"), sec.get("key")):
            continue
        root = server.query(f"/library/sections/{sec.get('key')}/all?includeGuids=1")
        for el in root.findall("Video" if stype == "movie" else "Directory"):
            judged += 1
            rk = el.get("ratingKey")
            tmdb = _tmdb(el)
            year = el.get("year") or ""
            entry = {"ratingKey": rk, "tmdb": tmdb, "ids": guid_ids(el), "title": el.get("title"), "type": stype,
                     "year": int(year) if year.isdigit() else None}
            if kept_forever(exempt, rk, stype, entry["ids"]):
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
                "daysLeft": days_left(inactive, config, warned.get(rk), now),
                "warning": inactive >= notify,
            }
    if not views and judged > EMPTY_HISTORY_TITLES:
        return {}   # the bot takes this history as unreadable and judges nothing
    return out
