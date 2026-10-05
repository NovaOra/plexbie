# path: portal/mobile.py
"""Signing in from the Plexbie app (iOS and Android).

The app never sees a Discord or Plex token, and never a cookie:

  1. The app opens GET /auth/mobile/start?via=discord|plex&challenge=…&state=…&redirect=…
     in the phone's own browser sheet. `challenge` is the SHA-256 of a secret only
     the app holds (PKCE, RFC 7636); `redirect` must be the app's own address.
  2. The usual Discord or Plex sign-in runs in that sheet, tied to it by the signed
     flow cookie like any browser sign-in.
  3. Where a browser sign-in would set the session cookie, the sheet is instead sent
     to the app's address with ?code=…&state=…, a code that works once, for a
     minute, and only together with the app's secret. That address is the app's own,
     com.plexbie.app:/auth. (App versions before 1.0.0 on Android could instead ask for
     https://<this site>/auth/mobile/return, an App Link; RETURN_PATH still forwards
     that to the app's scheme, so a phone that hasn't updated can still sign in.)
     An invite link opened in the app adds invite=<its code>: Plex sign-in then
     accepts the invite, as on the website, and the app hears how it went.
  4. POST /auth/mobile/token {code, verifier} trades it for an app session token.

App sessions are kept here, by the SHA-256 of their token (a copy of the store gets
nobody in). Each has a device name, a last use, an idle and an absolute expiry, and
is ended on sign-out. The token is accepted only as "Authorization: Bearer", together
with the X-Plexbie header, never as a cookie.
"""
import asyncio
import base64
import hashlib
import re
import secrets
import time
from typing import Dict, Optional, Set, Tuple
from urllib.parse import urlencode

from aiohttp import web

from core.logging import get_logger
from core.security import secure_equals
from database.kv_store import kv_get, kv_set
from portal.ratelimit import Limiter

logger = get_logger(__name__)

#: What the app checks to tell a server that supports it from an older one.
API_VERSION = "1"
#: The only place a sign-in code is ever sent: the app's own address (RFC 8252 style).
APP_REDIRECT = "com.plexbie.app:/auth"
RETURN_PATH = "/auth/mobile/return"
REDIRECTS = {APP_REDIRECT}          # plus https://<this site>/auth/mobile/return (allow_return)
FORWARDED = ("code", "state", "error", "invite")


def allow_return(public_url: str) -> None:
    """The site's https return address is allowed too (for app versions before 1.0.0,
    which returned through an Android App Link), once the public address is https."""
    if (public_url or "").startswith("https://"):
        REDIRECTS.add(public_url.rstrip("/") + RETURN_PATH)


def forward_to_app(query) -> web.HTTPFound:
    """The https return address opened in a browser (the phone didn't verify the link):
    on to the app's own scheme with the same answer, nothing else."""
    params = {k: str(query[k])[:256] for k in FORWARDED if query.get(k)}
    return web.HTTPFound(f"{APP_REDIRECT}?{urlencode(params)}", headers=PRIVATE)

CODE_SECONDS = 60
APP_IDLE_DAYS = 30                  # unused this long, an app sign-in ends
APP_MAX_DAYS = 180                  # and after this long whatever happens
TOUCH_SECONDS = 3600                # last use is saved at most hourly per session
MAX_APP_SESSIONS = 5000
PER_PERSON = 10                     # app sign-ins one account may hold; its oldest goes first
START_TRIES = (20, 600)             # app sign-ins started per address per 10 minutes
TOKEN_TRIES = (20, 600)             # code exchanges per address per 10 minutes
APP_SESSIONS = ("web_sessions", "app")
TOKEN_PREFIX = "pxa_"

CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")          # base64url SHA-256, no padding
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")     # RFC 7636 section 4.1
STATE = re.compile(r"[A-Za-z0-9._~-]{16,128}")
CODE = re.compile(r"[A-Za-z0-9_-]{43}")
TOKEN = re.compile(TOKEN_PREFIX + r"[A-Za-z0-9_-]{43}")

#: Headers for every step of an app sign-in: nothing cached, and the code-bearing
#: addresses never passed on as a referrer to another site. "same-origin", not
#: "no-referrer": under no-referrer a browser posts a form with "Origin: null", so the
#: confirm page's own Sign in button would look like it came from another site.
PRIVATE = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "same-origin"}

#: The one answer for a code that's wrong, used, expired or paired with the wrong secret.
BAD_CODE = "That sign-in didn't work. Start again from the app."
#: The sign-in store couldn't be read: not the same as "signed out".
UNAVAILABLE = "Plexbie can't check sign-ins right now. Try again in a moment."


class Unavailable(Exception):
    """The app sign-in store couldn't be read or written."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def s256(verifier: str) -> str:
    """The PKCE challenge for a verifier: base64url(SHA-256), unpadded."""
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def start_params(query) -> Optional[dict]:
    """The app's PKCE challenge, state and redirect, or None if any is missing or not
    allowed. Only S256: a "plain" challenge (or none) would make a caught code enough."""
    redirect, challenge, state = query.get("redirect"), query.get("challenge"), query.get("state")
    if redirect not in REDIRECTS:
        return None
    if query.get("challenge_method", "S256") != "S256":
        return None
    if not (challenge and CHALLENGE.fullmatch(challenge) and state and STATE.fullmatch(state)):
        return None
    return {"challenge": challenge, "state": state, "redirect": redirect}


def valid(mobile) -> bool:
    """A flow cookie's app details, re-checked before anything is sent there."""
    return isinstance(mobile, dict) and start_params(mobile) is not None


def back_to_app(mobile: dict, **params: str) -> web.HTTPFound:
    """Send the sign-in sheet back to the app. Only ever to an allowed address."""
    if not valid(mobile):
        raise ValueError("not an app sign-in")
    location = f"{mobile['redirect']}?{urlencode({**params, 'state': mobile['state']})}"
    return web.HTTPFound(location, headers=PRIVATE)


def bearer(request: Optional[web.Request]) -> Optional[str]:
    """The token from "Authorization: Bearer …" ("" if that header is malformed), or
    None when the request carries no Bearer header at all. Other schemes are not ours."""
    if request is None:
        return None
    header = request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token if TOKEN.fullmatch(token) else ""


def _device(value) -> str:
    text = "".join(c for c in str(value or "") if c.isprintable()).strip()[:60]
    return text or "Plexbie app"


class MobileSessions:
    def __init__(self):
        self.start_limit = Limiter(*START_TRIES)
        self.token_limit = Limiter(*TOKEN_TRIES)
        self._codes: Dict[str, dict] = {}                         # hash(code) -> what it stands for
        self._spent: Dict[str, Tuple[float, Optional[str]]] = {}   # hash(code) -> (until, session it made)
        self._sessions: Optional[Dict[str, dict]] = None          # hash(token) -> session, once loaded
        self._load_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._tasks: Set[asyncio.Task] = set()

    # -------------------------------------------------------------- codes
    def mint_code(self, mobile: dict, via: str, session: dict) -> str:
        """A one-time code for this sign-in: 256 random bits, kept only as a hash,
        bound to the app's challenge, state, redirect and the person who signed in."""
        now = time.time()
        self._prune(now)
        code = secrets.token_urlsafe(32)
        self._codes[_hash(code)] = {
            "challenge": mobile["challenge"], "state": mobile["state"], "redirect": mobile["redirect"],
            "via": via, "session": dict(session), "exp": now + CODE_SECONDS,
        }
        return code

    def _prune(self, now: float) -> None:
        for key in [k for k, c in self._codes.items() if c["exp"] < now]:
            self._spent[key] = (now + CODE_SECONDS, None)
            del self._codes[key]
        for key in [k for k, (until, _) in self._spent.items() if until < now]:
            del self._spent[key]

    async def exchange(self, code, verifier, device=None) -> Optional[dict]:
        """{token, expiresAt} for a good code and the matching verifier, else None.
        Raises Unavailable, with the code untouched, when sign-ins can't be stored.

        A code is tried once: it's taken out before anything is checked (and before
        any await after the store is loaded, so two racing requests can't both have
        it). A code used again within its lifetime ends the session the first use
        made: one of the two was not the app (which never retries an exchange)."""
        if not (isinstance(code, str) and CODE.fullmatch(code)):
            return None
        await self.load()
        if not self.ready:
            raise Unavailable()
        key, now = _hash(code), time.time()
        self._prune(now)
        entry = self._codes.pop(key, None)
        if entry is None:
            spent = self._spent.get(key)
            if spent and spent[1] and spent[0] >= now:
                self._spent[key] = (spent[0], None)
                logger.warning("An app sign-in code was used twice; the session it made is ended")
                await self.revoke(spent[1])
            return None
        self._spent[key] = (now + CODE_SECONDS, None)
        if entry["exp"] < now or not (isinstance(verifier, str) and VERIFIER.fullmatch(verifier)):
            return None
        if not secure_equals(s256(verifier), entry["challenge"]):
            return None
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_key = _hash(token)
        expires = int(now) + APP_MAX_DAYS * 86400
        session = entry["session"]
        record = {
            "via": entry["via"], "sid": secrets.token_urlsafe(12), "id": str(session["id"]),
            "name": session.get("name"), "email": session.get("email"), "avatar": session.get("avatar"),
            "device": _device(device), "created": int(now), "last": int(now), "exp": expires,
        }
        self._sessions[token_key] = record
        self._spent[key] = (now + CODE_SECONDS, token_key)
        self._cap(now, record)
        await self._save()
        logger.info(f"App sign-in via {entry['via'].capitalize()}: {record['name']}")
        return {"token": token, "expiresAt": expires}

    # ----------------------------------------------------------- sessions
    @property
    def ready(self) -> bool:
        """The stored sign-ins have been read, so "not found" really means signed out."""
        return self._sessions is not None

    async def load(self) -> None:
        """Read the stored sign-ins once. Until that works nothing is minted, saved or
        refused as signed out, so a database hiccup can't wipe or end anyone's sign-in."""
        if self._sessions is not None:
            return
        async with self._load_lock:
            if self._sessions is not None:
                return
            try:
                saved = await kv_get(*APP_SESSIONS, {}) or {}
            except Exception as e:                  # no database (yet): try again next time
                logger.info(f"App sign-ins unavailable ({type(e).__name__})")
                return
            now = time.time()
            self._sessions = {k: v for k, v in (saved if isinstance(saved, dict) else {}).items()
                              if isinstance(v, dict) and self._alive(v, now)}

    @staticmethod
    def _alive(record: dict, now: float) -> bool:
        return record.get("exp", 0) > now and now - record.get("last", 0) < APP_IDLE_DAYS * 86400

    def lookup(self, token: str) -> Optional[dict]:
        """The session behind a token, or None. Counts as a use of it."""
        if not token or self._sessions is None:
            return None
        key, now = _hash(token), time.time()
        record = self._sessions.get(key)
        if record is None:
            return None
        if not self._alive(record, now):
            self._sessions.pop(key, None)
            self._later(self._save())
            return None
        if now - record.get("last", 0) > TOUCH_SECONDS:
            record["last"] = int(now)
            self._later(self._save())
        return {**record, "app": key}

    def is_alive(self, key: str) -> Optional[bool]:
        """Whether the app sign-in with this key still holds; None until the store is read."""
        if self._sessions is None:
            return None
        record = self._sessions.get(key)
        return bool(record) and self._alive(record, time.time())

    async def revoke(self, key: str) -> bool:
        """End one app sign-in. False if that couldn't be stored (it may come back)."""
        await self.load()
        if self._sessions is None:
            return False
        if self._sessions.pop(key, None) is None:
            return True
        return await self._save()

    def _cap(self, now: float, newest: dict) -> None:
        """Expired sign-ins go; one account keeps at most PER_PERSON (its own oldest
        go first), so nobody signing in over and over can push out anyone else's.
        The overall cap is a last resort."""
        sessions = self._sessions or {}
        for key in [k for k, r in sessions.items() if not self._alive(r, now)]:
            del sessions[key]
        mine = sorted((k for k, r in sessions.items()
                       if r.get("via") == newest["via"] and r.get("id") == newest["id"]),
                      key=lambda k: sessions[k].get("last", 0))
        for key in mine[:max(0, len(mine) - PER_PERSON)]:
            del sessions[key]
        while len(sessions) > MAX_APP_SESSIONS:
            del sessions[min(sessions, key=lambda k: sessions[k].get("last", 0))]

    async def _save(self) -> bool:
        async with self._save_lock:
            if self._sessions is None:
                return False
            try:
                await kv_set(*APP_SESSIONS, dict(self._sessions))
                return True
            except Exception as e:
                logger.warning(f"Could not save app sign-ins: {type(e).__name__}")
                return False

    def _later(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:                         # no loop (a plain call in a test)
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
