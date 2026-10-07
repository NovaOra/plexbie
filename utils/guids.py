# path: utils/guids.py
"""Plex GUIDs ("tmdb://603", "tvdb://81189?lang=en") to the ids inside.

One reader for both of Plex's shapes, so the cleanup task (plexapi objects) and the
website's countdown (Plex's XML) tie a title to the same ids."""
from typing import Dict, Iterable, List, Optional

#: The ids a title is matched by: Seerr's and Radarr's (TMDB), Sonarr's (TheTVDB), IMDb.
SCHEMES = ("tmdb", "tvdb", "imdb")


def guid_number(guid: Optional[str], scheme: str) -> Optional[int]:
    """The id in a Plex GUID for `scheme` ("tmdb", "tvdb"), or None if it has none."""
    prefix = f"{scheme}://"
    if not guid or prefix not in guid:
        return None
    try:
        return int(str(guid).split(prefix, 1)[1].split("?", 1)[0].split("/", 1)[0])
    except ValueError:
        return None


def item_guids(item) -> List[str]:
    """A plexapi item's GUIDs: its own (`guid`), then each of its `guids`, by id or,
    without one, by tag."""
    out = [str(item.guid)] if getattr(item, "guid", None) else []
    for g in getattr(item, "guids", None) or []:
        out.append(str(getattr(g, "id", None) or getattr(g, "tag", None) or g))
    return out


def element_guids(el) -> List[str]:
    """The same off Plex's XML: a listed film's or show's guid attribute, then its Guid
    tags (listed with includeGuids=1)."""
    out = [el.get("guid")] if el.get("guid") else []
    return out + [g.get("id") for g in el.findall("Guid") if g.get("id")]


def plex_ids(guids: Iterable[Optional[str]]) -> Dict[str, str]:
    """A title's TMDB / TheTVDB / IMDb ids among its GUIDs ("tmdb://1396"), the first
    of each, as written."""
    out: Dict[str, str] = {}
    for gid in guids:
        gid = str(gid or "")
        if "://" in gid:
            k, v = gid.split("://", 1)
            if k in SCHEMES and v:
                out.setdefault(k, v)
    return out


def first_number(guids: Iterable[Optional[str]], scheme: str) -> Optional[int]:
    """The first id for `scheme` among a title's GUIDs that guid_number can read."""
    for gid in guids:
        number = guid_number(gid, scheme)
        if number:
            return number
    return None
