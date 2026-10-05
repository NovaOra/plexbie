# path: core/message_log.py
"""What Plexbie said to whom and how it got there, and what people said back.

Every message the bot sends a person is recorded once: Discord DMs (through
admin_mirror.send_user_dm and the arrival notice), and for people without
Discord the website alert or email from core.notify, including the ones that
had nowhere to go. What people send Plexbie is recorded too (direction "in"):
a DM to the bot, "Something wrong?", and answers on their ticket from Discord,
the website or the app. The Manage page reads it as a conversation per person.

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

#: How a message went out (or, for one sent to Plexbie, where it came from).
CHANNELS = ("discord", "web", "email", "none")
DIRECTIONS = ("out", "in")


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
                 title: Optional[str] = None, error: Optional[str] = None, direction: str = "out") -> None:
    """Note one message to one person (or, with direction="in", from them). Never raises."""
    global _writes
    try:
        now = datetime.now(timezone.utc)
        key = f"{now.strftime('%Y%m%dT%H%M%S%f')}-{secrets.token_hex(3)}"
        await kv_set(NAMESPACE, key, {
            "at": now.isoformat(),
            "direction": direction if direction in DIRECTIONS else "out",
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
        p = by.setdefault(k, {"id": k, "name": None, "count": 0, "received": 0, "failed": 0, "via": set()})
        p["name"] = e.get("discord_name") or e.get("plex_name") or p["name"] or "Someone"
        incoming = e.get("direction") == "in"
        p["received" if incoming else "count"] += 1
        p["failed"] += 0 if e.get("delivered") else 1
        p["via"].add(e.get("channel"))
        p["last"] = {"at": e.get("at"), "text": e.get("title") or e.get("text"), "channel": e.get("channel"),
                     "delivered": e.get("delivered"), "direction": "in" if incoming else "out"}
    out = [{**p, "via": sorted(v for v in p["via"] if v)} for p in by.values()]
    out.sort(key=lambda p: p["last"]["at"] or "", reverse=True)
    return out


async def conversation(person: str) -> List[Dict[str, Any]]:
    """Every logged message to and from one person, oldest first."""
    out = []
    for key, e in sorted((await kv_get_all(NAMESPACE)).items()):
        if isinstance(e, dict) and person_key(e) == person:
            out.append({"id": key, "at": e.get("at"), "direction": "in" if e.get("direction") == "in" else "out",
                        "channel": e.get("channel"), "delivered": e.get("delivered"),
                        "title": e.get("title"), "text": e.get("text"), "context": e.get("context"), "error": e.get("error")})
    return out


async def record_dm(message) -> None:
    """A Discord DM someone sent Plexbie (an on_message listener). Never raises."""
    try:
        if getattr(message, "guild", None) is not None or getattr(message.author, "bot", False):
            return
        text = message.content or ""
        files = [a.filename for a in getattr(message, "attachments", None) or []]
        if files:
            text = f"{text}\nAttached: {', '.join(files)}".strip()
        if not text.strip():
            return
        author = message.author
        await record(channel="discord", direction="in", text=text, context="Discord DM",
                     discord_id=author.id, discord_name=getattr(author, "display_name", None) or author.name)
    except Exception as e:
        logger.warning(f"Could not log a DM to Plexbie: {type(e).__name__}: {e}")


async def record_from_ticket(h: dict, *, text: str, title: str, source: str, context: str) -> None:
    """Something a member sent on their ticket, in the same conversation as Plexbie's
    messages to them (their Discord account, else their Plex account)."""
    await record(channel="discord" if source == "discord" else "web", direction="in", text=text, title=title, context=context,
                 discord_id=h.get("discord_id"), discord_name=h.get("who") if h.get("discord_id") else None,
                 plex_name=h.get("plex_name") or (None if h.get("discord_id") else h.get("who")),
                 plex_account_id=h.get("plex_account_id"))
