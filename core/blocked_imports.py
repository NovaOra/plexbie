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


async def blocked(services, unreachable: Optional[Dict[str, str]] = None) -> List[dict]:
    """Every blocked download in Sonarr and Radarr right now, one entry per download.
    `unreachable`, when given, gets each app whose queue couldn't be read and why, so an
    empty list because Sonarr is down doesn't pass for nothing to look at."""
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
            if unreachable is not None:
                unreachable[app] = f"Couldn't reach {client.name} just now."
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


def _episode(e: dict) -> dict:
    return {"id": e.get("id"), "label": _ep(e), "season": int(e.get("seasonNumber") or 0),
            "episode": int(e.get("episodeNumber") or 0), "title": e.get("title") or "", "hasFile": bool(e.get("hasFile"))}


async def _options(client, app: str) -> dict:
    """Qualities and languages to choose from, as Sonarr's/Radarr's own Manual Import offers."""
    out = {"qualities": [], "languages": []}
    try:
        out["qualities"] = [{"id": (d.get("quality") or {}).get("id"), "name": (d.get("quality") or {}).get("name") or d.get("title")}
                            for d in await client.get("qualitydefinition") or [] if (d.get("quality") or {}).get("id") is not None]
    except Exception as e:
        logger.info(f"Blocked imports: no quality list from {client.name}: {e}")
    try:
        out["languages"] = [{"id": x.get("id"), "name": x.get("name")} for x in await client.get("language") or [] if x.get("id") is not None]
    except Exception as e:
        logger.info(f"Blocked imports: no language list from {client.name}: {e}")
    return out


async def episodes_of(services, series_id: int) -> List[dict]:
    """Every episode of a show in Sonarr, to say which one a file is."""
    return sorted((_episode(e) for e in await services.sonarr.episodes(int(series_id))),
                  key=lambda e: (e["season"] or 999, e["episode"]))


async def library(services, app: str, q: str) -> List[dict]:
    """Shows (Sonarr) or films (Radarr) in the library whose title has `q`, for "Wrong show?"."""
    client = _client(services, app)
    rows = await (client.series() if app == "sonarr" else client.movies())
    words = [w for w in (q or "").casefold().split() if w]
    hits = [{"id": r.get("id"), "title": r.get("title") or "", "year": r.get("year")} for r in rows
            if words and all(w in (r.get("title") or "").casefold() for w in words)]
    return sorted(hits, key=lambda r: (len(r["title"]), r["title"]))[:20]


async def preview(services, app: str, download_id: str) -> dict:
    """What importing this download would do, and what looks off: for each file what
    Sonarr/Radarr think it is (and every reason they gave for not importing it), and the
    choices an admin can change, as their own Manual Import screen offers. `ok` is
    whether it can be imported from Plexbie at all (never with a program in it); each
    file's `ready` is whether it's placed (an episode, or a film)."""
    from core.blocking import run_blocking
    item = next((b for b in await blocked(services) if b["app"] == app and b["downloadId"] == download_id), None)
    if not item:
        raise LookupError("That download isn't waiting to be imported any more.")
    client = _client(services, app)
    raw = await client.get("manualimport", folder=item["folder"], downloadId=download_id, filterExistingFiles="false") or []
    if not isinstance(raw, list):
        raise LookupError(f"{client.name} couldn't look inside the download.")
    thing = "episode" if app == "sonarr" else "film"
    files, danger, warnings, seen = [], [], [], {}
    for f in raw:
        name = f.get("relativePath") or os.path.basename(f.get("path") or "")
        size = int(f.get("size") or 0)
        eps = [_episode(e) for e in f.get("episodes") or []] if app == "sonarr" else []
        movie = f.get("movie") or None
        where = [e["label"] for e in eps] if app == "sonarr" else ([movie.get("title")] if movie else [])
        rejections = [r.get("reason") for r in f.get("rejections") or [] if r.get("reason")]
        notes = []
        ext = os.path.splitext(name)[1].lower()
        if ext and ext not in VIDEO:
            notes.append(f"{ext} isn't a usual video file")
        if "sample" in name.lower():
            notes.append("Looks like a sample, not the real thing")
        if size and size < SMALL[app]:
            notes.append(f"Very small for a whole {thing}")
        if not where:
            notes.append(f"{client.name} can't tell which {thing} this is: pick it")
        for w in where:
            seen.setdefault(w, []).append(name)
        quality = (f.get("quality") or {}).get("quality") or {}
        series = f.get("series") or {}
        files.append({
            "name": name, "size": size, "as": where, "quality": quality.get("name"), "qualityId": quality.get("id"),
            "languages": [{"id": x.get("id"), "name": x.get("name")} for x in f.get("languages") or []],
            "releaseGroup": f.get("releaseGroup") or "", "rejections": rejections, "notes": rejections + notes,
            "episodes": eps, "seriesId": series.get("id") or (item["ownerId"] if app == "sonarr" else None),
            "movie": {"id": movie.get("id"), "title": movie.get("title"), "year": movie.get("year")} if movie else None,
            "ready": bool(where), "_raw": f,
        })
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
            bad = os.path.splitext(x["name"])[1].lower() in DANGEROUS
            others.append({"name": x["name"], "size": x["size"], "danger": bad})
            if bad:
                danger.append(x["name"])
                warnings.append(f"{x['name']} is a program, not a video. Don't import this: delete the download.")
    if not files:
        warnings.append(f"{client.name} found no video files it could import.")
    owner = {"id": item["ownerId"], "title": item["title"], "year": item["year"]}
    out = {
        "app": app, "downloadId": download_id, "title": item["title"], "year": item["year"],
        "release": item["release"], "folder": item["folder"], "messages": item["messages"],
        "episodes": item["episodes"], "warnings": warnings, "others": others[:50],
        "files": files, "ok": bool(files) and not danger,
        "series": owner if app == "sonarr" else None, "movie": owner if app == "radarr" else None,
        "options": await _options(client, app),
    }
    if app == "sonarr" and item["ownerId"]:
        try:
            out["options"]["episodes"] = await episodes_of(services, item["ownerId"])
        except Exception as e:
            logger.info(f"Blocked imports: no episode list for {item['title']}: {e}")
    return out


def public(p: dict) -> dict:
    """A preview as the website and app see it (without Sonarr's raw entries)."""
    return {**p, "files": [{k: v for k, v in f.items() if k != "_raw"} for f in p["files"]]}


async def do_import(services, app: str, download_id: str, choices: Optional[List[dict]] = None) -> str:
    """Import it through Sonarr's/Radarr's Manual Import, as their own Activity page
    would, with the admin's choices for each file (`choices`: name, skip, seriesId,
    episodeIds / movieId, qualityId, languageIds, releaseGroup). Every choice is checked
    against Sonarr/Radarr; a program in the download, an unplaced file or two files as
    one episode refuse it."""
    p = await preview(services, app, download_id)
    client = _client(services, app)
    thing = "episode" if app == "sonarr" else "film"
    if not p["ok"]:
        raise ValueError("Not importing this one from Plexbie: " + (p["warnings"][-1] if p["warnings"] else "nothing in it can be imported."))
    by_name = {c.get("name"): c for c in choices or [] if isinstance(c, dict)}
    unknown = set(by_name) - {f["name"] for f in p["files"]}
    if unknown:
        raise ValueError("The download changed since you looked at it. Look again.")
    qualities = {q["id"]: q for q in p["options"]["qualities"]}
    languages = {x["id"]: x for x in p["options"]["languages"]}
    episode_ids: Dict[int, set] = {}

    async def series_episodes(series_id: int) -> set:
        if series_id not in episode_ids:
            episode_ids[series_id] = {e["id"] for e in await episodes_of(services, series_id)}
        return episode_ids[series_id]
    movie_ids = None
    files, claimed = [], {}
    for f in p["files"]:
        c, raw = by_name.get(f["name"], {}), f["_raw"]
        if c.get("skip"):
            continue
        entry = {"path": raw["path"], "folderName": raw.get("folderName"), "quality": raw.get("quality"),
                 "languages": raw.get("languages") or [], "releaseGroup": raw.get("releaseGroup"),
                 "downloadId": raw.get("downloadId") or download_id, "indexerFlags": raw.get("indexerFlags", 0)}
        if c.get("qualityId") is not None:
            q = qualities.get(c["qualityId"])
            if not q:
                raise ValueError(f"{client.name} doesn't have that quality.")
            entry["quality"] = {"quality": {"id": q["id"], "name": q["name"]},
                                "revision": ((raw.get("quality") or {}).get("revision") or {"version": 1, "real": 0, "isRepack": False})}
        if c.get("languageIds") is not None:
            if not isinstance(c["languageIds"], list) or not all(i in languages for i in c["languageIds"]):
                raise ValueError(f"{client.name} doesn't have that language.")
            entry["languages"] = [languages[i] for i in c["languageIds"]]
        if c.get("releaseGroup") is not None:
            entry["releaseGroup"] = str(c["releaseGroup"])[:60]
        if app == "sonarr":
            series_id = int(c.get("seriesId") or f["seriesId"] or 0)
            ids = c.get("episodeIds") if c.get("episodeIds") is not None else [e["id"] for e in f["episodes"]]
            if not series_id or not ids:
                raise ValueError(f"Pick which episode {f['name']} is (or skip it).")
            if not set(ids) <= await series_episodes(series_id):
                raise ValueError(f"Those episodes aren't in that show ({f['name']}).")
            for i in ids:
                if i in claimed:
                    raise ValueError(f"{claimed[i]} and {f['name']} are both set as the same episode.")
                claimed[i] = f["name"]
            entry.update(seriesId=series_id, episodeIds=list(ids), releaseType=raw.get("releaseType", "unknown"))
        else:
            movie_id = c.get("movieId") if c.get("movieId") is not None else (f["movie"] or {}).get("id")
            if not movie_id:
                raise ValueError(f"Pick which film {f['name']} is (or skip it).")
            if c.get("movieId") is not None:
                movie_ids = movie_ids or {m.get("id") for m in await client.movies()}
                if movie_id not in movie_ids:
                    raise ValueError("That film isn't in Radarr.")
            entry["movieId"] = movie_id
        files.append(entry)
    if not files:
        raise ValueError(f"Every file is skipped: nothing to import.")
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
