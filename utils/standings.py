# path: utils/standings.py
"""Watch-time standings, shared by the leaderboard and the inactivity check.

Both need the same answer to "who has watched the most", and they have to agree.
The leaderboard is what users see; the inactivity check exempts the top few from
removal. Two implementations of the same sum would drift, and the first symptom
would be somebody removed while the board still showed them in the top three.

Three identity namespaces meet here, which is the whole reason this is fiddly:

  * Plex account names, as they appear in a session (e.g. "Sam Rivera")
  * Tautulli friendly_names, which can differ for the same person
    (e.g. "Sam.Rivera" - a dot, for that same account)
  * whatever config/user_aliases.json maps between them

An alias entry maps one of those names onto the name everything else should be
grouped under. It is not a typo fix; the two spellings are genuinely different
identifiers from different systems.
"""
import json
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from core.logging import get_logger

logger = get_logger(__name__)

#: One definition of where the alias map lives, so callers cannot drift apart.
ALIASES_FILE = Path("config/user_aliases.json")
#: Watch streaks, written by watch_tracking and read by the website.
STREAKS_FILE = Path("config/watch_streaks.json")

#: How many of the highest watchers are treated as exempt from inactivity removal.
DEFAULT_TOP_WATCHERS = 3


def resolve_alias(name: str, aliases: Optional[Dict[str, str]]) -> str:
    """The name `name` should be grouped under."""
    if not aliases:
        return name
    return aliases.get(name, name)


def merge_aliased_users(
    users: Iterable[dict], aliases: Optional[Dict[str, str]]
) -> List[dict]:
    """Combine Tautulli user rows that alias to the same person.

    Sums `duration` and `plays`; keeps the first `user_id` seen. Rows are returned
    under their primary name.
    """
    if not aliases:
        return list(users)

    combined: Dict[str, dict] = {}
    for user in users:
        friendly_name = user.get("friendly_name") or "Unknown"
        primary = resolve_alias(friendly_name, aliases)
        entry = combined.get(primary)
        if entry is None:
            combined[primary] = {
                "friendly_name": primary,
                "duration": user.get("duration", 0) or 0,
                "plays": user.get("plays", 0) or 0,
                "user_id": user.get("user_id"),
            }
        else:
            entry["duration"] += user.get("duration", 0) or 0
            entry["plays"] += user.get("plays", 0) or 0
    return list(combined.values())


def standings(
    users: Iterable[dict],
    aliases: Optional[Dict[str, str]] = None,
    credits: Optional[Dict[str, int]] = None,
) -> List[Tuple[str, int]]:
    """[(primary_name, total_seconds)], highest first, excluding zero totals.

    `credits` is watch-party time keyed by Plex username. Those keys are resolved
    through the same aliases before being added, so a person whose Plex name and
    Tautulli name differ still gets one combined total rather than two partial
    ones.

    Ties are broken by name, ascending, so the ordering is stable across calls -
    which matters when the cut-off for exemption falls inside a tie.
    """
    totals: Dict[str, int] = {}
    for user in merge_aliased_users(users, aliases):
        name = user.get("friendly_name") or "Unknown"
        totals[name] = totals.get(name, 0) + int(user.get("duration", 0) or 0)

    for plex_username, seconds in (credits or {}).items():
        primary = resolve_alias(plex_username, aliases)
        totals[primary] = totals.get(primary, 0) + int(seconds or 0)

    ranked = [(name, total) for name, total in totals.items() if total > 0]
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked


def top_watchers(
    users: Iterable[dict],
    aliases: Optional[Dict[str, str]] = None,
    credits: Optional[Dict[str, int]] = None,
    limit: int = DEFAULT_TOP_WATCHERS,
) -> List[str]:
    """The primary names of the `limit` highest watchers.

    Fewer than `limit` people with any watch time means all of them qualify -
    there is no way to be "outside the top three" of two people.

    Returns a list rather than a set so the ranking stays inspectable in logs.
    """
    if limit <= 0:
        return []
    return [name for name, _ in standings(users, aliases, credits)[:limit]]


def load_aliases(path: Optional[Path] = None) -> Dict[str, str]:
    """Read the alias map, or {} if it is missing or unreadable.

    Blocking (it reads a file), so call it via run_blocking from an async context.
    watch_tracking keeps its own (mtime, size)-cached reader because it needs this
    on a ten-second loop; this plain form is for callers that need it once.
    """
    target = path or ALIASES_FILE
    try:
        data = json.loads(target.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Could not read aliases from {target}: {e}")
        return {}
    aliases = data.get("aliases", {})
    return aliases if isinstance(aliases, dict) else {}


def load_streaks(path: Optional[Path] = None, strict: bool = False) -> Dict[str, dict]:
    """Read the watch streaks, or {} if there is no file yet.

    A file that cannot be read or parsed is logged and read as {}, which suits
    callers that only show streaks. With `strict`, it raises instead (OSError or
    ValueError): the hourly update writes back what it read, and reading a
    half-written file as {} would replace everyone's history with today's.

    Blocking (it reads a file), so call it via run_blocking from an async context.
    """
    target = path or STREAKS_FILE
    try:
        streaks = json.loads(target.read_text())
        if not isinstance(streaks, dict):
            raise ValueError(f"expected an object, found {type(streaks).__name__}")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        if strict:
            raise
        logger.error(f"Could not read watch streaks from {target}: {e}")
        return {}
    return streaks


def save_streaks(streaks: Dict[str, dict], path: Optional[Path] = None) -> None:
    """Write the watch streaks in one step.

    Writes a temp file beside it and os.replace()s it into place, so a crash or
    a full disk mid-write leaves the previous file whole rather than truncated.
    Each save gets its own temp file: the hourly update and a refresh after a
    watch can overlap, and two writers sharing one would mix their output.
    Blocking, so call it via run_blocking from an async context.
    """
    target = path or STREAKS_FILE
    fd, name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        # mkstemp makes the file 0600; keep the file readable as before.
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(streaks, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
