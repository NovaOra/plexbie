# path: portal/auth.py
"""Signing in to plexbie.com: "Log in with Discord" and "Sign in with Plex".

Sessions are a signed cookie (HMAC-SHA256 with WEB_SESSION_SECRET) holding who
signed in and how: never a Discord or Plex token, which are used once during
sign-in and dropped. What someone may do is worked out on each request from the
bot's live view of them (Discord roles, the plex.tv share list), so taking a role
away takes effect without anyone having to sign out.

Identity rules (PRODUCT.md):
  member  Discord: holds PLEX_MEMBER_ROLE_ID.  Plex: on the server's plex.tv share
          list, or is the owner.
  admin   Discord: holds ADMIN_ROLE_ID, has Administrator, or is BOT_OWNER_ID.
          Plex: is the server owner, or is linked to a Discord admin.
"""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from typing import Any, Dict, Optional, Set
from urllib.parse import quote, urlencode, urlsplit

from aiohttp import web

from core.logging import get_logger
from core.security import secure_equals
from database.kv_store import kv_get, kv_set
from database.session import get_session
from portal.cache import TTLCache
from portal.mobile import allow_return as mobile_allow_return, forward_to_app
from portal.mobile import (BAD_CODE, PRIVATE, UNAVAILABLE, MobileSessions, Unavailable, back_to_app, bearer,
                           start_params, valid as app_flow)
from portal.ratelimit import Limiter
from core.discord_lookup import home_guild

logger = get_logger(__name__)

SESSION_COOKIE = "plexbie_session"
FLOW_COOKIE = "plexbie_flow"
START_COOKIE = "plexbie_plex"     # the device id a Plex sign-in window made its PIN with
CONFIRM_COOKIE = "plexbie_app_signin"   # an app sign-in waiting for "Sign in to the app as …?"
CONFIRM_SECONDS = 300
CONFIRM_PATH = "/auth/mobile/confirm"
SESSION_DAYS = 30
FLOW_SECONDS = 600
PLEX_PRODUCT = "Plexbie"
WEB_JOINS_NAMESPACE = "web_plex_joins"
INVITE_COOKIE = "plexbie_invite"
#: A plex.tv picture's address (the only ones the site fetches for Plex avatars).
PLEX_THUMB = re.compile(r"^https://plex\.tv/users/[0-9a-f]{8,32}/avatar(\?c=\d+)?$")
#: A Discord avatar hash (animated ones start a_).
AVATAR_KEY = re.compile(r"^(a_)?[0-9a-f]{32}$")
INVITE_SECONDS = 3600
INVITE_TRIES = (20, 3600)        # invite links opened per address per hour
PIN_TRIES = (20, 600)            # Plex sign-ins started per address per 10 minutes
CHECK_TRIES = (240, 600)         # sign-in polls per address per 10 minutes (one every 1.5 s)
#: Discord sign-ins finished, per address and for the whole site, per 10 minutes.
#: Each makes a call to Discord from the bot's own address, and Discord bans an
#: address that sends it too many refused calls, which would take the bot down too.
DISCORD_TRIES = (10, 600)
DISCORD_ALL_TRIES = (120, 600)
#: Sessions signed out on this server, kept until they'd have expired anyway.
REVOKED = ("web_sessions", "revoked")
#: The Plex owner's account id, kept from plex.tv's last answer.
OWNER_ID = ("web_sessions", "plex_owner_id")
#: Who the server is shared with, as plex.tv last said, and how long that answer
#: stands in while plex.tv can't be reached.
SHARE_LIST = ("web_sessions", "plex_share_list")
SHARE_LIST_DAYS = 3


# ------------------------------------------------------------- signed values
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def unsign(secret: str, token: Optional[str]) -> Optional[dict]:
    """The payload if the signature is good and it hasn't expired, else None."""
    if not token or "." not in token or not secret:
        return None
    body, mac = token.rsplit(".", 1)
    expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not secure_equals(mac, expected):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def safe_next(value: Optional[str]) -> str:
    """Only same-site paths, so the login redirect can't be bent elsewhere.

    Control characters are refused outright: browsers drop tabs and line breaks
    from a URL, so "/<tab>/evil.example" would become "//evil.example"."""
    if not value or len(value) > 300 or not value.startswith("/") or value.startswith("//") \
            or "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value



def _app_error(outcome: str) -> str:
    """What the app is told about a sign-in that didn't finish: only whether the
    person said no, was asked to wait, or it just didn't work."""
    return {"cancelled": "denied", "busy": "busy"}.get(outcome, "failed")


def _page(body: str, refresh: str = "") -> str:
    """A bare page for the sign-in window, in the site's colours. `refresh` moves
    it on after a moment without script or a tap (see Auth.plex_go)."""
    head = f"<meta http-equiv=refresh content='1.5;url={html.escape(refresh, quote=True)}'>" if refresh else ""
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>" + head +
            "<title>Plexbie</title><body style='background:#10172b;color:#f7f1f6;font:17px/1.5 system-ui,sans-serif;"
            "text-align:center;padding:48px 20px'>" + body)


#: The app's "sign in as …?" page: one form, back to this site (and on to the app), never framed.
CONFIRM_CSP = ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self' com.plexbie.app:; "
               "base-uri 'none'; frame-ancestors 'none'")

#: The sign-in window's own pages: inline styles, and window.close() on the last one.
WINDOW_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
              "connect-src 'self' https://plex.tv; base-uri 'none'; form-action 'none'")


def pin_page(params: dict) -> str:
    """The sign-in window: make a plex.tv PIN from this browser, hand it to
    params["post"], then go to the plex.tv address that answers with. On a
    failure it shows the server's reason, or, with params["back"] (a whole-page
    sign-in, no window to close), goes to the page listed for that HTTP status."""
    return _page(
        "<p id=m>Opening Plex…</p>"
        "<script>(async function(){var p=" + json.dumps(params).replace("</", "<\\/") + ",d={};"
        "try{var r=await fetch('https://plex.tv/api/v2/pins?strong=true',{method:'POST',headers:{"
        "'Accept':'application/json','X-Plex-Product':p.product,'X-Plex-Client-Identifier':p.client}});"
        "if(!r.ok)throw 0;var pin=await r.json();"
        "var s=await fetch(p.post,{method:'POST',credentials:'same-origin',headers:Object.assign({'Content-Type':'application/json'},p.headers||{}),"
        "body:JSON.stringify({id:pin.id,code:pin.code,next:p.next,invite:p.invite})});"
        "d=await s.json()||{};if(!d.url)throw s.status;location.replace(d.url);"
        "}catch(e){if(p.back)return location.replace(p.back[e]||p.back[0]);"
        "document.getElementById('m').textContent=d.error||'Couldn\\u2019t reach Plex. Close this window and try again.';}})();</script>")


class Auth:
    def __init__(self, bot, services, cache: TTLCache):
        self.bot = bot
        self.services = services
        self.config = services.config
        self.cache = cache
        self.secret = self.config.web_session_secret or ""
        self.public = (self.config.web_public_url or "").rstrip("/")
        mobile_allow_return(self.public)
        for old in getattr(self.config, "web_aliases", None) or []:
            mobile_allow_return(old)        # phones still signing in through an old address
        self.secure = self.public.startswith("https://")
        # Each Plex sign-in makes its own device id (kept in that browser's cookie), so
        # a PIN can only be finished by the browser that started it, and nothing
        # public is derived from the session secret.
        self.invites = None                      # portal.invites.Invites, set by the server
        self._invite_limit = Limiter(*INVITE_TRIES)
        self._pin_limit = Limiter(*PIN_TRIES)
        self._check_limit = Limiter(*CHECK_TRIES)
        self._discord_limit = Limiter(*DISCORD_TRIES)
        self._discord_all = Limiter(*DISCORD_ALL_TRIES)
        self._discord_states: Dict[str, float] = {}   # sign-in states already used, until they'd expire
        self._discord_pause = 0.0                     # Discord said slow down: no calls until then
        self._revoked: Optional[Dict[str, int]] = None
        self._revoke_lock = asyncio.Lock()       # one sign-out saved at a time, none lost
        self._owner_id: Optional[str] = None
        self._tasks: Set[asyncio.Task] = set()
        self._pin_locks: Dict[int, asyncio.Lock] = {}
        self._pin_users: Dict[int, int] = {}     # finishers holding or waiting on each PIN's lock
        self._pins_done: Dict[int, str] = {}
        self.mobile = MobileSessions()           # sign-ins from the Plexbie app (portal/mobile.py)
        from core import notify
        notify.session_alive = self.mobile.is_alive  # app alerts stop when their sign-in ends

    # ---------------------------------------------------------- cookies
    @staticmethod
    def base_url(request: web.Request) -> str:
        """This site's own address as the visitor reached it, trusting a proxy's X-Forwarded-* headers.

        Reached by the public name (WEB_PUBLIC_URL's host), the public address wins:
        behind Cloudflare and Nginx Proxy Manager the last hop is plain http, and NPM
        overwrites X-Forwarded-Proto with it, so the headers would say http for a
        site visitors reach over https.
        """
        import os
        from urllib.parse import urlparse
        from core.config import public_url
        from portal.ratelimit import trusted, visitor_scheme
        # Forwarding headers only from a trusted proxy (cloudflared, NPM); from
        # anyone else they'd only be the visitor's own say-so. Cloudflare's CF-Visitor
        # wins over X-Forwarded-Proto, which a proxy behind it may rewrite to http.
        via_proxy = trusted(request.remote)
        proto = (visitor_scheme(request) or (request.headers.get("X-Forwarded-Proto") if via_proxy else None)
                 or request.scheme).split(",")[0].strip()
        host = ((request.headers.get("X-Forwarded-Host") if via_proxy else None) or request.host).split(",")[0].strip()
        public = public_url(os.getenv("WEB_PUBLIC_URL", ""))
        if public and urlparse(public).netloc.lower() == host.lower():
            return public
        return f"{proto}://{host}"

    def _set(self, response: web.StreamResponse, name: str, payload: dict, seconds: int,
             request: Optional[web.Request] = None) -> None:
        # "typ" names the cookie it was made for: one signing key signs sessions,
        # sign-in flows and invites, and none may be passed off as another.
        payload = {**payload, "typ": name, "exp": int(time.time()) + seconds}
        # Secure on https only: an https public name must not stop sign-in over the
        # plain LAN address, where a Secure cookie would be dropped.
        secure = self.base_url(request).startswith("https://") if request is not None else self.secure
        response.set_cookie(name, sign(self.secret, payload), max_age=seconds, httponly=True,
                            secure=secure, samesite="Lax", path="/")

    def _clear(self, response: web.StreamResponse, name: str) -> None:
        response.del_cookie(name, path="/")

    def _cookie(self, request: web.Request, name: str) -> Optional[dict]:
        payload = unsign(self.secret, request.cookies.get(name))
        if not payload:
            return None
        # Only what was made as this cookie: the same key signs other things (download
        # links, iPhone sources), and none of them may stand in for a sign-in.
        if payload.get("typ") != name:
            return None
        return payload

    def session(self, request: web.Request) -> Optional[dict]:
        token = bearer(request)
        if token is not None:
            # The app: its token and nothing else (a cookie alongside it is ignored),
            # and only with the header other sites can't make a browser send.
            if not token or request.headers.get("X-Plexbie") != "1":
                return None
            return self.mobile.lookup(token)
        s = self._cookie(request, SESSION_COOKIE)
        if not s or not s.get("id") or (s.get("sid") and s["sid"] in (self._revoked or {})):
            return None
        return s

    async def load_revoked(self) -> None:
        """Signed-out sessions, so a copied cookie stops working on logout."""
        if self._revoked is None:
            now = int(time.time())
            try:
                saved = await kv_get(*REVOKED, {}) or {}
            except Exception as e:          # no database yet: nothing has been revoked
                logger.info(f"Signed-out sessions unavailable ({type(e).__name__})")
                return
            self._revoked = {sid: exp for sid, exp in saved.items() if isinstance(exp, int) and exp > now}

    # ---------------------------------------------------- discord sign-in
    def discord_ready(self) -> bool:
        return bool(self.config.discord_client_id and self.config.discord_client_secret and self.config.discord_callback_url)

    async def discord_login(self, request: web.Request) -> web.Response:
        if not self.discord_ready():
            raise web.HTTPFound("/?login=unavailable")
        raise self._to_discord(request, {"next": safe_next(request.query.get("next"))})

    def _to_discord(self, request: web.Request, flow: dict) -> web.HTTPFound:
        state = secrets.token_urlsafe(24)
        url = "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": self.config.discord_client_id,
            "redirect_uri": self.config.discord_callback_url,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "prompt": "none",
        })
        response = web.HTTPFound(url)
        self._set(response, FLOW_COOKIE, {**flow, "via": "discord", "state": state}, FLOW_SECONDS, request)
        return response

    def _discord_end(self, outcome: str, flow: Optional[dict] = None) -> web.HTTPFound:
        mobile = (flow or {}).get("mobile")
        if app_flow(mobile):
            response = back_to_app(mobile, error=_app_error(outcome))
        else:
            response = web.HTTPFound(f"/?login={outcome}")
        self._clear(response, FLOW_COOKIE)
        return response

    def _spend_state(self, state: str) -> bool:
        """True the first time a sign-in's state comes back; a replay of it is refused."""
        now = time.monotonic()
        if len(self._discord_states) > 2000:
            self._discord_states = {k: v for k, v in self._discord_states.items() if v > now}
        if self._discord_states.get(state, 0) > now:
            return False
        self._discord_states[state] = now + FLOW_SECONDS
        return True

    async def discord_callback(self, request: web.Request) -> web.Response:
        flow = self._cookie(request, FLOW_COOKIE) or {}
        state = str(flow.get("state", ""))
        if flow.get("via") != "discord" or not secure_equals(state, request.query.get("state", "-")):
            raise self._discord_end("expired", flow)
        code = request.query.get("code")
        if not code:
            raise self._discord_end("cancelled", flow)
        if not self._spend_state(state):
            raise self._discord_end("expired", flow)
        if (time.monotonic() < self._discord_pause or self._discord_limit.over(request)
                or self._discord_all.hit("site")):
            raise self._discord_end("busy", flow)
        try:
            token = (await self._fetch_json("post", "https://discord.com/api/oauth2/token", data={
                "client_id": self.config.discord_client_id,
                "client_secret": self.config.discord_client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.discord_callback_url,
            }))["access_token"]
            me = await self._fetch_json("get", "https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {token}"})
        except Exception as e:
            if getattr(e, "status", None) == 429:
                try:
                    wait = float((getattr(e, "headers", None) or {}).get("Retry-After") or 60)
                except ValueError:
                    wait = 60.0
                self._discord_pause = time.monotonic() + min(max(wait, 5.0), 3600.0)
                logger.warning(f"Discord asked Plexbie to slow down; Discord sign-in paused for {wait:.0f}s")
            logger.warning(f"Discord sign-in failed: {type(e).__name__}")
            raise self._discord_end("failed", flow)
        person = {
            "via": "discord",
            "sid": secrets.token_urlsafe(12),
            "id": str(me["id"]),
            "name": me.get("global_name") or me.get("username") or "Discord user",
            "avatar": me.get("avatar"),
        }
        if app_flow(flow.get("mobile")):
            # From the app: asked to confirm, then a one-time code back to it. No session cookie.
            response = web.HTTPFound(CONFIRM_PATH, headers=PRIVATE)
            self._clear(response, FLOW_COOKIE)
            self._set(response, CONFIRM_COOKIE, {"mobile": flow["mobile"], "person": person}, CONFIRM_SECONDS, request)
            raise response
        response = web.HTTPFound(safe_next(flow.get("next")))
        self._clear(response, FLOW_COOKIE)
        self._set(response, SESSION_COOKIE, person, SESSION_DAYS * 86400, request)
        logger.info(f"Web sign-in via Discord: {me.get('username')}")
        raise response

    # ------------------------------------------------------- plex sign-in
    @staticmethod
    def _plex_headers(client: str, token: Optional[str] = None) -> dict:
        h = {"Accept": "application/json", "X-Plex-Product": PLEX_PRODUCT, "X-Plex-Client-Identifier": client}
        if token:
            h["X-Plex-Token"] = token
        return h

    def _plex_auth_url(self, request: web.Request, code: str, popup: bool, client: str) -> str:
        # Back to the address they're on: the flow cookie lives there, and the public
        # name may not reach this install (yet). The address must be /auth/#!? -
        # without the "#!" Plex just opens its home page.
        forward = f"{self.base_url(request)}/auth/plex/callback" + ("?popup=1" if popup else "")
        return "https://app.plex.tv/auth/#!?" + urlencode({
            "clientID": client,
            "code": code,
            "forwardUrl": forward,
            "context[device][product]": PLEX_PRODUCT,
        }, quote_via=quote)

    def _plex_flow(self, request: web.Request, pin_id: int, code: str, client: str,
                   next_: Optional[str], invite: bool) -> dict:
        # The PIN's code and device id go with it: plex.tv's answer must match both,
        # so knowing (or guessing) someone else's PIN number gets nobody anywhere.
        flow = {"via": "plex", "pin": pin_id, "code": code, "client": client, "next": safe_next(next_)}
        held = self._cookie(request, INVITE_COOKIE) or {}
        if invite and held.get("key"):
            flow["invite"] = held["key"]
        return flow

    async def plex_login(self, request: web.Request) -> web.Response:
        """Whole-page sign-in, where the small window can't open: the same first page
        as plex_go, in this tab, and back through plex_callback rather than polling.
        The PIN is made by the browser here too (see plex_go); this server never makes one."""
        return self._pin_window(request, safe_next(request.query.get("next")), bool(request.query.get("invite")), page=True)

    async def plex_go(self, request: web.Request) -> web.Response:
        """The sign-in window's first page. It asks plex.tv for a PIN itself, hands
        the PIN to plex_pin, and moves on to plex.tv, while the site polls plex_check.

        Why the browser makes the PIN: Plex refuses a sign-in ("Security Alert,
        another device...") when the PIN was made from a different internet
        address than the one approving it, e.g. this server at home and a phone
        on mobile data. Why this page at all: on phones a tap on a plex.tv link
        opens the Plex app, which never comes back; a page that moves on by
        itself, after the tap, stays in the browser. Plex's forwardUrl redirect
        isn't relied on either: once the PIN is approved, polling notices.

        The address check also stops a plex.tv link forwarded to someone else: when
        they approve it, plex.tv refuses because the sender's browser made the PIN.
        So no sign-in here, the app's included, lets this server make the PIN. It
        can't stop another site's page making a PIN in that person's own browser
        (plex.tv answers any site), so only they can: by pressing Allow only on a
        sign-in they started here (README, Security).
        """
        return self._pin_window(request, safe_next(request.query.get("next")), bool(request.query.get("invite")))

    def _pin_window(self, request: web.Request, next_: str, invite: bool, mobile: Optional[dict] = None,
                    page: bool = False) -> web.Response:
        client = secrets.token_hex(16)
        params = {"client": client, "product": PLEX_PRODUCT, "next": next_, "invite": invite,
                  "post": "/auth/plex/pin", "headers": {"X-Plexbie": "1"}}
        if page:
            # No window to close: back to the site, with the banner for what went wrong.
            params["back"] = {"0": "/?login=failed", "410": "/?login=expired", "429": "/?login=busy"}
        logger.info("Plex sign-in started" + (" from the app" if mobile else ""))
        response = web.Response(content_type="text/html", text=pin_page(params), headers={
            "Cache-Control": "no-store", "Content-Security-Policy": WINDOW_CSP})
        start = {"client": client}
        if mobile:
            start["mobile"] = mobile
        elif page:
            start["page"] = True
        self._set(response, START_COOKIE, start, FLOW_SECONDS, request)
        return response

    async def plex_pin(self, request: web.Request) -> web.Response:
        """The PIN the sign-in window made: remember it, and say where to go on plex.tv."""
        # Only this site's own sign-in window: a header other sites can't add, and
        # the device id this browser's window was given.
        if request.headers.get("X-Plexbie") != "1" or not self._same_origin(request):
            return web.json_response({"error": "Start the sign-in from this site."}, status=403)
        start = self._cookie(request, START_COOKIE) or {}
        if not start.get("client"):
            return web.json_response({"error": "That sign-in expired. Press Sign in with Plex again."}, status=410)
        if self._pin_limit.over(request):
            return web.json_response({"error": "Too many sign-ins. Wait a few minutes and try again."}, status=429)
        try:
            body = await request.json()
            pin_id, code = int(body["id"]), str(body["code"])
        except Exception:
            return web.json_response({"error": "Bad PIN"}, status=400)
        if not re.fullmatch(r"[A-Za-z0-9]{4,64}", code):
            return web.json_response({"error": "Bad PIN"}, status=400)
        mobile = start.get("mobile") if app_flow(start.get("mobile")) else None
        # The app's sheet and a whole-page sign-in have no site polling behind them:
        # plex.tv's forwardUrl brings them back to the whole-page callback, which
        # finishes there (or sends the sheet on to the app).
        popup = not mobile and not start.get("page")
        response = web.json_response({"url": self._plex_auth_url(request, code, popup=popup, client=start["client"])})
        flow = self._plex_flow(request, pin_id, code, start["client"], body.get("next"),
                               bool(body.get("invite")) and not mobile)
        if mobile:
            flow["mobile"] = mobile
        self._set(response, FLOW_COOKIE, flow, FLOW_SECONDS, request)
        self._clear(response, START_COOKIE)
        return response

    def _same_origin(self, request: web.Request) -> bool:
        origin = request.headers.get("Origin")
        return not origin or origin in {self.base_url(request), f"{request.scheme}://{request.host}", self.public or None}

    async def plex_check(self, request: web.Request) -> web.Response:
        if self._check_limit.over(request):
            return web.json_response({"waiting": True})
        flow = self._cookie(request, FLOW_COOKIE) or {}
        if flow.get("via") != "plex" or not flow.get("pin") or not flow.get("client"):
            if self.session(request):
                return web.json_response({"done": True, "next": "/"})
            return web.json_response({"error": "That sign-in expired. Press Sign in with Plex again."}, status=410)
        response = web.json_response({"waiting": True})
        try:
            target = await self._finish_plex(flow, request, response)
        except web.HTTPFound as redirect:
            return web.json_response({"done": True, "next": redirect.location})
        if target is None:
            return response
        response.text = json.dumps({"done": True, "next": target})
        return response

    async def plex_callback(self, request: web.Request) -> web.Response:
        popup = bool(request.query.get("popup"))
        flow = self._cookie(request, FLOW_COOKIE) or {}
        if flow.get("via") != "plex" or not flow.get("pin") or not flow.get("client"):
            # In the small window the first tab has usually finished already.
            if popup:
                return self._close_window()
            raise web.HTTPFound("/?login=expired")
        mobile = flow.get("mobile") if app_flow(flow.get("mobile")) else None
        if mobile:
            return await self._plex_back_to_app(flow, mobile, request)
        response = self._close_window() if popup else web.HTTPFound("/")
        target = await self._finish_plex(flow, request, response)
        if target is None:
            raise web.HTTPFound("/?login=cancelled")
        if not popup:
            response.headers["Location"] = target
            raise response
        return response

    async def _plex_back_to_app(self, flow: dict, mobile: dict, request: web.Request) -> web.Response:
        """plex.tv sent the app's sheet back. Approved: on to the app with a code. Not
        yet (plex.tv can forward a moment before the PIN shows as approved): look again
        a few times, then tell the app it didn't happen."""
        response = web.HTTPFound("/")
        try:
            target = await self._finish_plex(flow, request, response)
        except web.HTTPFound:
            raise self._plex_app_end("failed", mobile)
        if target == CONFIRM_PATH:
            response.headers["Location"] = target
            response.headers.update(PRIVATE)
            raise response
        tries = request.query.get("try", "0")
        tries = int(tries) if tries.isascii() and tries.isdigit() and len(tries) < 3 else 9
        if tries >= 4:
            raise self._plex_app_end("cancelled", mobile)
        return web.Response(content_type="text/html", headers={**PRIVATE, "Content-Security-Policy": WINDOW_CSP},
                            text=_page("<p>Checking with Plex…</p>", refresh=f"/auth/plex/callback?try={tries + 1}"))

    def _plex_app_end(self, outcome: str, mobile: dict) -> web.HTTPFound:
        response = back_to_app(mobile, error=_app_error(outcome))
        self._clear(response, FLOW_COOKIE)
        return response

    @staticmethod
    def _close_window() -> web.Response:
        return web.Response(content_type="text/html", headers={"Content-Security-Policy": WINDOW_CSP}, text=_page(
            "<p>You're signed in. You can close this window.</p><p><a style='color:#ffd1e4' href='/'>Open Plexbie</a></p>"
            "<script>setTimeout(function(){window.close()},300)</script>"))

    async def _finish_plex(self, flow: dict, request: web.Request, response: web.StreamResponse) -> Optional[str]:
        """If the PIN was approved, sign them in on `response` and return where to go.
        None while it's still waiting. Raises HTTPFound for a failure page.

        Once per PIN: the polling tab and the sign-in window can both get here, and
        the first one signs Plexbie out of the Plex account afterwards, which would
        fail the second. The second just gets the first one's answer (the browser
        already holds the session cookie). A PIN's lock goes once nobody holds or
        waits on it, so PINs nobody approves leave nothing behind.
        """
        pin = int(flow["pin"])
        lock = self._pin_locks.setdefault(pin, asyncio.Lock())
        self._pin_users[pin] = self._pin_users.get(pin, 0) + 1
        try:
            async with lock:
                if pin in self._pins_done:
                    self._clear(response, FLOW_COOKIE)
                    return self._pins_done[pin]
                target = await self._finish_plex_once(flow, request, response)
                if target is not None:
                    self._pins_done[pin] = target
                    while len(self._pins_done) > 500:
                        self._pins_done.pop(next(iter(self._pins_done)))
                return target
        finally:
            self._pin_users[pin] -= 1
            if not self._pin_users[pin]:
                del self._pin_users[pin]
                self._pin_locks.pop(pin, None)

    async def _finish_plex_once(self, flow: dict, request: web.Request, response: web.StreamResponse) -> Optional[str]:
        try:
            client = str(flow["client"])
            pin = await self._fetch_json("get", f"https://plex.tv/api/v2/pins/{int(flow['pin'])}", headers=self._plex_headers(client))
            if not secure_equals(str(pin.get("code") or ""), str(flow.get("code") or "")):
                return None                 # not the PIN this browser made
            token = pin.get("authToken")
            if not token:
                return None
            logger.info("Plex sign-in approved")
            me = await self._fetch_json("get", "https://plex.tv/api/v2/user", headers=self._plex_headers(client, token))
        except Exception as e:
            logger.warning(f"Plex sign-in failed: {type(e).__name__}")
            raise web.HTTPFound("/?login=failed")
        person = {
            "via": "plex",
            "sid": secrets.token_urlsafe(12),
            "id": str(me["id"]),
            "name": me.get("username") or me.get("title") or "Plex user",
            "email": me.get("email"),
            "avatar": None,
        }
        mobile = flow.get("mobile")
        if app_flow(mobile):
            # From the app: no session cookie; an invite it was opened with is accepted
            # now (with their Plex sign-in, as on the website), then they're asked to
            # confirm, then a one-time code for the app.
            outcome = await self._use_invite(mobile["invite"], me, token) if mobile.get("invite") else None
            self._clear(response, FLOW_COOKIE)
            self._set(response, CONFIRM_COOKIE, {"mobile": mobile, "person": person, "invite": outcome}, CONFIRM_SECONDS, request)
            self._background(self._forget_device(token, client))
            return CONFIRM_PATH
        target = safe_next(flow.get("next"))
        outcome = await self._use_invite(flow.get("invite"), me, token) if flow.get("invite") else None
        if outcome:
            target = f"/?invite={outcome}"
        self._clear(response, FLOW_COOKIE)
        if outcome in ("ok", "already", "invalid"):
            self._clear(response, INVITE_COOKIE)
        # Done with their Plex sign-in: sign Plexbie out of their account so the
        # key Plex gave us stops working, rather than merely being forgotten.
        self._background(self._forget_device(token, client))
        self._set(response, SESSION_COOKIE, person, SESSION_DAYS * 86400, request)
        logger.info(f"Web sign-in via Plex: {me.get('username')}")
        return target

    def _background(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _forget_device(self, token: str, client: str) -> None:
        """Remove Plexbie from the account's authorized devices, which revokes the token."""
        from core.blocking import run_blocking
        try:
            from plexapi.myplex import MyPlexAccount
            account = await run_blocking(MyPlexAccount, token=token)
            for device in await run_blocking(account.devices):
                if device.clientIdentifier == client:
                    await run_blocking(device.delete)
        except Exception as e:
            logger.info(f"Could not sign Plexbie out of a Plex account: {type(e).__name__}")

    # ------------------------------------------------------ the Plexbie app
    def app_methods(self) -> list:
        """The sign-ins the app may offer here."""
        return (["discord"] if self.discord_ready() else []) + ["plex"]

    async def mobile_start(self, request: web.Request) -> web.Response:
        """The app's sign-in, opened in the phone's browser sheet (see portal/mobile.py).
        Anything not exactly right is refused here, before Discord or Plex is asked,
        and nothing the app sent is written back into the page."""
        mobile = start_params(request.query)
        via = request.query.get("via")
        if mobile is None or via not in self.app_methods():
            return web.Response(status=400, content_type="text/html", headers={**PRIVATE, "Content-Security-Policy": WINDOW_CSP},
                                text=_page("<p>This sign-in link isn't right. Start again from the Plexbie app.</p>"))
        if self.mobile.start_limit.over(request):
            raise back_to_app(mobile, error="busy")
        invite = request.query.get("invite")
        if invite and via == "plex" and self.invites is not None and not self._limited(request):
            found = await self.invites.find(invite)
            if found:
                mobile = {**mobile, "invite": found[0]}
        if via == "discord":
            response = self._to_discord(request, {"next": "/", "mobile": mobile})
            response.headers.update(PRIVATE)
            raise response
        response = self._pin_window(request, "/", False, mobile)
        response.headers.update(PRIVATE)
        return response

    async def mobile_token(self, request: web.Request) -> web.Response:
        """A one-time code and the app's PKCE verifier, traded for an app session token."""
        if request.headers.get("X-Plexbie") != "1":
            return web.json_response({"error": "Missing request header."}, status=403, headers=PRIVATE)
        if self.mobile.token_limit.over(request):
            return web.json_response({"error": "Too many sign-ins. Wait a few minutes and try again."},
                                     status=429, headers=PRIVATE)
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            body = {}
        try:
            result = await self.mobile.exchange(body.get("code"), body.get("verifier"), body.get("device"))
        except Unavailable:
            return web.json_response({"error": UNAVAILABLE}, status=503, headers=PRIVATE)
        if result is None:
            return web.json_response({"error": BAD_CODE}, status=400, headers=PRIVATE)
        return web.json_response(result, headers=PRIVATE)

    async def mobile_confirm_page(self, request: web.Request) -> web.Response:
        """"Sign in to the Plexbie app as …?" Discord and plex.tv can sign someone in
        without a click (they remember them), and on Android another app could have
        started this sheet, so the code is only made after a tap here, on this site.
        It is shown to whoever started the sign-in, so it doesn't help when a Plex
        link was sent to someone else to approve; plex.tv's address check stops a
        forwarded link, but not another site's page that makes the PIN in their own
        browser (see plex_go)."""
        pending = self._cookie(request, CONFIRM_COOKIE) or {}
        person, mobile = pending.get("person") or {}, pending.get("mobile")
        headers = {**PRIVATE, "Content-Security-Policy": CONFIRM_CSP}
        if not app_flow(mobile) or not person.get("id"):
            return web.Response(status=410, content_type="text/html", headers=headers,
                                text=_page("<p>This sign-in has expired. Start again from the Plexbie app.</p>"))
        name = html.escape(str(person.get("name") or "you"))
        service = "Plex" if person.get("via") == "plex" else "Discord"
        button = ("font:inherit;font-weight:600;border-radius:999px;padding:12px 28px;margin:6px;cursor:pointer;"
                  "border:1.5px solid #ffd1e4;")
        return web.Response(content_type="text/html", headers=headers, text=_page(
            f"<p style='font-size:20px'>Sign in to the Plexbie app as <b>{name}</b>?</p>"
            f"<p style='opacity:.8'>With {service}. Only continue if you started this from the Plexbie app on this device.</p>"
            f"<form method=post action='{CONFIRM_PATH}'>"
            f"<button name=answer value=yes style='{button}background:#ffd1e4;color:#10172b'>Sign in</button>"
            f"<button name=answer value=no style='{button}background:none;color:#ffd1e4'>Cancel</button></form>"))

    async def mobile_confirm(self, request: web.Request) -> web.Response:
        """The tap on the confirm page: a one-time code for the app, or "denied"."""
        pending = self._cookie(request, CONFIRM_COOKIE) or {}
        person, mobile = pending.get("person") or {}, pending.get("mobile")
        if not self._same_origin(request) or not app_flow(mobile) or not person.get("id"):
            return web.Response(status=410, content_type="text/html", headers={**PRIVATE, "Content-Security-Policy": CONFIRM_CSP},
                                text=_page("<p>This sign-in has expired. Start again from the Plexbie app.</p>"))
        try:
            answer = (await request.post()).get("answer")
        except Exception:
            answer = None
        invite = {"invite": pending["invite"]} if pending.get("invite") else {}
        if answer == "yes":
            response = back_to_app(mobile, code=self.mobile.mint_code(mobile, person.get("via") or "discord", person), **invite)
        else:
            response = back_to_app(mobile, error="denied", **invite)
        self._clear(response, CONFIRM_COOKIE)
        raise response

    # -------------------------------------------------------- invite links
    def _limited(self, request: web.Request) -> bool:
        return self._invite_limit.over(request)

    async def open_invite(self, request: web.Request) -> web.Response:
        """/invite/<code>: remember the invite in a cookie, then drop the code from the address bar."""
        response = web.HTTPFound("/invite")
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        found = None
        if self.invites is not None and not self._limited(request):
            found = await self.invites.find(request.match_info.get("token"))
        if found:
            self._set(response, INVITE_COOKIE, {"key": found[0]}, INVITE_SECONDS, request)
        else:
            self._clear(response, INVITE_COOKIE)
        raise response

    async def invite_check(self, request: web.Request) -> web.Response:
        """The app's invite screen: what an invite link's code may show (as invite_info)."""
        try:
            token = str((await request.json()).get("token") or "")[:128]
        except Exception:
            token = ""
        found = None
        if token and self.invites is not None and not self._limited(request):
            found = await self.invites.find(token)
        return await self._invite_answer(found[0] if found else None)

    async def mobile_return(self, request: web.Request) -> web.Response:
        """The app's https sign-in address, opened in a browser: on to the app."""
        raise forward_to_app(request.query)

    async def invite_info(self, request: web.Request) -> web.Response:
        """What the invite page may show. Nothing about why a link doesn't work."""
        return await self._invite_answer((self._cookie(request, INVITE_COOKIE) or {}).get("key"))

    async def _invite_answer(self, key: Optional[str]) -> web.Response:
        found = await self.invites.usable(key) if (self.invites is not None and key) else None
        if not found:
            return web.json_response({"valid": False}, headers={"Cache-Control": "no-store"})
        rec = found[1]
        return web.json_response({
            "valid": True,
            "label": rec.get("label"),
            "inviter": rec.get("created_by"),
            "emailLocked": bool(rec.get("email")),
            "expiresAt": rec.get("expires_at"),
            # The house rules, from the bot's own settings, so the page can't drift from them.
            "inactivityDays": self.config.inactivity_removal_days,
            "warnDays": self.config.inactivity_warning_days,
        }, headers={"Cache-Control": "no-store"})

    async def _use_invite(self, key: str, me: dict, token: str) -> Optional[str]:
        """Never raises: anything that breaks answers "failed" (the invite stays
        unspent), so the sign-in still finishes and Plexbie still signs itself out
        of their Plex account."""
        from plugins.user_invites.cog import join_with_invite_link
        if self.invites is None:
            return None
        try:
            name = (me.get("username") or me.get("title") or "").strip()
            access = await self._plex_access()
            if access is not None and str(me.get("id")) in access:
                return "already"       # already on Plex: leave the invite unspent

            async def join(rec: dict) -> bool:
                result = await join_with_invite_link(
                    self.bot, self.services, plex_name=name, plex_account_id=str(me.get("id")),
                    email=me.get("email") or "", user_token=token,
                    label=rec.get("label") or "", created_by=rec.get("created_by") or "an admin")
                return bool(result.get("ok"))

            outcome = await self.invites.redeem(key, me, join)
        except Exception as e:
            # The type only: the message could carry their Plex sign-in.
            logger.error(f"Invite link could not be used: {type(e).__name__}")
            return "failed"
        if outcome == "ok":
            self.cache.drop("auth:plex-access")
            self.cache.drop("admin:shared")
        return outcome

    async def logout(self, request: web.Request) -> web.Response:
        """Sign out: the cookie is cleared, and the session is refused from now on
        even if a copy of the cookie turns up elsewhere."""
        await self.mobile.load()
        s = self.session(request)
        if s and s.get("app"):
            # An app sign-in is ended outright; if that can't be stored, say so rather
            # than "signed out" while the token would still work after a restart.
            if not await self.mobile.revoke(s["app"]):
                return web.json_response({"error": UNAVAILABLE}, status=503)
            from core import notify
            await notify.forget_app_session(s["app"])
        elif s and s.get("sid"):
            async with self._revoke_lock:
                await self.load_revoked()
                if self._revoked is None:
                    # The signed-out list couldn't be read: saving one entry over it would
                    # bring every earlier signed-out cookie back.
                    return web.json_response({"error": UNAVAILABLE}, status=503)
                now = int(time.time())
                revoked = {k: v for k, v in self._revoked.items() if v > now}
                revoked[s["sid"]] = int(s.get("exp") or now + SESSION_DAYS * 86400)
                try:
                    await kv_set(*REVOKED, revoked)
                except Exception as e:
                    # Still signed in, so trying again saves it again.
                    logger.warning(f"Could not save a sign-out: {type(e).__name__}")
                    return web.json_response({"error": UNAVAILABLE}, status=503)
                self._revoked = revoked
        response = web.json_response({"ok": True})
        self._clear(response, SESSION_COOKIE)
        return response

    # ------------------------------------------------------------ who is it
    async def _discord_roles(self, uid: int) -> Dict[str, Any]:
        """{in_guild, member, admin} from the bot's own view of the guild."""
        async def load():
            guild = home_guild(self.bot, self.config)
            member = guild.get_member(uid) if guild else None
            if guild and member is None:
                try:
                    member = await guild.fetch_member(uid)
                except Exception:
                    member = None
            roles = {r.id for r in member.roles} if member else set()
            owner = bool(self.config.bot_owner_id) and uid == self.config.bot_owner_id
            admin = owner or bool(member and (
                (self.config.admin_role_id and self.config.admin_role_id in roles)
                or member.guild_permissions.administrator))
            plex_member = owner or admin or bool(self.config.plex_member_role_id and self.config.plex_member_role_id in roles)
            return {"in_guild": member is not None or owner, "member": plex_member, "admin": admin}
        return await self.cache.get(f"auth:discord:{uid}", 60, load)

    async def _plex_access(self) -> Optional[set]:
        """The Plex account ids (as strings) with access now, owner included, or None
        if unknown. Ids only: a name can be registered by anyone on plex.tv, and a
        Plex Home user's title ("Kids") isn't even a username, so matching names let
        a stranger who took that name in."""
        info = await self._plex_share_info()
        return info["ids"] if info else None

    async def _plex_share_info(self) -> Optional[dict]:
        """{"ids": set of account ids with access, "owner": the owner's account id or
        None}, or None if nobody can tell. From plex.tv's share list; if plex.tv
        can't be reached, the accounts Plexbie has already matched by id."""
        async def load():
            from core.blocking import run_blocking
            from core.plex_account import can_sign_in, shared_accounts
            if can_sign_in(self.config):
                try:
                    accounts = await run_blocking(shared_accounts, self.config)
                    info = {"ids": {str(a["id"]) for a in accounts if a.get("id")},
                            "owner": next((str(a["id"]) for a in accounts if a.get("owner") and a.get("id")), None),
                            "thumbs": {str(a["id"]): a["thumb"] for a in accounts if a.get("id") and a.get("thumb")}}
                    await kv_set(*SHARE_LIST, {"ids": sorted(info["ids"]), "owner": info["owner"], "at": time.time()})
                    return info
                except Exception as e:
                    logger.info(f"plex.tv share list unavailable ({type(e).__name__}); using the last one it gave")
            # plex.tv's own last answer, while it's recent. Not Plexbie's tracking rows:
            # those keep people removed in Plex directly (People shows them as having
            # lost access), who'd be let back in for as long as plex.tv is down.
            last = await kv_get(*SHARE_LIST) or {}
            if isinstance(last, dict) and time.time() - float(last.get("at") or 0) < SHARE_LIST_DAYS * 86400:
                return {"ids": {str(i) for i in last.get("ids") or []}, "owner": last.get("owner")}
            return None
        return await self.cache.get("auth:plex-access", 300, load)

    async def plex_thumb(self, account_id: Any) -> Optional[str]:
        """Someone's plex.tv picture as plex.tv gave it in the last five minutes, or None."""
        info = await self._plex_share_info()
        thumb = ((info or {}).get("thumbs") or {}).get(str(account_id)) if account_id else None
        return thumb if isinstance(thumb, str) and PLEX_THUMB.match(thumb) else None

    async def _plex_avatar(self, account_id: Any) -> Optional[str]:
        """The site's address for someone's plex.tv picture: it changes when the picture
        does, so browsers and the app fetch the new one."""
        thumb = await self.plex_thumb(account_id)
        if not thumb:
            return None
        return f"/img/plex-avatar/{account_id}/{hashlib.sha1(thumb.encode()).hexdigest()[:12]}.png"

    def _discord_avatar_hash(self, uid: int, signed_in_with: Optional[str]) -> Optional[str]:
        """Their Discord picture as the bot sees it now (it hears about changes), else
        the one they signed in with."""
        user = self.bot.get_user(uid) if self.bot is not None else None
        live = getattr(getattr(user, "avatar", None), "key", None)
        avatar = live or signed_in_with
        return avatar if avatar and AVATAR_KEY.match(str(avatar)) else None

    async def _fetch_json(self, method: str, url: str, **kw) -> Any:
        """One call to Discord or plex.tv: the JSON reply. A bad status raises aiohttp's
        error, whose .status the Discord callback reads to pause on a 429."""
        async with getattr(self.services.http_session, method)(url, timeout=15, **kw) as r:
            r.raise_for_status()
            return await r.json()

    async def _plex_row(self, *, discord_id: Optional[int] = None, plex_name: Optional[str] = None,
                        plex_id: Optional[str] = None):
        from database.people import people_for_account, person_by_discord, person_by_name
        async with get_session() as session:
            if discord_id:
                return await person_by_discord(session, discord_id)
            if plex_id and str(plex_id).isdigit():
                # By account only: still them after a username change, and never
                # someone else's row because a stranger registered the same name.
                rows = await people_for_account(session, plex_id)
                if len(rows) > 1:
                    # Two rows claiming one account: neither is trusted for a Discord
                    # link (and so admin) until an admin sorts them out on People.
                    logger.warning(f"{len(rows)} tracked people share Plex account {plex_id}; not using either")
                    return None
                return rows[0] if rows else None
            return await person_by_name(session, plex_name)

    async def who(self, request: web.Request) -> Optional[dict]:
        await self.load_revoked()
        await self.mobile.load()
        s = self.session(request)
        if not s:
            return None
        if s.get("sid") and not s.get("app") and self._revoked is None:
            # Can't tell whether this cookie was signed out: not "signed in" either.
            raise web.HTTPServiceUnavailable(text=json.dumps({"error": UNAVAILABLE}), content_type="application/json",
                                             headers={"Retry-After": "5", "Cache-Control": "no-store"})
        return await self.describe(s)

    async def describe(self, s: dict) -> dict:
        """Who a session belongs to, and what they may do, as of now: roles and Plex
        access are looked up fresh each time (a session only says who signed in)."""
        out = {
            "user": {"id": s["id"], "name": s.get("name"), "avatar": None, "via": s.get("via")},
            "member": False, "admin": False, "joinPending": False,
            "discordId": None, "plexName": None, "plexAccountId": None, "email": None, "inGuild": None, "accessUnknown": False,
        }
        if s.get("via") == "discord":
            uid = int(s["id"])
            roles = await self._discord_roles(uid)
            row = await self._plex_row(discord_id=uid)
            invite = await kv_get("plex_invites", str(uid), {}) or {}
            avatar = self._discord_avatar_hash(uid, s.get("avatar"))
            out.update(
                member=roles["member"], admin=roles["admin"], inGuild=roles["in_guild"], discordId=str(uid),
                plexName=row.plex_username if row else None,
                joinPending=invite.get("status") == "pending" and not roles["member"],
            )
            if avatar:
                # Through the image proxy: visitors' browsers contact no third party.
                out["user"]["avatar"] = f"/img/avatar/{uid}/{avatar}.png"
            elif row and row.plex_user_id:
                out["user"]["avatar"] = await self._plex_avatar(row.plex_user_id)   # no Discord picture: their Plex one
        else:
            info = await self._plex_share_info()
            access = info["ids"] if info else None
            known_owner = None
            if info and info.get("owner"):
                known_owner = info["owner"]
                if self._owner_id != known_owner:          # remembered for when plex.tv can't be asked
                    self._owner_id = known_owner
                    await kv_set(*OWNER_ID, known_owner)
            else:
                if self._owner_id is None:
                    try:
                        self._owner_id = await kv_get(*OWNER_ID)
                    except Exception:
                        self._owner_id = None
                known_owner = self._owner_id
            # The owner by account id only, as plex.tv reported it (remembered). Until
            # plex.tv has answered once nobody is the owner: names and emails are
            # what the signer-in chose, so they can't stand in for it.
            owner = bool(known_owner) and str(s.get("id")) == str(known_owner)
            row = await self._plex_row(plex_name=s.get("name"), plex_id=s.get("id"))
            linked_admin = False
            if row and row.discord_id:
                linked_admin = (await self._discord_roles(int(row.discord_id)))["admin"]
            pending = await kv_get(WEB_JOINS_NAMESPACE, s["id"], {}) or {}
            member = owner or (access is not None and str(s.get("id")) in access)
            out.update(
                member=member, admin=owner or linked_admin, plexName=s.get("name"), plexAccountId=s["id"],
                email=s.get("email"), discordId=str(row.discord_id) if row and row.discord_id else None,
                accessUnknown=access is None and not owner,
                joinPending=pending.get("status") == "pending" and not member,
            )
            out["user"]["avatar"] = await self._plex_avatar(s["id"])
            if not out["user"]["avatar"] and row and row.discord_id:
                linked = self._discord_avatar_hash(int(row.discord_id), None)   # no Plex picture: their Discord one
                if linked:
                    out["user"]["avatar"] = f"/img/avatar/{row.discord_id}/{linked}.png"
        return out
