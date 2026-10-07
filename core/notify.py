# path: core/notify.py
"""Messages for members who don't use Discord.

Discord members get DMs. Someone who joined with an invite link and signed in
with Plex has no DM to receive, so without this they would never hear that a
request arrived, or - worse - that their account is about to be removed for
inactivity. Two channels, in order:

  1. Phone/browser notifications from plexbie.com (Web Push). The member turns
     them on in the website; each browser's subscription is kept in the
     key-value store. Keys (VAPID) are generated on first use and stored there
     too, so there is nothing to configure.
     The Plexbie app registers its own push token the same way (Expo's push
     service delivers it to the phone); see "the app" below.
  2. Email, as the fallback when nothing was delivered by push and the member's
     email is known. Off unless SMTP_HOST, SMTP_USERNAME and SMTP_PASSWORD are
     set (for Proton: smtp.protonmail.ch, port 587, an SMTP token).

Everything here is best-effort: a failure is logged and never raised, because
the callers are background loops (inactivity checks, arrival notices) that must
carry on.
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional

from core.blocking import run_blocking
from core.logging import get_logger
from database.kv_store import kv_delete, kv_delete_many, kv_get, kv_get_all, kv_set

logger = get_logger(__name__)

SUBS_NAMESPACE = "web_push"
APP_NAMESPACE = "app_push"
#: Expo's push service: the app's tokens are Expo push tokens, delivered through FCM/APNs.
EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
EXPO_TOKEN = re.compile(r"ExponentPushToken\[[A-Za-z0-9_-]{10,100}\]")
MAX_APPS = 5                       # phones per member; the oldest registration goes first

#: Set by the website (portal/auth.py): whether an app sign-in, by its key, still holds.
#: True, False, or None when that can't be told yet. A phone registered under a sign-in
#: that has ended (signed out, expired, revoked) gets nothing more and is forgotten.
session_alive: Optional[Callable[[str], Optional[bool]]] = None
KEYS_NAMESPACE = "web_push_keys"
#: Who push services contact about this install's alerts: its own operator (SITE_CONTACT),
#: else its own address, else the Plexbie project.
def vapid_contact() -> str:
    from core.config import public_url
    contact = (os.getenv("SITE_CONTACT") or "").strip()
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", contact):
        return f"mailto:{contact}"
    public = public_url(os.getenv("WEB_PUBLIC_URL", ""))
    return public if public.startswith("https://") else "mailto:support@plexbie.com"


def plain(text: str) -> str:
    """Discord-flavoured text (bold, code, mentions) as plain words for a notification or email."""
    text = re.sub(r"<@!?\d+>", "", text or "")
    text = re.sub(r"(\*\*|__|`|~~)", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


async def _row_for(plex_name: Optional[str], plex_account_id: Optional[str] = None):
    """Plexbie's own tracking row for this person, if any.

    By Plex account id whenever there is one, and then never by name: a name that
    comes with an account id is the plex.tv username someone signed in with, which
    they choose and could set to another member's ("Kids"). A name alone is only
    ever one Plexbie took from its own rows, so it's looked up as given."""
    pid = str(plex_account_id or "")
    if not plex_name and not pid.isdigit():
        return None
    try:
        from database.people import person_by_name, sole_person_for_account
        from database.session import get_session
        async with get_session() as session:
            if pid.isdigit():
                return await sole_person_for_account(session, pid)
            return await person_by_name(session, plex_name)
    except Exception as e:
        logger.debug(f"No tracking row for {plex_name or pid}: {e}")
        return None


async def email_for(plex_name: Optional[str], plex_account_id: Optional[str] = None) -> Optional[str]:
    """The email Plexbie has on file for a Plex account (from joining), if any.

    From Plexbie's own row, or the web join of that account id. Not a join found by
    name: the name on a join is whatever that person's plex.tv username was."""
    row = await _row_for(plex_name, plex_account_id)
    if row and row.plex_email:
        return row.plex_email
    account = str(plex_account_id or (row.plex_user_id if row and row.plex_user_id else "") or "")
    if account:
        rec = (await kv_get_all("web_plex_joins")).get(account)
        if isinstance(rec, dict) and rec.get("email"):
            return rec["email"]
    return None


# ------------------------------------------------------------------ keys
async def vapid_keys() -> Dict[str, str]:
    """{"private": base64url DER, "public": base64url uncompressed point}, made once and kept.

    The private key is stored as base64url DER because that is the form
    pywebpush accepts as a string (a PEM is refused with an ASN.1 error).
    """
    stored = await kv_get(KEYS_NAMESPACE, "vapid")
    if isinstance(stored, dict) and stored.get("private") and stored.get("public"):
        return stored
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    private = base64.urlsafe_b64encode(der).rstrip(b"=").decode()
    point = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    stored = {"private": private, "public": base64.urlsafe_b64encode(point).rstrip(b"=").decode()}
    await kv_set(KEYS_NAMESPACE, "vapid", stored)
    logger.info("Generated the web push keys")
    return stored


# --------------------------------------------------------- subscriptions
def _sub_key(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:40]


#: The browsers' own push services. A subscription can only point at one of these,
#: or a member could make the server send requests to addresses on your network.
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com",
              ".push.apple.com", ".notify.windows.com", "push.microsoft.com")


def _push_host(endpoint: str) -> bool:
    from urllib.parse import urlsplit
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and parts.port in (None, 443) and \
        any(host == h.lstrip(".") or (h.startswith(".") and host.endswith(h)) for h in PUSH_HOSTS)


#: A browser's push subscription is a URL and two short base64 keys; anything
#: bigger isn't one. Each member keeps at most MAX_SUBS browsers (oldest dropped).
MAX_ENDPOINT, MAX_P256DH, MAX_AUTH, MAX_SUBS = 1024, 128, 64, 10


def _valid_subscription(sub: Any) -> bool:
    keys = sub.get("keys") if isinstance(sub, dict) else None
    return (isinstance(sub, dict) and isinstance(sub.get("endpoint"), str) and len(sub["endpoint"]) <= MAX_ENDPOINT
            and _push_host(sub["endpoint"]) and isinstance(keys, dict)
            and isinstance(keys.get("p256dh"), str) and 0 < len(keys["p256dh"]) <= MAX_P256DH
            and isinstance(keys.get("auth"), str) and 0 < len(keys["auth"]) <= MAX_AUTH)


async def subscribe(subscription: dict, *, plex_account_id: Optional[str], plex_name: Optional[str],
                    discord_id: Optional[str]) -> bool:
    if not _valid_subscription(subscription):
        return False
    key = _sub_key(subscription["endpoint"])
    existing = await kv_get(SUBS_NAMESPACE, key)
    if (isinstance(existing, dict) and not _owns(existing, plex_account_id, discord_id)
            and (existing.get("subscription") or {}).get("keys") != subscription["keys"]):
        # Someone else's browser, and these aren't its keys: whoever sent this only
        # knows the address, so they may not redirect or break that person's alerts.
        # The same browser (same keys) changing hands, a shared tablet, is fine.
        return False
    mine = [(k, rec) for k, rec in await subscriptions_for(plex_account_id=plex_account_id, discord_id=discord_id)
            if k != key]
    oldest = sorted(mine, key=lambda kr: str(kr[1].get("created_at") or ""))[:max(0, len(mine) - MAX_SUBS + 1)]
    if oldest:
        await kv_delete_many(SUBS_NAMESPACE, [k for k, _ in oldest])
    await kv_set(SUBS_NAMESPACE, key, {
        "subscription": {"endpoint": subscription["endpoint"], "keys": {
            "p256dh": subscription["keys"]["p256dh"], "auth": subscription["keys"]["auth"]}},
        "plex_account_id": str(plex_account_id) if plex_account_id else None,
        "plex_name": plex_name,
        "discord_id": str(discord_id) if discord_id else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return True


def _owns(rec: dict, plex_account_id: Optional[str], discord_id: Optional[str]) -> bool:
    """Whether a stored subscription belongs to this member (by account id or Discord id)."""
    return bool((plex_account_id and rec.get("plex_account_id") == str(plex_account_id))
                or (discord_id and rec.get("discord_id") == str(discord_id)))


async def unsubscribe(endpoint: str, *, plex_account_id: Optional[str], discord_id: Optional[str]) -> None:
    """Turn alerts off for one browser, only if it's this member's."""
    if not (isinstance(endpoint, str) and endpoint):
        return
    key = _sub_key(endpoint)
    rec = await kv_get(SUBS_NAMESPACE, key)
    if isinstance(rec, dict) and _owns(rec, plex_account_id, discord_id):
        await kv_delete(SUBS_NAMESPACE, key)


async def subscriptions_for(*, plex_account_id: Optional[str] = None, plex_name: Optional[str] = None,
                            discord_id: Optional[str] = None) -> List[tuple]:
    """(key, record) for every browser this member turned alerts on in.

    By Plex account id or Discord id only. A name (plex_name) is turned into those
    through Plexbie's own row of that name, never compared with the name a browser
    signed up under: that's the subscriber's plex.tv username, which anyone can set
    to someone else's ("Kids")."""
    pid = str(plex_account_id) if plex_account_id else None
    did = str(discord_id) if discord_id else None
    if (plex_name or pid) and not (pid and did):
        row = await _row_for(plex_name, pid)
        if row is not None:
            pid = pid or (str(row.plex_user_id) if row.plex_user_id else None)
            did = did or (str(row.discord_id) if row.discord_id else None)
    out = []
    for key, rec in (await kv_get_all(SUBS_NAMESPACE)).items():
        if not isinstance(rec, dict):
            continue
        if (pid and rec.get("plex_account_id") == pid) or (did and rec.get("discord_id") == did):
            out.append((key, rec))
    return out


# --------------------------------------------------------------- senders
def _push_blocking(subscription: dict, payload: str, private_key: str) -> int:
    """Send one push; returns the push service's HTTP status (201 delivered, 404/410 gone)."""
    from pywebpush import WebPushException, webpush
    try:
        response = webpush(subscription_info=subscription, data=payload, vapid_private_key=private_key,
                           vapid_claims={"sub": vapid_contact()}, ttl=86400, timeout=15)
        return getattr(response, "status_code", 201)
    except WebPushException as e:
        return getattr(e.response, "status_code", 0) if e.response is not None else 0


async def _push(subs: List[tuple], payload: Dict[str, Any]) -> int:
    if not subs:
        return 0
    try:
        keys = await vapid_keys()
    except Exception as e:
        logger.warning(f"Web push unavailable: {type(e).__name__}: {e}")
        return 0
    sent, gone = 0, []
    body = json.dumps(payload)
    for key, rec in subs:
        try:
            status = await run_blocking(_push_blocking, rec["subscription"], body, keys["private"])
        except Exception as e:
            logger.warning(f"Web push failed: {type(e).__name__}: {e}")
            continue
        if status in (404, 410):
            gone.append(key)                           # the browser threw the subscription away
        elif 200 <= status < 300:
            sent += 1
        else:
            logger.warning(f"Web push refused with HTTP {status}")
    if gone:
        await kv_delete_many(SUBS_NAMESPACE, gone)
    return sent


# --------------------------------------------------------------- the app
def _app_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:40]


#: Android notification channels the app makes. "alerts" vibrates in Plexbie's own
#: pattern, "alerts-quiet" doesn't (the app's Vibration setting picks); app versions
#: before them have only "default".
APP_CHANNELS = ("alerts", "alerts-quiet", "default")


async def register_app(token: Any, platform: Any, *, plex_account_id: Optional[str], plex_name: Optional[str],
                       discord_id: Optional[str], session: Optional[str] = None, channel: Any = None,
                       live: Any = None) -> bool:
    """Alerts on in the Plexbie app on one phone, tied to the app sign-in (`session`)
    that asked, so they stop when that sign-in ends.

    A token another member holds moves only for a request from the app itself (with
    `session`): the phone produced the token, so the person signed in on it now is
    whose alerts it should show. Without that, it's left alone."""
    if not (isinstance(token, str) and EXPO_TOKEN.fullmatch(token)) or not (plex_account_id or discord_id):
        return False
    key = _app_key(token)
    existing = await kv_get(APP_NAMESPACE, key)
    if isinstance(existing, dict) and not _owns(existing, plex_account_id, discord_id) and not session:
        return False
    mine = [(k, r) for k, r in await apps_for(plex_account_id=plex_account_id, discord_id=discord_id) if k != key]
    oldest = sorted(mine, key=lambda kr: str(kr[1].get("created_at") or ""))[:max(0, len(mine) - MAX_APPS + 1)]
    if oldest:
        await kv_delete_many(APP_NAMESPACE, [k for k, _ in oldest])
    await kv_set(APP_NAMESPACE, key, {
        "token": token,
        "platform": platform if platform in ("ios", "android") else None,
        "plex_account_id": str(plex_account_id) if plex_account_id else None,
        "plex_name": plex_name,
        "discord_id": str(discord_id) if discord_id else None,
        "session": session,
        "channel": channel if channel in APP_CHANNELS else None,
        # Live progress (Android): its requests' downloads kept in the notification shade.
        "live": live is True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return True


async def forget_app_session(session: str) -> None:
    """An app sign-in ended (sign-out): the phones it registered stop getting alerts now."""
    if not session:
        return
    gone = [k for k, r in (await kv_get_all(APP_NAMESPACE)).items() if isinstance(r, dict) and r.get("session") == session]
    if gone:
        await kv_delete_many(APP_NAMESPACE, gone)


async def unregister_app(token: Any, *, plex_account_id: Optional[str], discord_id: Optional[str]) -> None:
    """Alerts off on one phone (or it signed out), only if that phone is this member's."""
    if not isinstance(token, str) or not EXPO_TOKEN.fullmatch(token):
        return
    key = _app_key(token)
    rec = await kv_get(APP_NAMESPACE, key)
    if isinstance(rec, dict) and _owns(rec, plex_account_id, discord_id):
        await kv_delete(APP_NAMESPACE, key)


async def apps_for(*, plex_account_id: Optional[str] = None, plex_name: Optional[str] = None,
                   discord_id: Optional[str] = None) -> List[tuple]:
    """(key, record) for every phone this member turned app alerts on in. By account
    id or Discord id only, the same rule as subscriptions_for."""
    pid = str(plex_account_id) if plex_account_id else None
    did = str(discord_id) if discord_id else None
    if (plex_name or pid) and not (pid and did):
        row = await _row_for(plex_name, pid)
        if row is not None:
            pid = pid or (str(row.plex_user_id) if row.plex_user_id else None)
            did = did or (str(row.discord_id) if row.discord_id else None)
    return [(k, r) for k, r in (await kv_get_all(APP_NAMESPACE)).items() if isinstance(r, dict) and (
        (pid and r.get("plex_account_id") == pid) or (did and r.get("discord_id") == did))]


def app_push_on() -> bool:
    """Alerts to the phone app go through the Plexbie project's Expo account (its app is
    built against it), so they're opt-in: APP_PUSH=expo. Off, the default, the app tells
    members to use the website's alerts, which every install sends itself."""
    return (os.getenv("APP_PUSH") or "").strip().lower() == "expo"


async def _push_app(apps: List[tuple], payload: Dict[str, Any], live: Optional[Dict[str, Any]] = None) -> int:
    """Send to the app on each phone through Expo's push service. A phone that no
    longer has the app (DeviceNotRegistered) is forgotten. Returns how many accepted."""
    if not app_push_on():
        return 0
    # Only phones whose sign-in still holds; the rest are forgotten.
    stale = [k for k, r in apps if r.get("session") and session_alive is not None and session_alive(r["session"]) is False]
    if stale:
        await kv_delete_many(APP_NAMESPACE, stale)
        apps = [(k, r) for k, r in apps if k not in stale]
    if not apps:
        return 0
    import aiohttp
    if live is not None:
        # Data only: no title, so nothing shows by itself; the app draws (or clears) the
        # live notification from it, in the background if need be.
        messages = [{"to": r["token"], "data": {"plexbie": "live", **live}, "priority": "high"} for _, r in apps]
    else:
        messages = [{
            "to": r["token"], "title": str(payload.get("title") or "Plexbie")[:120], "body": str(payload.get("body") or "")[:400],
            "data": {"url": str(payload.get("url") or "/app")}, "sound": "default", "channelId": r.get("channel") or "default", "priority": "high",
        } for _, r in apps]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            headers = {"Accept": "application/json"}
            # With Expo's "enhanced push security" on, only a sender holding this token is heard.
            if os.getenv("EXPO_ACCESS_TOKEN"):
                headers["Authorization"] = f"Bearer {os.getenv('EXPO_ACCESS_TOKEN')}"
            async with http.post(EXPO_PUSH_URL, json=messages, headers=headers) as resp:
                answer = await resp.json(content_type=None)
                if resp.status >= 400:
                    logger.warning(f"App push refused with HTTP {resp.status}")
                    return 0
    except Exception as e:
        logger.warning(f"App push failed: {type(e).__name__}")
        return 0
    tickets = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(tickets, list):
        return 0
    sent, gone = 0, []
    for (key, _), ticket in zip(apps, tickets):
        if not isinstance(ticket, dict):
            continue
        if ticket.get("status") == "ok":
            sent += 1
        elif (ticket.get("details") or {}).get("error") == "DeviceNotRegistered":
            gone.append(key)
        else:
            logger.warning(f"App push not accepted: {(ticket.get('details') or {}).get('error') or 'unknown'}")
    if gone:
        await kv_delete_many(APP_NAMESPACE, gone)
    return sent


async def push_app_live(apps: List[tuple], data: Dict[str, Any]) -> int:
    """A live-progress update ({"op": "show" | "end", "id", ...}) to these phones. Best-effort."""
    try:
        return await _push_app(apps, {}, live=data)
    except Exception as e:
        logger.info(f"Live progress push failed: {type(e).__name__}")
        return 0


async def push_app_to_discord(discord_id: Any, *, title: str, body: str, url: str = "/app") -> int:
    """The app's copy of a Discord DM, for someone who turned app alerts on. Best-effort."""
    try:
        apps = await apps_for(discord_id=str(discord_id)) if discord_id else []
        return await _push_app(apps, {"title": title, "body": body, "url": url})
    except Exception as e:
        logger.warning(f"App push for a DM failed: {type(e).__name__}")
        return 0


def email_configured(config) -> bool:
    return bool(getattr(config, "smtp_host", None) and getattr(config, "smtp_username", None)
                and getattr(config, "smtp_password", None))


def _email_blocking(config, to: str, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"] = config.smtp_from or f"Plexbie <{config.smtp_username}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    with smtplib.SMTP(config.smtp_host, int(config.smtp_port or 587), timeout=20) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(config.smtp_username, config.smtp_password)
        smtp.send_message(msg)


async def _email(config, to: Optional[str], subject: str, text: str) -> bool:
    if not (to and email_configured(config)):
        return False
    try:
        await run_blocking(_email_blocking, config, to, subject, text)
        return True
    except Exception as e:
        logger.warning(f"Email to a member failed: {type(e).__name__}: {e}")
        return False


async def send_test(*, plex_account_id=None, plex_name=None, discord_id=None) -> int:
    """A test alert to every browser this person turned alerts on in. Returns how many got it."""
    subs = await subscriptions_for(plex_account_id=plex_account_id, plex_name=plex_name, discord_id=discord_id)
    apps = await apps_for(plex_account_id=plex_account_id, plex_name=plex_name, discord_id=discord_id)
    payload = {"title": "Alerts are on", "body": "This is how Plexbie will tell you when a request arrives "
               "or your access needs attention.", "url": "/app", "tag": "test"}
    return await _push(subs, payload) + await _push_app(apps, payload)


async def push_app_to_everyone(*, title: str, body: str, url: str = "/app") -> int:
    """One alert to every phone with app alerts on (a new app version). Returns how many got it."""
    try:
        apps = [(k, r) for k, r in (await kv_get_all(APP_NAMESPACE)).items() if isinstance(r, dict) and r.get("token")]
        return await _push_app(apps, {"title": title, "body": body, "url": url})
    except Exception as e:
        logger.warning(f"App push to everyone failed: {type(e).__name__}")
        return 0


async def push_to_admins(*, discord_ids: set, plex_account_ids: set, title: str, body: str, url: str, tag: str) -> int:
    """A phone/browser alert to every admin who turned alerts on. Returns how many got it."""
    accounts = {str(a) for a in plex_account_ids if a}
    ids = {str(d) for d in discord_ids if d}

    def theirs(r) -> bool:
        return isinstance(r, dict) and bool((r.get("discord_id") and r["discord_id"] in ids)
                                            or (r.get("plex_account_id") and r["plex_account_id"] in accounts))
    subs = [(k, r) for k, r in (await kv_get_all(SUBS_NAMESPACE)).items() if theirs(r)]
    apps = [(k, r) for k, r in (await kv_get_all(APP_NAMESPACE)).items() if theirs(r)]
    payload = {"title": title, "body": body, "url": url, "tag": tag}
    return await _push(subs, payload) + await _push_app(apps, payload)


_admin_tasks: set = set()


def alert_admins_soon(bot, config, *, title: str, body: str, url: str, tag: str) -> None:
    """A phone/browser alert to the admins about something that just reached the admin
    channel (a request, a join request), in the background: the caller never waits on
    Expo or web push, and it never raises."""
    async def go():
        try:
            from core.discord_lookup import home_guild
            from database.kv_store import kv_get as get
            from portal.auth import OWNER_ID
            ids = {str(config.bot_owner_id)} if getattr(config, "bot_owner_id", None) else set()
            guild = home_guild(bot, config)
            for m in (guild.members if guild else []):
                roles = {r.id for r in m.roles}
                if m.guild_permissions.administrator or (config.admin_role_id and config.admin_role_id in roles):
                    ids.add(str(m.id))
            owner = await get(*OWNER_ID)
            await push_to_admins(discord_ids=ids, plex_account_ids={owner} if owner else set(),
                                 title=title, body=body, url=url, tag=tag)
        except Exception as e:
            logger.info(f"Couldn't alert the admins ({tag}): {type(e).__name__}")
    task = asyncio.create_task(go())
    _admin_tasks.add(task)
    task.add_done_callback(_admin_tasks.discard)


# ------------------------------------------------------------------ main
# Alerts still name pages as /app/... (the website forwards them to the root, where the
# household's pages moved): older versions of the phone app only know those addresses.
async def notify_member(services, *, title: str, body: str, url: str = "/app", tag: Optional[str] = None,
                        plex_account_id: Optional[str] = None, plex_name: Optional[str] = None,
                        discord_id: Optional[str] = None, email: Optional[str] = None, context: str = "",
                        sent_by: Optional[str] = None) -> str:
    """Tell a member something without Discord. Returns "push", "email" or "none".
    `discord_id` also finds the alerts they turned on while signed in with Discord."""
    from core.message_log import record
    how = "none"
    try:
        subs = await subscriptions_for(plex_account_id=plex_account_id, plex_name=plex_name, discord_id=discord_id)
        apps = await apps_for(plex_account_id=plex_account_id, plex_name=plex_name, discord_id=discord_id)
        payload = {"title": title, "body": body, "url": url, "tag": tag or context or None}
        if await _push(subs, payload) + await _push_app(apps, payload):
            logger.info(f"Notified {plex_name or plex_account_id} by web push ({context})")
            how = "push"
        else:
            public = (getattr(services.config, "web_public_url", None) or "").rstrip("/")
            text = f"{body}\n\n{public}{url}\n\n-- Plexbie" if public else f"{body}\n\n-- Plexbie"
            if await _email(services.config, email or await email_for(plex_name, plex_account_id), title, text):
                logger.info(f"Notified {plex_name or plex_account_id} by email ({context})")
                how = "email"
            else:
                logger.info(f"Could not reach {plex_name or plex_account_id} without Discord ({context}): no alerts on, no email route")
    except Exception as e:
        logger.warning(f"notify_member failed ({context}): {type(e).__name__}: {e}")
    await record(channel={"push": "web", "email": "email"}.get(how, "none"), delivered=how != "none",
                 error=None if how != "none" else "No phone alerts turned on and no email to send to",
                 plex_name=plex_name, plex_account_id=plex_account_id, title=title, text=body, context=context, sent_by=sent_by)
    return how
