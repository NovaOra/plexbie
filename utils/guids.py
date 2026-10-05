# path: utils/guids.py
"""Plex GUIDs ("tmdb://603", "tvdb://81189?lang=en") to the number inside."""
from typing import Optional


def guid_number(guid: Optional[str], scheme: str) -> Optional[int]:
    """The id in a Plex GUID for `scheme` ("tmdb", "tvdb"), or None if it has none."""
    prefix = f"{scheme}://"
    if not guid or prefix not in guid:
        return None
    try:
        return int(str(guid).split(prefix, 1)[1].split("?", 1)[0].split("/", 1)[0])
    except ValueError:
        return None
