# path: core/message_log.py
"""What Plexbie said to whom, and how it got there.

Every message the bot sends a person is recorded once: Discord DMs (through
admin_mirror.send_user_dm and the arrival notice), and for people without
Discord the website alert or email from core.notify, including the ones that
had nowhere to go. The Manage page reads it as a conversation per person.

Kept for KEEP_DAYS and at most MAX_ENTRIES; older entries are trimmed as new
ones are written. Logging never raises: a message that was sent must not be
reported as failed because its log entry could not be written.
"""
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.logging import get_logger
from database.kv_store import kv_delete_many, kv_get_all, kv_set

logger = get_logger(__name__)

NAMESPACE = "message_log"
KEEP_DAYS = 90
MAX_ENTRIES = 3000
TRIM_EVERY = 25          # writes between trims
_writes = 0

#: How a message went out.
CHANNELS = ("discord", "web", "email", "none")


def embed_text(embed) -> str:
    """A Discord embed as plain lines: title, description, then each field."""
    if embed is None:
        return ""
    parts: List[str] = []
    if getattr(embed, "title", None):
        parts.append(str(embed.title))
    if getattr(embed, "description", None):
        parts.append(str(embed.description))
    for f in getattr(embed, "fields", []) or []:
        parts.append(f"{f.name}: {f.value}")
    return "\n".join(parts)


def _plain(text: str) -> str:
    """notify.plain, with Discord's <t:…> timestamps written out as dates, cut to fit the log."""
    from core.notify import plain
    text = re.sub(r"<t:(\d+)(:[A-Za-z])?>", lambda m: datetime.fromtimestamp(int(m.group(1)), timezone.utc).strftime("%d %b %Y"), text or "")
    return plain(text)[:2000]


async def record(*, channel: str, text: str, context: str = "", delivered: bool = True,
                 discord_id: Optional[Any] = None, discord_name: Optional[str] = None,
                 plex_name: Optional[str] = None, plex_account_id: Optional[Any] = None,
                 title: Optional[str] = None, error: Optional[str] = None) -> None:
    """Note one message to one person. Never raises."""
    global _writes
    try:
        now = datetime.now(timezone.utc)
        key = f"{now.strftime('%Y%m%dT%H%M%S%f')}-{secrets.token_hex(3)}"
        await kv_set(NAMESPACE, key, {
            "at": now.isoformat(),
            "channel": channel if channel in CHANNELS else "none",
            "delivered": bool(delivered),
            "discord_id": str(discord_id) if discord_id else None,
            "discord_name": discord_name,
            "plex_name": plex_name,
            "plex_account_id": str(plex_account_id) if plex_account_id else None,
            "title": _plain(title or "")[:200] or None,
            "text": _plain(text),
            "context": context[:200],
            "error": (error or "")[:300] or None,
        })
        _writes += 1
        if _writes % TRIM_EVERY == 1:
            await trim()
    except Exception as e:
        logger.warning(f"Could not log a message ({context}): {type(e).__name__}: {e}")


async def trim(now: Optional[datetime] = None) -> int:
    """Drop entries older than KEEP_DAYS, then the oldest beyond MAX_ENTRIES."""
    rows = await kv_get_all(NAMESPACE)
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=KEEP_DAYS)).strftime("%Y%m%dT%H%M%S")
    keys = sorted(rows)
    old = [k for k in keys if k[:15] < cutoff]
    keep = [k for k in keys if k[:15] >= cutoff]
    old += keep[:-MAX_ENTRIES] if len(keep) > MAX_ENTRIES else []
    if old:
        await kv_delete_many(NAMESPACE, old)
    return len(old)


def person_key(entry: Dict[str, Any]) -> str:
    """One conversation per person: their Discord account, else their Plex account."""
    if entry.get("discord_id"):
        return f"d{entry['discord_id']}"
    return f"p{(entry.get('plex_name') or entry.get('plex_account_id') or 'unknown').lower()}"


async def people() -> List[Dict[str, Any]]:
    """Everyone Plexbie has written to, newest conversation first, with the last message."""
    by: Dict[str, Dict[str, Any]] = {}
    for key, e in sorted((await kv_get_all(NAMESPACE)).items()):
        if not isinstance(e, dict):
            continue
        k = person_key(e)
        p = by.setdefault(k, {"id": k, "name": None, "count": 0, "failed": 0, "via": set()})
        p["name"] = e.get("discord_name") or e.get("plex_name") or p["name"] or "Someone"
        p["count"] += 1
        p["failed"] += 0 if e.get("delivered") else 1
        p["via"].add(e.get("channel"))
        p["last"] = {"at": e.get("at"), "text": e.get("title") or e.get("text"), "channel": e.get("channel"),
                     "delivered": e.get("delivered")}
    out = [{**p, "via": sorted(v for v in p["via"] if v)} for p in by.values()]
    out.sort(key=lambda p: p["last"]["at"] or "", reverse=True)
    return out


async def conversation(person: str) -> List[Dict[str, Any]]:
    """Every logged message to one person, oldest first."""
    out = []
    for key, e in sorted((await kv_get_all(NAMESPACE)).items()):
        if isinstance(e, dict) and person_key(e) == person:
            out.append({"id": key, "at": e.get("at"), "channel": e.get("channel"), "delivered": e.get("delivered"),
                        "title": e.get("title"), "text": e.get("text"), "context": e.get("context"), "error": e.get("error")})
    return out
