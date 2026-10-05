# path: utils/embeds.py
"""Discord embed utilities"""
from discord import Embed, Color


def create_info_embed(title: str, description: str = "", **kwargs) -> Embed:
    """Create a standard info embed"""
    embed = Embed(
        title=title,
        description=description,
        color=Color.blue(),
        **kwargs
    )
    return embed


def create_error_embed(title: str, description: str = "", **kwargs) -> Embed:
    """Create an error embed"""
    embed = Embed(
        title=title,
        description=description,
        color=Color.red(),
        **kwargs
    )
    return embed


MAX_FIELD_VALUE = 1024


def truncate_field(text: str, limit: int = MAX_FIELD_VALUE, suffix: str = "\n… truncated") -> str:
    """Clamp an embed field value to Discord's limit, keeping whole lines.

    Discord rejects a field value over 1024 characters with HTTPException 400,
    which callers surface as a generic error - so an over-long list makes a command
    look broken exactly when it has the most to report. the Plex account listing hit
    this: ten entries describing over-long usernames came to roughly 2068
    characters.
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text

    room = limit - len(suffix)
    if room <= 0:
        return text[:limit]

    clipped = text[:room]
    # Prefer cutting at a line boundary so an entry is not left half-rendered.
    newline = clipped.rfind("\n")
    if newline > room // 2:
        clipped = clipped[:newline]
    return clipped + suffix
