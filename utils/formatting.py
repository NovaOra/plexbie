# path: utils/formatting.py
"""Display helpers for media labels."""
from typing import Optional

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
