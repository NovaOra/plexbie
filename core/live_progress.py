# path: core/live_progress.py
"""Live progress in the Plexbie app (Android): while a request downloads, its requester's
phone keeps one notification for it, with a progress bar that updates itself. On
Android 16 it's also a Live Update (a chip in the status bar). The app draws it; this
sends the updates, as silent data-only pushes, to phones that turned it on.

Only what's actually moving is shown: a request appears once it's downloading (never
while it's waiting for a release or a copy), and goes when it's on Plex. So nothing
holds the notification bar:
  - a download that hasn't moved for STALL is taken down (and comes back if it moves);
  - each update tells the phone to drop it by itself after the app's timeout unless
    another update comes, so a bot that goes quiet can't leave one behind.
Each update carries "ts", when it was sent (milliseconds since the epoch): a phone that
was offline can get them late and out of order, and the app can ignore one older than
the last it applied, so a late "show" can't bring back one that has ended.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from core.logging import get_logger
from database.kv_store import kv_delete_many, kv_get_all, kv_set_many
from utils.formatting import parse_utc

logger = get_logger(__name__)

NAMESPACE = "live_progress"
#: The stages a live notification is up for.
SHOWN = ("downloading", "unpacking", "importing")
#: Stages that end a request's live notification for good.
FINISHED = ("available", "declined", "closed")
#: An update at least this often while it's up, so the phone's own timeout (the app's
#: LIVE_TIMEOUT, 30 minutes) never takes down one that's still going.
KEEPALIVE = timedelta(minutes=10)
#: A smaller move than this (percent) waits for the keepalive.
STEP = 5
#: Not moved for this long: down, until it moves again.
STALL = timedelta(hours=2)
#: Requests older than this aren't followed.
WATCH_DAYS = 60
#: How long the bookshelf's titles are kept before the folders are scanned again.
SHELF_TTL = 900


def _owner(rec: dict) -> Tuple[Optional[str], Optional[str]]:
    uid = rec.get("user_id")
    return (str(uid) if uid else None, str(rec["plex_account_id"]) if rec.get("plex_account_id") else None)


def _title(rec: dict) -> str:
    media = rec.get("media") or {}
    title = media.get("title") or media.get("name") or "Your request"
    seasons = rec.get("seasons")
    if isinstance(seasons, list) and len(seasons) == 1:
        return f"{title} · Season {seasons[0]}"
    return title


def _same_thing(rec: dict) -> tuple:
    """What a request is for: the same title and seasons is the same notification."""
    media = rec.get("media") or {}
    seasons = rec.get("seasons")
    return (media.get("media_type") or rec.get("media_type"), str(media.get("id") or media.get("open_library_key") or _title(rec)),
            tuple(sorted(seasons)) if isinstance(seasons, list) else str(seasons))


def _text(live: dict) -> str:
    stage = live.get("stage")
    if stage == "importing":
        return "Downloaded. Adding it to Plex"
    if stage == "unpacking":
        return (live.get("detail") or "Unpacking").replace(" in SABnzbd", "")
    pct = live.get("percent")
    lead = f"Downloading, {pct}%" if isinstance(pct, int) else "Downloading"
    return f"{lead}. {live['detail']}" if live.get("detail") else lead


async def _live_apps() -> Dict[str, List[tuple]]:
    """Phones with live progress on, by owner ("d<discord id>" / "p<plex account>")."""
    from core import notify
    out: Dict[str, List[tuple]] = {}
    for k, r in (await kv_get_all(notify.APP_NAMESPACE)).items():
        if not (isinstance(r, dict) and r.get("token") and r.get("live") and r.get("platform") == "android"):
            continue
        if r.get("discord_id"):
            out.setdefault(f"d{r['discord_id']}", []).append((k, r))
        if r.get("plex_account_id"):
            out.setdefault(f"p{r['plex_account_id']}", []).append((k, r))
    return out


async def _shelf_titles(config, cache) -> List[str]:
    """The bookshelf's titles, kept in progress's cache (a failed rescan keeps the last)."""
    from portal import books as shelf
    from core.blocking import run_blocking

    async def load() -> List[str]:
        found = await run_blocking(shelf.scan, config.bookshelf_audiobook_library, config.bookshelf_ebook_library)
        return shelf.shelf_titles(found)
    return await cache.get("shelf:titles", SHELF_TTL, load)


async def tick(services, progress, *, now: Optional[datetime] = None) -> int:
    """One pass: send what changed. Returns how many updates went out."""
    from core import notify
    from database.request_store import all_requests
    if not notify.app_push_on():
        return 0
    now = now or datetime.now(timezone.utc)
    ts = int(now.timestamp() * 1000)
    phones = await _live_apps()
    states = await kv_get_all(NAMESPACE)
    if not phones and not states:
        return 0
    sent, gone, keep = 0, [], {}

    def phones_of(owners) -> List[tuple]:
        seen, out = set(), []
        for o in owners or []:
            for k, r in phones.get(o) or []:
                if k not in seen:
                    seen.add(k)
                    out.append((k, r))
        return out
    # Numbered as everywhere else (No. 0214): by the request's key, oldest first.
    ordered = sorted((await all_requests()).items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)
    records = {key: {**rec, "_slot": n + 1} for n, (key, rec) in enumerate(ordered)}
    # Two requests for the same thing (asked twice, or once here and once in Seerr,
    # which the bot records too) are one notification on a phone: the newest request's.
    newest: Dict[tuple, str] = {}
    for key, rec in records.items():
        if (rec or {}).get("status") != "approved":
            continue
        did, pid = _owner(rec)
        group = (tuple(sorted(k for k, _ in phones_of([o for o in (f"d{did}" if did else None, f"p{pid}" if pid else None) if o]))),
                 _same_thing(rec))
        if group[0] and (group not in newest or str(rec.get("timestamp") or "") > str(records[newest[group]].get("timestamp") or "")):
            newest[group] = key
    shown_for = set(newest.values())
    for key in [k for k in states if k not in records]:
        # Deleted: take its notification down on the phones it was on.
        state = states[key] if isinstance(states[key], dict) else {}
        if state.get("shown") and phones_of(state.get("owners")):
            sent += await notify.push_app_live(phones_of(state.get("owners")), {"op": "end", "id": key, "ts": ts})
        gone.append(key)
    for key, rec in records.items():
        state = states.get(key) if isinstance(states.get(key), dict) else None
        did, pid = _owner(rec or {})
        owners = [o for o in (f"d{did}" if did else None, f"p{pid}" if pid else None) if o]
        apps = phones_of(owners)
        if rec.get("status") != "approved" or (apps and key not in shown_for):
            if state:
                if state.get("shown") and apps:
                    sent += await notify.push_app_live(apps, {"op": "end", "id": key, "ts": ts})
                gone.append(key)
            continue
        if not apps and not state:
            continue
        asked = parse_utc(rec.get("timestamp"))
        if asked and now - asked > timedelta(days=WATCH_DAYS):
            continue
        media = rec.get("media") or {}
        try:
            if rec.get("media_type") in ("ebook", "audiobook", "both") or "open_library_key" in media:
                live = await progress.book(media, await _shelf_titles(services.config, progress.cache))
            else:
                live = await progress.video(media, rec.get("seasons"))
        except Exception as e:
            logger.info(f"Live progress: couldn't check {_title(rec)}: {e}")
            continue
        stage, pct = live.get("stage"), live.get("percent")
        if stage in FINISHED:
            if state and state.get("shown") and apps:
                sent += await notify.push_app_live(apps, {"op": "end", "id": key, "ts": ts})
            if state:
                gone.append(key)
            continue
        if stage not in SHOWN and not state:
            continue
        state = dict(state or {}, owners=owners)
        moved = stage != state.get("stage") or pct != state.get("percent")
        if moved:
            state.update(stage=stage, percent=pct, moved=now.isoformat())
        stalled = now - (parse_utc(state.get("moved")) or now) > STALL
        if stage in SHOWN and not stalled and apps:
            last = parse_utc(state.get("sent"))
            due = (not state.get("shown") or stage != state.get("sentStage")
                   or (isinstance(pct, int) and abs(pct - (state.get("sentPercent") or 0)) >= STEP)
                   or not last or now - last >= KEEPALIVE)
            if due:
                sent += await notify.push_app_live(apps, {
                    "op": "show", "id": key, "slot": rec.get("_slot"), "title": _title(rec), "text": _text(live),
                    "stage": stage, "percent": pct if isinstance(pct, int) else None, "ts": ts,
                })
                state.update(shown=True, sent=now.isoformat(), sentStage=stage, sentPercent=pct)
        elif state.get("shown"):
            # Stalled, gone back to searching, or nobody's phone wants it any more.
            if apps:
                sent += await notify.push_app_live(apps, {"op": "end", "id": key, "ts": ts})
            state["shown"] = False
        keep[key] = state
    if keep:
        await kv_set_many(NAMESPACE, keep)
    if gone:
        await kv_delete_many(NAMESPACE, gone)
    return sent
