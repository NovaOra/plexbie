# path: portal/invites.py
"""Invite links: onto the household Plex without Discord.

An admin makes a link on the Manage page and sends it to one person. Opening it
and signing in with Plex shares the server with that account straight away; the
admin already said yes by making the link, so there is no approval step.

What keeps a stranger out:
  * the link carries 192 random bits, and only its SHA-256 is stored, so the
    database (or a backup of it) holds nothing that opens an invite;
  * one use, then it's spent; it also expires (7 days by default) and an admin
    can revoke it while it's unused;
  * optionally locked to an email: then only the Plex account with that email
    can use it, once plex.tv says the email is confirmed, so a forwarded link is
    worthless;
  * redemption runs under a per-invite lock, so two sign-ins racing on one link
    cannot both get in;
  * a bad, used, expired or revoked link all look the same from outside.
Plex sign-in for people who weren't invited is not a way in: it only lets
existing members in (see actions.join).
"""
import asyncio
import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from core.logging import get_logger
from database.kv_store import kv_delete, kv_get, kv_get_all, kv_set
from database.models import utc_now

logger = get_logger(__name__)

NAMESPACE = "web_invites"
TOKEN_BYTES = 24                  # 192 bits, 32 url-safe characters
DEFAULT_DAYS = 7
MAX_DAYS = 30
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{32}$")
_EMAIL_SHAPE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def status(rec: dict, now: Optional[datetime] = None) -> str:
    """active, used, revoked or expired."""
    if rec.get("used_at"):
        return "used"
    if rec.get("revoked_at"):
        return "revoked"
    try:
        expires = datetime.fromisoformat(rec["expires_at"])
    except (KeyError, TypeError, ValueError):
        return "expired"
    return "active" if expires > (now or utc_now()) else "expired"


def public(key: str, rec: dict) -> dict:
    """What the Manage page sees: never the link itself, which only exists at creation."""
    return {
        "id": key,
        "label": rec.get("label") or "",
        "email": rec.get("email"),
        "createdBy": rec.get("created_by"),
        "createdAt": rec.get("created_at"),
        "expiresAt": rec.get("expires_at"),
        "status": status(rec),
        "usedBy": rec.get("used_by"),
        "usedAt": rec.get("used_at"),
    }


class Invites:
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    async def create(self, *, label: str, email: Optional[str], days: int, actor: str) -> Tuple[str, dict]:
        label = (label or "").strip()[:60]
        if not label:
            raise ValueError("Give the invite a name, so you know who it's for.")
        email = (email or "").strip().lower() or None
        if email and not _EMAIL_SHAPE.match(email):
            raise ValueError("That doesn't look like an email address.")
        days = max(1, min(MAX_DAYS, int(days or DEFAULT_DAYS)))
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = utc_now()
        rec = {
            "label": label,
            "email": email,
            "created_by": actor,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=days)).isoformat(),
        }
        key = digest(token)
        await kv_set(NAMESPACE, key, rec)
        logger.info(f"{actor} made an invite link for {label!r} ({days} days{', email-locked' if email else ''})")
        return token, public(key, rec)

    async def find(self, token: Optional[str]) -> Optional[Tuple[str, dict]]:
        """The invite behind a link, if the link is well-formed and still usable."""
        if not token or not _TOKEN_SHAPE.match(token):
            return None
        return await self.usable(digest(token))

    async def usable(self, key: Optional[str]) -> Optional[Tuple[str, dict]]:
        if not key:
            return None
        rec = await kv_get(NAMESPACE, key)
        if not isinstance(rec, dict) or status(rec) != "active":
            return None
        return key, rec

    async def all(self) -> List[dict]:
        """Active first, then used, expired, revoked; newest first within each."""
        rows = [public(k, r) for k, r in (await kv_get_all(NAMESPACE)).items() if isinstance(r, dict)]
        grouped: Dict[str, List[dict]] = {}
        for r in rows:
            grouped.setdefault(r["status"], []).append(r)
        out: List[dict] = []
        for s in ("active", "used", "expired", "revoked"):
            out += sorted(grouped.get(s, []), key=lambda r: r["createdAt"] or "", reverse=True)
        return out[:100]

    async def revoke(self, key: str, actor: str) -> dict:
        async with self.lock(key):
            rec = await kv_get(NAMESPACE, key)
            if not isinstance(rec, dict):
                return {"ok": False, "message": "No such invite."}
            if status(rec) != "active":
                return {"ok": False, "message": f"That invite is already {status(rec)}."}
            rec.update(revoked_at=utc_now().isoformat(), revoked_by=actor)
            await kv_set(NAMESPACE, key, rec)
        logger.info(f"{actor} revoked the invite link for {rec.get('label')!r}")
        return {"ok": True, "message": f"The link for {rec.get('label')} no longer works."}

    async def delete(self, key: str, actor: str) -> dict:
        """Remove a cancelled or expired invite from the list. Used ones are kept: they are
        the record of who joined, and "who brought whom" reads them."""
        async with self.lock(key):
            rec = await kv_get(NAMESPACE, key)
            if not isinstance(rec, dict):
                return {"ok": False, "message": "No such invite."}
            if status(rec) not in ("revoked", "expired"):
                return {"ok": False, "message": "Only cancelled or expired invites can be deleted."}
            await kv_delete(NAMESPACE, key)
        logger.info(f"{actor} deleted the {status(rec)} invite for {rec.get('label')!r}")
        return {"ok": True, "message": f"Deleted the invite for {rec.get('label')}."}

    async def renew(self, key: str, actor: str) -> Tuple[str, dict]:
        """A fresh link for the same person: same name and email lock, a new code.

        An open invite's old link stops working at once (someone lost it); a cancelled
        or expired one is replaced; a used one is kept as history and a new invite is
        made beside it ("invite again"). The new link lasts as long as the original did.
        """
        async with self.lock(key):
            rec = await kv_get(NAMESPACE, key)
            if not isinstance(rec, dict):
                raise ValueError("No such invite.")
            try:
                days = (datetime.fromisoformat(rec["expires_at"]) - datetime.fromisoformat(rec["created_at"])).days or DEFAULT_DAYS
            except (KeyError, TypeError, ValueError):
                days = DEFAULT_DAYS
            if status(rec) != "used":
                await kv_delete(NAMESPACE, key)
        token, info = await self.create(label=rec.get("label") or "Someone", email=rec.get("email"), days=days, actor=actor)
        logger.info(f"{actor} made a new link for {rec.get('label')!r} (was {status(rec)})")
        return token, info

    async def redeem(self, key: str, me: dict, join) -> str:
        """Use an invite for the Plex account `me`. Returns an outcome word.

        `join(rec)` does the sharing and returns True on success; it only runs
        while the invite is held and still unused, and the invite is spent only
        if it succeeds.
          ok           shared with them
          email        locked to another email; the invite is untouched
          unconfirmed  locked to their email, but plex.tv says they haven't confirmed
                       it yet; the invite is untouched
          invalid      used, expired, revoked or unknown
          failed       Plex refused; the invite is untouched so they can retry
        """
        async with self.lock(key):
            found = await self.usable(key)
            if not found:
                return "invalid"
            _, rec = found
            if rec.get("email") and rec["email"] != (me.get("email") or "").strip().lower():
                logger.info(f"Invite for {rec.get('label')!r} refused: signed in with a different email")
                return "email"
            if rec.get("email") and me.get("confirmed") is False:
                # Anyone can make a Plex account with an address that isn't theirs.
                # Only an explicit "not confirmed" holds it back, so a plex.tv answer
                # without the flag can't stop every locked invite.
                logger.info(f"Invite for {rec.get('label')!r} held: the Plex account hasn't confirmed its email")
                return "unconfirmed"
            if not await join(rec):
                return "failed"
            rec.update(used_at=utc_now().isoformat(), used_by=me.get("username") or me.get("title"), used_by_id=str(me.get("id")))
            try:
                await kv_set(NAMESPACE, key, rec)
            except Exception as e:
                # They're on Plex now; telling them it failed would only send them round again.
                logger.error(f"Invite for {rec.get('label')!r} used, but could not be marked used: {type(e).__name__}")
        logger.info(f"Invite for {rec.get('label')!r} used by {rec.get('used_by')}")
        return "ok"
