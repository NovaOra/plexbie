# path: portal/plex.py
"""Plex reads for the portal, as plain data.

Everything here is synchronous and must be called through run_blocking. It reads
Plex's XML endpoints directly with server.query() rather than walking plexapi
objects: a list of 1,300 films is one request this way, whereas touching an
attribute plexapi had not loaded would quietly issue a request per item.
"""
import re
from typing import Dict, List, Optional
from urllib.parse import urlencode

#: Only artwork paths of this shape may be proxied, so the image route can never
#: be used to fetch arbitrary Plex URLs with the server's token.
PLEX_ART_PATH = re.compile(r"^/library/metadata/\d+/(thumb|art)/\d+$")


def plex_art(path: Optional[str], width: int = 342) -> Optional[str]:
    """Site URL for a Plex poster or backdrop, served through /img/plex."""
    if not path or not PLEX_ART_PATH.match(path):
        return None
    return "/img/plex?" + urlencode({"p": path, "w": width, "v": 2})


def _guids(el) -> Dict[str, str]:
    out = {}
    for g in el.findall("Guid"):
        gid = g.get("id", "")
        if "://" in gid:
            scheme, value = gid.split("://", 1)
            out[scheme] = value
    return out


def _genres(el) -> List[str]:
    return [g.get("tag") for g in el.findall("Genre") if g.get("tag")][:3]


def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def sections(server) -> List[dict]:
    """Every library section with its item count."""
    root = server.query("/library/sections")
    out = []
    for d in root.findall("Directory"):
        key = d.get("key")
        kind = d.get("type")
        count = 0
        if kind in ("movie", "show", "artist"):
            size = server.query(f"/library/sections/{key}/all?X-Plex-Container-Start=0&X-Plex-Container-Size=0")
            count = _int(size.get("totalSize"), _int(size.get("size")))
        out.append({"key": key, "title": d.get("title"), "kind": kind, "count": count})
    return out


def _video(el, kind: str) -> dict:
    g = _guids(el)
    return {
        "kind": kind,
        "id": g.get("tmdb") or f"plex:{el.get('ratingKey')}",
        "ratingKey": el.get("ratingKey"),
        "tmdb": g.get("tmdb"),
        "title": el.get("title"),
        "year": el.get("year") or "",
        "poster": plex_art(el.get("thumb")),
        "backdrop": plex_art(el.get("art"), 1280),
        "genres": _genres(el),
        "addedAt": _int(el.get("addedAt")),
        "availability": "available",
    }


def watch_url(server, rating_key) -> Optional[str]:
    """Plex's own page for a title (app.plex.tv): opens it for anyone the server is shared with."""
    machine = getattr(server, "machineIdentifier", None)
    if not machine or not rating_key:
        return None
    return (f"https://app.plex.tv/desktop/#!/server/{machine}/details"
            f"?key=%2Flibrary%2Fmetadata%2F{rating_key}")


def library(server, kind: str) -> List[dict]:
    """Every film ("movie") or show ("tv") on the server, newest first."""
    stype = "movie" if kind == "movie" else "show"
    items = []
    for sec in sections(server):
        if sec["kind"] != stype:
            continue
        root = server.query(f"/library/sections/{sec['key']}/all?includeGuids=1")
        tag = "Video" if stype == "movie" else "Directory"
        items.extend(_video(el, kind) for el in root.findall(tag))
    items.sort(key=lambda t: t["addedAt"], reverse=True)
    return items


def recent(server, per_section: int = 12) -> List[dict]:
    """Recent arrivals: films individually, episodes grouped by show."""
    out = []
    for sec in sections(server):
        if sec["kind"] == "movie":
            root = server.query(
                f"/library/sections/{sec['key']}/all?type=1&sort=addedAt:desc&includeGuids=1"
                f"&X-Plex-Container-Start=0&X-Plex-Container-Size={per_section}"
            )
            for el in root.findall("Video"):
                out.append({"title": _video(el, "movie"), "addedAt": _int(el.get("addedAt")), "detail": None})
        elif sec["kind"] == "show":
            root = server.query(
                f"/library/sections/{sec['key']}/all?type=4&sort=addedAt:desc"
                f"&X-Plex-Container-Start=0&X-Plex-Container-Size={per_section * 5}"
            )
            shows: Dict[str, dict] = {}
            for ep in root.findall("Video"):
                key = ep.get("grandparentRatingKey")
                if not key:
                    continue
                show = shows.setdefault(key, {"eps": [], "addedAt": _int(ep.get("addedAt")), "el": ep})
                show["eps"].append((_int(ep.get("parentIndex")), _int(ep.get("index"))))
            for key, show in list(shows.items())[:per_section]:
                meta = server.query(f"/library/metadata/{key}?includeGuids=1").find("Directory")
                if meta is None:
                    continue
                title = _video(meta, "tv")
                eps = sorted(show["eps"])
                seasons = sorted({s for s, _ in eps})
                if len(eps) == 1:
                    detail = f"S{eps[0][0]} E{eps[0][1]}"
                elif len(seasons) == 1:
                    detail = f"Season {seasons[0]}, {len(eps)} episodes"
                else:
                    detail = f"{len(eps)} new episodes"
                out.append({"title": title, "addedAt": show["addedAt"], "detail": detail})
    out.sort(key=lambda a: a["addedAt"], reverse=True)
    return out


def now_playing(items) -> List[dict]:
    """Plain data from the shared Plex sessions snapshot (services.plex_sessions()).

    Reads only attributes the sessions response already carries; still call it
    via run_blocking, because plexapi may fetch an attribute it lacks.
    """
    out = []
    for item in items or []:
        episode = getattr(item, "type", "") == "episode"
        users = getattr(item, "usernames", None) or []
        players = getattr(item, "players", None) or []
        player = players[0] if players else None
        duration = getattr(item, "duration", 0) or 0
        offset = getattr(item, "viewOffset", 0) or 0
        thumb = getattr(item, "grandparentThumb", None) if episode else getattr(item, "thumb", None)
        out.append({
            "plexUser": users[0] if users else "Someone",
            "title": getattr(item, "grandparentTitle", None) if episode else getattr(item, "title", ""),
            "subtitle": f"S{getattr(item, 'parentIndex', '')} E{getattr(item, 'index', '')}, {getattr(item, 'title', '')}" if episode else None,
            "poster": plex_art(thumb, 185),
            "progress": round(offset / duration, 3) if duration else 0,
            "device": (getattr(player, "title", None) or getattr(player, "product", "")) if player else "",
        })
    return out
