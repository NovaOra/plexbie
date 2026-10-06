# path: core/blocked_imports.py
"""Downloads Sonarr or Radarr finished but won't import by themselves ("import blocked").

Sonarr and Radarr refuse to import a finished download when they aren't sure of it: a
release they matched only by ID (East of Eden came as "East.Of.Eden", the show is "East
of Eden (2026)"), episodes they can't place, a file that isn't what was grabbed. Most
are fine; some really are the wrong episode, the wrong film, or something that shouldn't
be in a video folder at all. So Plexbie never imports one on its own:

  - every few minutes it looks for them, and for each new one opens a ticket on its
    request (or, with no request, tells the admin channel) and alerts the admins;
  - the ticket shows what's in the download, with what looks off (preview);
  - an admin imports it from there (do_import), which goes through Sonarr's or Radarr's
    own Manual Import, so they move and name the files exactly as they would have;
  - when it's gone from the queue (imported, here or in Sonarr), its ticket is closed.
"""
import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.logging import get_logger
from database.kv_store import kv_delete_many, kv_get_all, kv_set_many

logger = get_logger(__name__)

NAMESPACE = "blocked_imports"
#: Sonarr's and Radarr's words for a finished download they won't import by themselves.
BLOCKED = ("importBlocked", "importFailed")
APPS = ("sonarr", "radarr")
#: What a download id looks like (SABnzbd's nzo ids, torrent hashes, client GUIDs).
DOWNLOAD_ID = re.compile(r"[\w.:-]{1,120}")
VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".webm", ".wmv", ".mpg", ".mpeg"}
#: Files that have no business in a video download: importing is refused while one's there.
DANGEROUS = {".exe", ".scr", ".bat", ".cmd", ".com", ".lnk", ".msi", ".js", ".jse", ".vbs", ".vbe", ".ps1",
             ".jar", ".apk", ".dll", ".hta", ".pif", ".reg", ".wsf"}
#: Smaller than this for a whole episode / film is worth a second look.
SMALL = {"sonarr": 100 * 2**20, "radarr": 400 * 2**20}


def _client(services, app: str):
    return services.sonarr if app == "sonarr" else services.radarr


def _ep(e: dict) -> str:
    return f"S{int(e.get('seasonNumber') or 0):02d}E{int(e.get('episodeNumber') or 0):02d}"


async def blocked(services) -> List[dict]:
    """Every blocked download in Sonarr and Radarr right now, one entry per download."""
    out: List[dict] = []
    for app in APPS:
        client = _client(services, app)
        if not client.configured:
            continue
        extra = {"includeSeries": "true", "includeEpisode": "true"} if app == "sonarr" else {"includeMovie": "true"}
        try:
            records = await client.queue(**extra)
        except Exception as e:
            logger.info(f"Blocked imports: couldn't read {client.name}'s queue: {e}")
            continue
        groups: Dict[str, List[dict]] = {}
        for r in records:
            if r.get("trackedDownloadState") in BLOCKED and r.get("downloadId"):
                groups.setdefault(str(r["downloadId"]), []).append(r)
        for did, recs in groups.items():
            first = recs[0]
            owner = first.get("series") or first.get("movie") or {}
            messages = []
            for r in recs:
                for m in r.get("statusMessages") or []:
                    for text in (m.get("messages") or []):
                        if text and text not in messages:
                            messages.append(text)
            out.append({
                "app": app, "downloadId": did,
                "title": owner.get("title") or first.get("title") or "A download",
                "year": owner.get("year"), "tmdbId": owner.get("tmdbId"), "tvdbId": owner.get("tvdbId"),
                "ownerId": first.get("seriesId") or first.get("movieId"),
                "release": first.get("title") or "", "folder": first.get("outputPath") or "",
                "state": first.get("trackedDownloadState"), "messages": messages[:6],
                "episodes": sorted({_ep(r.get("episode") or {}) for r in recs if r.get("episode")}),
            })
    return out


def _scan_folder(folder: str) -> Optional[List[dict]]:
    """Blocking: every file in the download folder, if Plexbie can see it (it usually can,
    with the same /data mount as Sonarr and Radarr). None when it can't."""
    if not folder or not os.path.isdir(folder):
        return None
    found = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            found.append({"name": os.path.relpath(path, folder), "size": size})
            if len(found) >= 300:
                return found
    return found


async def preview(services, app: str, download_id: str) -> dict:
    """What importing this download would do, and what looks off. `ok` is whether it can
    be imported from Plexbie at all; `files` keeps Sonarr's/Radarr's own entries for it."""
    from core.blocking import run_blocking
    item = next((b for b in await blocked(services) if b["app"] == app and b["downloadId"] == download_id), None)
    if not item:
        raise LookupError("That download isn't waiting to be imported any more.")
    client = _client(services, app)
    raw = await client.get("manualimport", folder=item["folder"], downloadId=download_id, filterExistingFiles="false") or []
    if not isinstance(raw, list):
        raise LookupError(f"{client.name} couldn't look inside the download.")
    files, problems, warnings, seen = [], [], [], {}
    for f in raw:
        name = f.get("relativePath") or os.path.basename(f.get("path") or "")
        size = int(f.get("size") or 0)
        where = ([_ep(e) for e in f.get("episodes") or []] if app == "sonarr"
                 else [(f.get("movie") or {}).get("title")] if f.get("movie") else [])
        notes = [r.get("reason") for r in f.get("rejections") or [] if r.get("reason")]
        ext = os.path.splitext(name)[1].lower()
        if ext and ext not in VIDEO:
            notes.append(f"{ext} isn't a usual video file")
        if "sample" in name.lower():
            notes.append("Looks like a sample, not the real thing")
        if size and size < SMALL[app]:
            notes.append(f"Very small for a whole {'episode' if app == 'sonarr' else 'film'}")
        if not where:
            notes.append(f"{client.name} can't tell which {'episode' if app == 'sonarr' else 'film'} this is")
        for w in where:
            seen.setdefault(w, []).append(name)
        files.append({"name": name, "size": size, "as": where, "quality": ((f.get("quality") or {}).get("quality") or {}).get("name"),
                      "notes": notes, "_raw": f})
        if f.get("rejections") or not where:
            problems.append(name)
    for w, names in seen.items():
        if len(names) > 1:
            warnings.append(f"{len(names)} files claim to be {w}")
    missing = [e for e in item["episodes"] if e not in seen]
    if missing:
        warnings.append(f"Grabbed for {', '.join(missing)}, but no file in it matches")
    others: List[dict] = []
    listing = await run_blocking(_scan_folder, item["folder"])
    if listing is None:
        warnings.append("Plexbie can't see the download folder itself, so only the video files are listed. "
                        "Check the folder for anything else before importing.")
    else:
        videos = {f["name"] for f in files}
        for x in listing:
            if x["name"] in videos:
                continue
            ext = os.path.splitext(x["name"])[1].lower()
            danger = ext in DANGEROUS
            others.append({"name": x["name"], "size": x["size"], "danger": danger})
            if danger:
                problems.append(x["name"])
                warnings.append(f"{x['name']} is a program, not a video. Don't import this: delete the download.")
    if not files:
        problems.append("no files")
        warnings.append(f"{client.name} found no video files it could import.")
    return {
        "app": app, "downloadId": download_id, "title": item["title"], "year": item["year"],
        "release": item["release"], "folder": item["folder"], "messages": item["messages"],
        "episodes": item["episodes"], "warnings": warnings, "others": others[:50],
        "files": files, "ok": not problems,
    }


def public(p: dict) -> dict:
    """A preview as the website and app see it (without Sonarr's raw entries)."""
    return {**p, "files": [{k: v for k, v in f.items() if k != "_raw"} for f in p["files"]]}


async def do_import(services, app: str, download_id: str) -> str:
    """Import it through Sonarr's/Radarr's Manual Import, as their own Activity page
    would. Refused when the preview finds anything it can't vouch for."""
    p = await preview(services, app, download_id)
    if not p["ok"]:
        raise ValueError("Not importing this one from Plexbie: " + (p["warnings"][-1] if p["warnings"] else
                         "a file in it can't be placed. Sort it out in " + ("Sonarr" if app == "sonarr" else "Radarr") + "."))
    client = _client(services, app)
    files = []
    for f in p["files"]:
        raw = f["_raw"]
        entry = {"path": raw["path"], "folderName": raw.get("folderName"), "quality": raw.get("quality"),
                 "languages": raw.get("languages") or [], "releaseGroup": raw.get("releaseGroup"),
                 "downloadId": raw.get("downloadId") or download_id, "indexerFlags": raw.get("indexerFlags", 0)}
        if app == "sonarr":
            entry.update(seriesId=(raw.get("series") or {}).get("id"), episodeIds=[e["id"] for e in raw.get("episodes") or []],
                         releaseType=raw.get("releaseType", "unknown"))
        else:
            entry.update(movieId=(raw.get("movie") or {}).get("id"))
        files.append(entry)
    command = await client.command("ManualImport", files=files, importMode="auto")
    cid = (command or {}).get("id")
    for _ in range(60):          # up to two minutes: a big file on another disk is copied
        await asyncio.sleep(2)
        state = await client.get(f"command/{cid}") if cid else {}
        status = (state or {}).get("status")
        if status == "completed":
            return (state.get("message") or f"Imported {len(files)} file{'s' if len(files) != 1 else ''}").rstrip(".") + "."
        if status in ("failed", "aborted", "cancelled"):
            raise RuntimeError(f"{client.name} couldn't import it: {state.get('message') or status}.")
    return f"{client.name} is still importing it; it'll be on Plex once that's done."


async def _request_for(item: dict) -> Optional[str]:
    """The newest approved request for this show or film, if there is one."""
    from database.request_store import all_requests
    want = "tv" if item["app"] == "sonarr" else "movie"
    best = None
    for key, rec in (await all_requests()).items():
        media = (rec or {}).get("media") or {}
        if rec.get("status") != "approved" or media.get("media_type") != want or not item.get("tmdbId"):
            continue
        if str(media.get("id")) == str(item["tmdbId"]) and (best is None or int(key) > int(best)):
            best = key
    return best


async def check(bot, services) -> None:
    """One pass: tell the admins about new blocked downloads, close the tickets of
    ones that are gone."""
    from core import notify, season_search
    from portal import help as helpdesk
    items = await blocked(services)
    known = await kv_get_all(NAMESPACE)
    now = datetime.now(timezone.utc).isoformat()
    current, changed = set(), {}
    for item in items:
        key = f"{item['app']}:{item['downloadId']}"
        current.add(key)
        if key in known:
            continue
        app_name = "Sonarr" if item["app"] == "sonarr" else "Radarr"
        why = item["messages"][0] if item["messages"] else "it wasn't sure of the files"
        note = (f"{item['title']} finished downloading, but {app_name} won't import it by itself: {why} "
                "Look at the files on this ticket before you import it: a blocked import can be the wrong "
                "episode, the wrong film, or something that shouldn't be there.")
        blocked_ref = {"app": item["app"], "downloadId": item["downloadId"]}
        req = await _request_for(item)
        h = None
        if req:
            h = await season_search.open_help(bot, req, seasons=None, reason="blocked", note=note,
                                              status_now="Downloaded, import blocked")
            if h:
                await helpdesk.add(h["id"], "action", "Plexbie", "", blocked=blocked_ref)
            else:
                # A ticket's already open on it: this goes on that one.
                open_one = next(iter((await helpdesk.open_for({str(req)})).values()), None)
                if open_one:
                    h = await helpdesk.add(open_one["id"], "action", "Plexbie", note, blocked=blocked_ref)
                    notify.alert_admins_soon(bot, services.config, title=f"{item['title']} won't import",
                                             body=f"{app_name} needs you to look at it. It's on the ticket.",
                                             url=f"/manage?tab=tickets&ticket={open_one['id']}", tag=f"blocked-{key}")
        if not h:
            channel_id = services.config.admin_channel_id
            channel = bot.get_channel(int(channel_id)) if channel_id else None
            if channel:
                try:
                    await channel.send(f"🧩 **{item['title']}**: {note} It's on Manage → Health.")
                except Exception as e:
                    logger.info(f"Couldn't tell the admin channel about {item['title']}: {e}")
            notify.alert_admins_soon(bot, services.config, title=f"{item['title']} won't import",
                                     body=f"{app_name} needs you to look at it first.", url="/manage?tab=health", tag=f"blocked-{key}")
        changed[key] = {"seen": now, "title": item["title"], "help": (h or {}).get("id")}
        logger.info(f"Blocked import: {item['title']} ({key}); admins told")
    gone = [k for k in known if k not in current]
    for k in gone:
        hid = (known[k] or {}).get("help") if isinstance(known[k], dict) else None
        if hid:
            await helpdesk.resolve(hid, "Plexbie", "It's been imported, so Plex will have it shortly.")
    if changed:
        await kv_set_many(NAMESPACE, changed)
    if gone:
        await kv_delete_many(NAMESPACE, gone)


async def ticket_for(app: str, download_id: str) -> Optional[str]:
    rec = (await kv_get_all(NAMESPACE)).get(f"{app}:{download_id}")
    return rec.get("help") if isinstance(rec, dict) else None
