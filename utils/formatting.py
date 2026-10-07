# path: utils/formatting.py
"""Display helpers for media labels and stored times."""
from datetime import datetime, timezone
from typing import Any, Optional

#: Shown when an episode's season or number is unknown.
UNKNOWN_MARKER = "??"


def season_episode(season: Optional[int], episode: Optional[int]) -> str:
    """Format a season/episode label, tolerating missing numbers.

    Returns "S01E02", or a partial label such as "S01E??" when one number is
    unavailable.

    plexapi leaves parentIndex and index as None for episodes whose season
    metadata is incomplete, and every call site formatted them directly with
    :02d - which raises TypeError: unsupported format string passed to
    NoneType.__format__. The consequences were invisible rather than loud: inside
    watch_tracking's 10-second loop the exception was swallowed, so the Now
    Watching display silently stopped updating for as long as such an episode was
    playing, and in /recent it aborted the whole command.
    """
    part_season = f"S{season:02d}" if isinstance(season, int) else f"S{UNKNOWN_MARKER}"
    part_episode = f"E{episode:02d}" if isinstance(episode, int) else f"E{UNKNOWN_MARKER}"
    return f"{part_season}{part_episode}"


def episode_label(
    show: Optional[str],
    season: Optional[int],
    episode: Optional[int],
    title: Optional[str] = None,
) -> str:
    """Format a full episode label: "Show - S01E02: Title".

    Omits the pieces that are missing rather than rendering "None".
    """
    show_part = show or "Unknown Show"
    label = f"{show_part} - {season_episode(season, episode)}"
    if title:
        label = f"{label}: {title}"
    return label


def ensure_utc(when: Optional[datetime]) -> Optional[datetime]:
    """A stored time with its zone: one without a zone is UTC.

    Every timestamp column holds UTC, but SQLite gives it back without a zone, and
    Python reads a zone-less datetime as the host's local time - so .timestamp()
    and .isoformat() were off by the host's offset whenever TZ was set. Not for
    plexapi's datetimes: those are the server's local time (portal.cleanup.aware).
    """
    if when is None or when.tzinfo is not None:
        return when
    return when.replace(tzinfo=timezone.utc)


def parse_utc(value: Any) -> Optional[datetime]:
    """A stored time as a UTC-aware datetime, or None when it isn't one.

    Takes a datetime, Unix seconds, or an ISO string with or without a zone (a
    trailing "Z" included); a zone-less one is UTC. Each caller decides what a
    missing or unreadable time means.
    """
    if not value or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value))
    except ValueError:
        return None
