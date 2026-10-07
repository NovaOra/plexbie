# path: portal/app.py
"""The aiohttp application behind a household's Plexbie.

Routes:
  /api/*     JSON for the web app (types in web/src/api/types.ts)
  /img/*     posters and covers, proxied and cached (portal/images.py)
  /auth/mobile/*, /api/mobile   sign-in for the Plexbie app (portal/mobile.py)
  /*         the built web app, with index.html for client-side routes

`who` decides who is asking: in the bot it reads the signed Discord session;
in the read-only preview it is always the bot owner. `readonly` refuses every
request that would change anything.
"""
import mimetypes
import os
import re
from urllib.parse import urlparse
from pathlib import Path
from typing import Awaitable, Callable, Optional

from aiohttp import web

from core.logging import get_logger
from portal.admin import Admin
from portal.data import Data
from portal.images import IMAGE_TRIES, ImageProxy
from portal.mobile import API_VERSION as MOBILE_API_VERSION, bearer
from portal.ratelimit import Limiter, visitor_scheme as _visitor_scheme
from utils.formatting import ensure_utc

logger = get_logger(__name__)

Who = Callable[[web.Request], Awaitable[Optional[dict]]]

# Cloudflare Web Analytics (the visit counts the privacy page mentions) loads its
# beacon from Cloudflare and reports back there; nothing else outside is allowed.
# Containers often lack these in their system type table, which made fonts and
# images go out as application/octet-stream.
for _type, _ext in (("font/woff2", ".woff2"), ("image/webp", ".webp"), ("image/avif", ".avif"),
                    ("application/manifest+json", ".webmanifest"), ("image/x-icon", ".ico")):
    mimetypes.add_type(_type, _ext)

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' https://static.cloudflareinsights.com; "
    "font-src 'self'; connect-src 'self' https://cloudflareinsights.com; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self' https://discord.com"
)


@web.middleware
async def canonical_host(request: web.Request, handler):
    """www.<public name> -> <public name>: sign-in cookies and the Discord/Plex
    return addresses belong to one name, so the site must only be used under it."""
    from core.config import public_url
    public = urlparse(public_url(os.getenv("WEB_PUBLIC_URL", "")))
    # The Host header only: Cloudflare sets it, while X-Forwarded-Host is whatever
    # the visitor sends (and could get a self-redirect cached for real visitors).
    host = (request.host or "").strip().lower()
    if public.netloc and host == "www." + public.netloc.lower():
        raise web.HTTPMovedPermanently(f"{public.scheme}://{public.netloc}{request.path_qs}",
                                       headers={"Cache-Control": "no-store"})
    if _visitor_scheme(request) == "http" and (not public.netloc or (public.scheme == "https" and host == public.netloc.lower())):
        # http:// through Cloudflare: send them to https, where the page can't be
        # altered on the way and the sign-in cookies (Secure) work. With no public
        # address set, Cloudflare's own word (CF-Visitor) is enough to know https exists.
        raise web.HTTPMovedPermanently(f"https://{public.netloc or host}{request.path_qs}",
                                       headers={"Cache-Control": "no-store"})
    return await handler(request)


def _https_visitor(request: web.Request) -> bool:
    """Reached over https: by the https public name, or (with no public address set)
    through Cloudflare, which says so in CF-Visitor. Never the plain LAN address."""
    from core.config import public_url
    public = urlparse(public_url(os.getenv("WEB_PUBLIC_URL", "")))
    if public.netloc:
        return public.scheme == "https" and (request.host or "").strip().lower() == public.netloc.lower()
    return _visitor_scheme(request) == "https"


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as e:
        response = e
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers["Server"] = "Plexbie"           # not which Python and aiohttp versions
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if not request.path.startswith("/auth/"):
        # Other sites that open a Plexbie page get no handle on it. "allow-popups",
        # and not on /auth/: the Plex sign-in window this site opens goes on to
        # plex.tv, and a stricter policy would cut it loose so it can't be closed.
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
    if _https_visitor(request):
        # Browsers only heed this over https, so the plain LAN address is unaffected.
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.path.startswith("/invite"):
        response.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/auth/"):
        # Sign-in codes and states stay out of other sites' Referer. Not "no-referrer":
        # with it a browser posts this site's own forms with "Origin: null" (portal/mobile.py).
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers.setdefault("Cache-Control", "no-store")
    # A household's own server: nothing on it belongs in search (robots.txt says so too).
    if request.path != "/robots.txt":
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    return response


def _safe_dist(dist: Path) -> bool:
    """The website folder can't be the filesystem root or hold Plexbie's own
    settings and database (a WEB_DIST of "/", "." or "config" would serve them)."""
    root = dist.resolve()
    config = Path("config").resolve()
    return root != Path(root.anchor) and root != config and root not in config.parents \
        and not (root / ".env").exists()


def _err(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def build_app(services, *, who: Who, readonly: bool, dist: Optional[str], image_cache: str,
              data: Optional[Data] = None, auth=None, actions=None,
              invites=None) -> web.Application:
    data = data or Data(services)
    admin = Admin(data, bot=getattr(actions, "bot", None))
    images = ImageProxy(services, data, image_cache, getattr(services.config, "web_image_cache_mb", 500))
    @web.middleware
    async def app_tokens(request: web.Request, handler):
        """The app's Bearer token is checked before anything else: a token that's
        wrong, ended or sent without X-Plexbie is a 401, never a quiet "nobody"
        (portal/mobile.py)."""
        if auth is not None and bearer(request) is not None and (
                request.path.startswith("/api/") or request.path.startswith("/img/")) and request.path != "/api/mobile":
            await auth.mobile.load()
            if not auth.mobile.ready:
                # Can't tell: not "signed out", or the app would forget a good sign-in.
                return web.json_response({"error": "Plexbie can't check sign-ins right now. Try again in a moment."},
                                         status=503, headers={"Retry-After": "5", "Cache-Control": "no-store"})
            if auth.session(request) is None:
                return web.json_response({"error": "Your sign-in has ended. Sign in again."}, status=401,
                                         headers={"WWW-Authenticate": 'Bearer error="invalid_token"', "Cache-Control": "no-store"})
        return await handler(request)

    app = web.Application(middlewares=[canonical_host, security_headers, app_tokens])

    async def member(request) -> dict:
        user = await who(request)
        if not user:
            raise web.HTTPUnauthorized(text='{"error":"Log in first."}', content_type="application/json")
        if not user.get("member"):
            raise web.HTTPForbidden(text='{"error":"Only Plex members can see this."}', content_type="application/json")
        return user

    async def admin_only(request) -> dict:
        user = await member(request)
        if not user.get("admin"):
            raise web.HTTPForbidden(text='{"error":"Admins only."}', content_type="application/json")
        return user

    def safe(fn):
        async def handler(request):
            try:
                return await fn(request)
            except web.HTTPException:
                raise
            except LookupError:
                return _err(404, "Not found.")
            except Exception as e:
                # The route, not the address: some addresses carry a token (an iPhone source).
                route = getattr(request.match_info.route.resource, "canonical", None) or "?"
                logger.warning(f"portal {request.method} {route}: {type(e).__name__}: {e}")
                return _err(502, "Plexbie couldn't reach one of its services. Try again in a moment.")
        return handler

    # ---------------------------------------------------------------- api
    async def session(request):
        user = await who(request)
        if not user:
            # Not signed in is an answer, not an error: every visitor's first page
            # load asks, and a 401 put an error in each of their consoles.
            return web.json_response(None)
        return web.json_response(user)

    async def status(request):
        await member(request)
        return web.json_response(await data.status())

    async def arrivals(request):
        await member(request)
        return web.json_response(await data.arrivals())

    async def library(request):
        await member(request)
        kind = request.query.get("kind", "movie")
        if kind not in ("movie", "tv", "book"):
            return _err(400, "Unknown shelf.")
        return web.json_response(await data.library(kind))

    async def search(request):
        user = await member(request)
        if actions is not None:
            actions.limit(user["user"]["id"], "lookup")
        kind = request.query.get("kind", "movie")
        if kind not in ("movie", "tv", "audiobook", "ebook"):
            return _err(400, "Unknown kind.")
        return web.json_response(await data.search(request.query.get("q", ""), kind))

    def ids(user: dict):
        did = user.get("discordId")
        return (int(did) if did else None), user.get("plexAccountId")

    async def tracked_name(user: dict) -> Optional[str]:
        """The name Plexbie tracks this member under, for stats kept by name. From a
        Plex sign-in that's the row for their account id, not the plex.tv username
        they signed in with, which they choose (and could set to someone else's)."""
        if (user.get("user") or {}).get("via") == "discord":
            return user.get("plexName")                   # already from their own row
        pid = str(user.get("plexAccountId") or "")
        if not pid.isdigit():
            return None
        from database.people import sole_person_for_account
        from database.session import get_session
        async with get_session() as session:
            row = await sole_person_for_account(session, pid)
        return row.plex_username if row else None

    async def title(request):
        user = await member(request)
        if actions is not None:
            actions.limit(user["user"]["id"], "lookup")
        kind = request.match_info["kind"]
        if kind not in ("movie", "tv", "audiobook", "ebook"):
            return _err(404, "Not found.")
        return web.json_response(await data.title(kind, request.match_info["id"], *ids(user)))

    async def my_requests(request):
        user = await member(request)
        return web.json_response(await data.my_requests(*ids(user)))

    async def search_all(request):
        user = await member(request)
        if actions is not None:
            actions.limit(user["user"]["id"], "lookup")
        return web.json_response(await data.search_all(request.query.get("q", "")))

    async def discover(request):
        from portal import prefs
        mine = await prefs.get(await member(request))
        out = await data.discover(request.match_info["kind"], prefs.tmdb_codes(mine["languages"]))
        return web.json_response({**out, "languages": mine["languages"], "languageOptions": prefs.options()})

    async def discover_shelf(request):
        from portal import prefs
        user = await member(request)
        if actions is not None:
            # Its own allowance: scrolling Discover shouldn't use up searching.
            actions.limit(user["user"]["id"], "browse")
        mine = await prefs.get(user)
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            raise LookupError("no such page")
        return web.json_response(await data.shelf(request.match_info["kind"], request.match_info["key"], page,
                                                  prefs.tmdb_codes(mine["languages"])))

    async def get_prefs(request):
        from portal import prefs
        return web.json_response({**await prefs.get(await member(request)), "languageOptions": prefs.options()})

    async def similar(request):
        user = await member(request)
        if actions is not None:
            actions.limit(user["user"]["id"], "lookup")
        return web.json_response(await data.similar(request.match_info["kind"], request.match_info["id"]))

    async def popular(request):
        await member(request)
        return web.json_response(await data.popular())

    async def watchparty(request):
        from sqlalchemy import or_, select
        from database.session import get_session
        from plugins.watch_party.models import WatchPartyCredit
        user = await member(request)
        conds = []
        if user.get("discordId"):
            conds.append(WatchPartyCredit.discord_id == int(user["discordId"]))
        name = await tracked_name(user)
        if name:
            conds.append(WatchPartyCredit.plex_username == name)
        credit = None
        if conds:
            async with get_session() as session:
                credit = (await session.execute(select(WatchPartyCredit).where(or_(*conds)))).scalars().first()
        if not credit:
            return web.json_response({"seconds": 0, "sessions": 0, "last": None})
        return web.json_response({"seconds": credit.total_duration, "sessions": credit.total_sessions,
                                  "last": ensure_utc(credit.last_credited_at).isoformat() if credit.last_credited_at else None})

    async def community(request):
        user = await member(request)
        did, _ = ids(user)
        return web.json_response(await data.community(did, await tracked_name(user)))

    async def admin_view(request):
        user = await admin_only(request)
        section = request.match_info["section"]
        if section == "links" and actions is not None:
            return web.json_response(await actions.link_candidates(user))
        if section == "discord" and actions is not None:
            return web.json_response(await actions.discord_overview(user))
        if section == "help":
            from portal import help as helpdesk
            return web.json_response(await helpdesk.all_help())
        if section == "plexinvites" and actions is not None:
            return web.json_response(await actions.plex_invites(user))
        if section == "messages":
            return web.json_response(await admin.message_people())
        if section == "all":
            return web.json_response(await admin.all_requests(request.query.get("q", ""), everything=request.query.get("all") == "1"))
        if section == "tickets":
            return web.json_response(await admin.tickets())
        loaders = {"requests": admin.requests, "joins": admin.joins, "people": admin.people,
                   "cleanup": admin.cleanup, "health": admin.health}
        if invites is not None:
            loaders["invites"] = invites.all
        if section not in loaders:
            return _err(404, "Not found.")
        return web.json_response(await loaders[section]())

    async def admin_request(request):
        await admin_only(request)
        found = await admin.request_detail(request.match_info["key"])
        return web.json_response(found) if found else _err(404, "No such request.")

    async def admin_ticket_view(request):
        await admin_only(request)
        found = await admin.ticket(request.match_info["id"])
        return web.json_response(found) if found else _err(404, "No such ticket.")

    async def refuse(request):
        if readonly:
            return _err(403, "This preview is read-only. Nothing was sent.")
        return _err(404, "Not found.")

    r = app.router
    r.add_get("/api/session", safe(session))
    r.add_get("/api/status", safe(status))
    r.add_get("/api/arrivals", safe(arrivals))
    r.add_get("/api/library", safe(library))
    r.add_get("/api/search", safe(search))
    r.add_get("/api/titles/{kind}/{id}", safe(title))
    r.add_get("/api/requests", safe(my_requests))
    r.add_get("/api/community", safe(community))
    r.add_get("/api/watchparty", safe(watchparty))
    r.add_get("/api/popular", safe(popular))
    r.add_get("/api/discover/{kind}", safe(discover))
    r.add_get("/api/search/all", safe(search_all))
    r.add_get("/api/prefs", safe(get_prefs))
    r.add_get("/api/discover/{kind}/{key}", safe(discover_shelf))
    r.add_get("/api/titles/{kind}/{id}/similar", safe(similar))
    r.add_get("/api/admin/{section}", safe(admin_view))

    async def admin_conversation(request):
        from core import message_log
        await admin_only(request)
        who = request.match_info["who"]
        if not re.fullmatch(r"[dp][\w .@+-]{1,120}", who):
            return _err(404, "Not found.")
        return web.json_response(await message_log.conversation(who))
    r.add_get("/api/admin/messages/{who}", safe(admin_conversation))

    async def admin_cleanup_search(request):
        # Manage → Cleanup's "Keep a title forever" box. Each search asks Plex once per library.
        user = await admin_only(request)
        if actions is None:
            return _err(404, "Not found.")
        actions.limit(user["user"]["id"], "cleanup_search")
        return web.json_response(await actions.cleanup_search(user, request.query.get("q", "")))
    r.add_get("/api/admin/cleanup/search", safe(admin_cleanup_search))
    r.add_get("/api/admin/request/{key:\\d+}", safe(admin_request))
    r.add_get("/api/admin/ticket/{id:[0-9a-f]{12}}", safe(admin_ticket_view))

    async def push_key(request):
        from core import notify
        await member(request)
        return web.json_response({"key": (await notify.vapid_keys())["public"]})
    if not readonly:
        r.add_get("/api/push/key", safe(push_key))
    if readonly:
        r.add_post("/api/{tail:.*}", refuse)
    elif actions is not None:
        async def body(request) -> dict:
            actions.check_request(request)
            try:
                payload = await request.json()
            except Exception:
                payload = {}
            return payload if isinstance(payload, dict) else {}

        async def post_request(request):
            payload = await body(request)
            user = await member(request)
            return web.json_response(await actions.create_request(user, payload), status=201)

        async def post_join(request):
            payload = await body(request)
            user = await who(request)
            if not user:
                return _err(401, "Log in first.")
            return web.json_response(await actions.join(user, payload), status=201)

        async def post_decide(request):
            await body(request)
            user = await admin_only(request)
            approve = request.match_info["decision"] == "approve"
            if request.match_info["decision"] not in ("approve", "decline"):
                return _err(404, "Not found.")
            if request.match_info["what"] == "requests":
                result = await actions.decide_request(user, request.match_info["id"], approve)
            else:
                result = await actions.decide_join(user, request.match_info["id"], approve)
            return web.json_response(result, status=200 if result.get("ok") else 409)

        async def post_exempt(request):
            payload = await body(request)
            return web.json_response(await actions.exempt(await admin_only(request), payload))

        async def post_remove(request):
            payload = await body(request)
            result = await actions.remove_person(await admin_only(request), payload)
            return web.json_response(result, status=200 if result.get("ok") else 409)

        async def push_subscribe(request):
            from core import notify
            payload = await body(request)
            user = await member(request)
            actions.limit(user["user"]["id"], "push")
            ok = await notify.subscribe(payload.get("subscription"), plex_account_id=user.get("plexAccountId"),
                                        plex_name=user.get("plexName"), discord_id=user.get("discordId"))
            return web.json_response({"ok": ok}, status=200 if ok else 400)

        async def push_unsubscribe(request):
            from core import notify
            payload = await body(request)
            user = await member(request)
            actions.limit(user["user"]["id"], "push")
            await notify.unsubscribe(str(payload.get("endpoint") or ""), plex_account_id=user.get("plexAccountId"),
                                     discord_id=user.get("discordId"))
            return web.json_response({"ok": True})

        async def push_app(request):
            """The Plexbie app on this phone: alerts on (a new Expo push token) or off."""
            from core import notify
            payload = await body(request)
            user = await member(request)
            actions.limit(user["user"]["id"], "push")
            if request.path.endswith("/remove"):
                await notify.unregister_app(payload.get("token"), plex_account_id=user.get("plexAccountId"),
                                            discord_id=user.get("discordId"))
                return web.json_response({"ok": True})
            # From the app, the registration is tied to its sign-in, so it ends with it.
            signed = auth.session(request) if auth is not None else None
            ok = await notify.register_app(payload.get("token"), payload.get("platform"), plex_account_id=user.get("plexAccountId"),
                                           plex_name=user.get("plexName"), discord_id=user.get("discordId"),
                                           session=(signed or {}).get("app"), channel=payload.get("channel"),
                                           live=payload.get("live"))
            return web.json_response({"ok": ok}, status=200 if ok else 400)

        async def push_test(request):
            from core import notify
            await body(request)
            user = await member(request)
            actions.limit(user["user"]["id"], "push_test")
            sent = await notify.send_test(plex_account_id=user.get("plexAccountId"), plex_name=user.get("plexName"),
                                          discord_id=user.get("discordId"))
            return web.json_response({"ok": sent > 0, "message": "Sent. It should pop up in a moment." if sent
                                      else "Nothing to send to. Turn alerts on in this browser first."})

        async def post_help(request):
            payload = await body(request)
            user = await member(request)
            return web.json_response(await actions.ask_help(user, request.match_info["id"], payload), status=201)

        async def post_help_search(request):
            await body(request)
            result = await actions.help_search(await admin_only(request), request.match_info["id"])
            return web.json_response(result, status=200 if result.get("ok") else 409)

        async def post_help_name(request):
            await body(request)
            result = await actions.help_by_name(await admin_only(request), request.match_info["id"])
            return web.json_response(result, status=200 if result.get("ok") else 409)

        async def post_help_episodes(request):
            await body(request)
            result = await actions.help_search(await admin_only(request), request.match_info["id"], by_episode=True)
            return web.json_response(result, status=200 if result.get("ok") else 409)

        async def post_help_resolve(request):
            payload = await body(request)
            result = await actions.help_resolve(await admin_only(request), request.match_info["id"], payload)
            return web.json_response(result, status=200 if result.get("ok") else 409)

        r.add_post("/api/requests/{id}/help", safe(post_help))
        r.add_post("/api/admin/help/{id}/search", safe(post_help_search))
        r.add_post("/api/admin/help/{id}/episodes", safe(post_help_episodes))
        r.add_post("/api/admin/help/{id}/name", safe(post_help_name))
        r.add_post("/api/admin/help/{id}/resolve", safe(post_help_resolve))

        # Downloads Sonarr/Radarr won't import by themselves (core/blocked_imports).
        BLOCKED = "/api/admin/blocked/{app:sonarr|radarr}/{did:[\\w.:-]{1,120}}"

        async def get_blocked(request):
            return web.json_response(await actions.blocked_list(await admin_only(request)))

        async def get_blocked_preview(request):
            m = request.match_info
            return web.json_response(await actions.blocked_preview(await admin_only(request), m["app"], m["did"]))

        async def post_blocked_import(request):
            payload = await body(request)
            m = request.match_info
            result = await actions.blocked_import(await admin_only(request), m["app"], m["did"], payload)
            return web.json_response(result, status=200 if result.get("ok") else 409)

        r.add_get("/api/admin/blocked", safe(get_blocked))
        r.add_get(BLOCKED, safe(get_blocked_preview))
        r.add_post(BLOCKED + "/import", safe(post_blocked_import))

        async def get_arr_library(request):
            return web.json_response(await actions.arr_library(await admin_only(request), request.match_info["app"],
                                                               request.query.get("q", "")))

        async def get_arr_episodes(request):
            return web.json_response(await actions.arr_episodes(await admin_only(request), int(request.match_info["id"])))

        r.add_get("/api/admin/arr/{app:sonarr|radarr}/library", safe(get_arr_library))
        r.add_get("/api/admin/arr/sonarr/series/{id:\\d{1,9}}/episodes", safe(get_arr_episodes))

        async def post_admin_ticket(request):
            payload = await body(request)
            result = await actions.admin_ticket(await admin_only(request), request.match_info["key"], payload)
            return web.json_response(result, status=201)

        async def post_request_search(request):
            await body(request)
            result = await actions.request_search(await admin_only(request), request.match_info["key"], request.match_info["how"])
            return web.json_response(result, status=200 if result.get("ok") else 409)

        r.add_post("/api/admin/request/{key:\\d+}/ticket", safe(post_admin_ticket))

        async def post_ticket_comment(request):
            payload = await body(request)
            return web.json_response(await actions.ticket_comment(await admin_only(request), request.match_info["id"], payload))

        async def post_ticket_status(request):
            payload = await body(request)
            result = await actions.ticket_status(await admin_only(request), request.match_info["id"], payload)
            return web.json_response(result, status=200 if result.get("ok") else 409)

        async def post_ticket_take(request):
            await body(request)
            return web.json_response(await actions.ticket_take(await admin_only(request), request.match_info["id"]))

        async def post_member_reply(request):
            payload = await body(request)
            return web.json_response(await actions.member_reply(await member(request), request.match_info["id"], payload))

        r.add_post("/api/admin/ticket/{id:[0-9a-f]{12}}/comment", safe(post_ticket_comment))
        r.add_post("/api/admin/ticket/{id:[0-9a-f]{12}}/status", safe(post_ticket_status))
        r.add_post("/api/admin/ticket/{id:[0-9a-f]{12}}/take", safe(post_ticket_take))
        r.add_post("/api/requests/{id}/help/reply", safe(post_member_reply))
        r.add_post("/api/admin/request/{key:\\d+}/search/{how:again|episodes|name}", safe(post_request_search))
        r.add_post("/api/push/subscribe", safe(push_subscribe))
        r.add_post("/api/push/unsubscribe", safe(push_unsubscribe))
        r.add_post("/api/push/test", safe(push_test))
        r.add_post("/api/push/app", safe(push_app))
        r.add_post("/api/push/app/remove", safe(push_app))
        r.add_post("/api/requests", safe(post_request))

        async def post_prefs(request):
            from portal import prefs
            payload = await body(request)
            user = await member(request)
            actions.limit(user["user"]["id"], "prefs")
            return web.json_response(await prefs.save(user, payload))
        r.add_post("/api/prefs", safe(post_prefs))
        r.add_post("/api/join", safe(post_join))

        # The Android app's latest version (portal/app_release): members only, and a
        # download is a short-lived signed link, opened by the phone's browser.
        from core.blocking import run_blocking
        from portal import app_release

        async def app_latest(request):
            await member(request)
            return web.json_response(app_release.public(await app_release.latest()), headers={"Cache-Control": "no-store"})

        async def app_download_link(request):
            await body(request)
            await member(request)
            info = await app_release.latest()
            if not info:
                return _err(404, "There's no app to download yet.")
            return web.json_response({"url": app_release.link(services.config.web_session_secret, info),
                                      "version": info["version"]}, headers={"Cache-Control": "no-store"})

        async def app_download(request):
            apk = await run_blocking(app_release.apk_for, services.config.web_session_secret, request.match_info["token"])
            if apk is None:
                return web.Response(status=410, text="This download link has run out. Get a new one in Plexbie.",
                                    headers={"Cache-Control": "no-store"})
            return web.FileResponse(apk, headers={
                "Content-Type": "application/vnd.android.package-archive", "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{apk.name}"', "X-Robots-Tag": "noindex"})

        # iPhones: a member's own SideStore/AltStore source, and the build it points at.
        # Both are reached by the token alone (those apps send no sign-in), and both
        # look the person up again every time: once they're off Plex, or they've
        # replaced the address, it stops.
        def site(request) -> str:
            from core.config import public_url
            from portal.auth import Auth
            public = public_url(os.getenv("WEB_PUBLIC_URL", ""))
            return public.rstrip("/") if public else f"{Auth.base_url(request).split('://', 1)[0]}://{request.host}"

        async def still_member(token: str) -> bool:
            s = await app_release.source_check(services.config.web_session_secret, token)
            if s is None or auth is None:
                return False
            return bool((await auth.describe({"via": s["via"], "id": s["id"]})).get("member"))

        async def app_ios_source_link(request):
            payload = await body(request)
            user = await member(request)
            if auth is None:
                return _err(404, "iPhone installs aren't set up here.")
            version = await app_release.source_version(user, renew=payload.get("renew") is True)
            url = f"{site(request)}/app-source/{app_release.source_token(services.config.web_session_secret, user, version)}.json"
            from urllib.parse import quote
            return web.json_response({"url": url, "sidestore": f"sidestore://source?url={quote(url, safe='')}",
                                      "altstore": f"altstore://source?url={quote(url, safe='')}"},
                                     headers={"Cache-Control": "no-store"})

        async def app_ios_source(request):
            token = request.match_info["token"]
            if not await still_member(token):
                return web.json_response({"error": "This Plexbie source has ended. Get a new one on the website's Alerts page."},
                                         status=410, headers={"Cache-Control": "no-store"})
            return web.json_response(app_release.altstore_source(await app_release.latest(), site(request), token),
                                     headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

        async def app_ios_download(request):
            ok = await still_member(request.match_info["token"])
            ipa = await run_blocking(app_release.ipa_for, ok, request.match_info["file"])
            if ipa is None:
                return web.Response(status=410, text="This download has ended. Refresh Plexbie's source in SideStore or AltStore.",
                                    headers={"Cache-Control": "no-store"})
            return web.FileResponse(ipa, headers={
                "Content-Type": "application/octet-stream", "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{ipa.name}"', "X-Robots-Tag": "noindex"})

        r.add_get("/api/app/latest", safe(app_latest))
        r.add_post("/api/app/download-link", safe(app_download_link))
        r.add_get("/download/app/{token}", app_download)
        r.add_post("/api/app/ios-source", safe(app_ios_source_link))
        r.add_get("/app-source/{token}.json", safe(app_ios_source))
        r.add_get("/download/ios/{token}/{file}", safe(app_ios_download))
        r.add_post("/api/admin/{what:requests|joins}/{id}/{decision}", safe(post_decide))
        r.add_post("/api/admin/cleanup/exempt", safe(post_exempt))
        r.add_post("/api/admin/people/remove", safe(post_remove))

        if invites is not None:
            async def post_invite(request):
                payload = await body(request)
                user = await admin_only(request)
                return web.json_response(await actions.create_invite(user, payload, invites, auth.base_url(request) if auth is not None else f"{request.scheme}://{request.host}"), status=201)

            async def post_revoke(request):
                await body(request)
                result = await actions.revoke_invite(await admin_only(request), request.match_info["id"], invites)
                return web.json_response(result, status=200 if result.get("ok") else 409)

            r.add_post("/api/admin/invites", safe(post_invite))
            r.add_post("/api/admin/invites/{id}/revoke", safe(post_revoke))

            async def post_invite_delete(request):
                await body(request)
                result = await actions.delete_invite(await admin_only(request), request.match_info["id"], invites)
                return web.json_response(result, status=200 if result.get("ok") else 409)

            async def post_invite_renew(request):
                await body(request)
                user = await admin_only(request)
                return web.json_response(await actions.renew_invite(user, request.match_info["id"], invites,
                                                                    auth.base_url(request) if auth is not None else f"{request.scheme}://{request.host}"), status=201)

            r.add_post("/api/admin/invites/{id}/delete", safe(post_invite_delete))

            async def post_plex_invite_cancel(request):
                result = await actions.plex_invite_cancel(await admin_only(request), await body(request))
                return web.json_response(result, status=200 if result.get("ok") else 409)

            async def post_plex_invite_change(request):
                result = await actions.plex_invite_change(await admin_only(request), await body(request))
                return web.json_response(result, status=200 if result.get("ok") else 409)

            r.add_post("/api/admin/plex-invites/cancel", safe(post_plex_invite_cancel))
            r.add_post("/api/admin/plex-invites/change", safe(post_plex_invite_change))
            r.add_post("/api/admin/invites/{id}/renew", safe(post_invite_renew))

        async def post_cleanup_settings(request):
            payload = await body(request)
            result = await actions.cleanup_settings(await admin_only(request), payload)
            return web.json_response(result, status=200 if result.get("ok") else 409)

        async def post_cleanup_scan(request):
            await body(request)
            result = await actions.cleanup_scan(await admin_only(request))
            return web.json_response(result, status=200 if result.get("ok") else 409)

        async def post_link(request):
            payload = await body(request)
            result = await actions.link_person(await admin_only(request), payload)
            return web.json_response(result, status=200 if result.get("ok") else 409)

        async def post_unlink(request):
            payload = await body(request)
            result = await actions.unlink_person(await admin_only(request), payload)
            return web.json_response(result, status=200 if result.get("ok") else 409)

        r.add_post("/api/admin/people/link", safe(post_link))
        r.add_post("/api/admin/people/unlink", safe(post_unlink))

        async def post_match(request):
            result = await actions.match_person(await admin_only(request), await body(request))
            return web.json_response(result, status=200 if result.get("ok") else 409)
        r.add_post("/api/admin/people/match", safe(post_match))

        async def post_keep(request):
            result = await actions.keep_person(await admin_only(request), await body(request))
            return web.json_response(result, status=200 if result.get("ok") else 409)
        r.add_post("/api/admin/people/keep", safe(post_keep))

        async def post_rename(request):
            result = await actions.rename_person(await admin_only(request), await body(request))
            return web.json_response(result, status=200 if result.get("ok") else 409)
        r.add_post("/api/admin/people/rename", safe(post_rename))
        async def post_say(request):
            payload = await body(request)
            result = await actions.say(await admin_only(request), payload)
            return web.json_response(result, status=200 if result.get("ok") else 409)

        r.add_post("/api/admin/say", safe(post_say))

        # Manage → Messages: answer as Plexbie, mark done, put a DM on their ticket.
        async def post_message_reply(request):
            payload = await body(request)
            return web.json_response(await actions.message_reply(await admin_only(request), request.match_info["who"], payload))

        async def post_message_done(request):
            payload = await body(request)
            return web.json_response(await actions.message_done(await admin_only(request), request.match_info["who"], payload))

        async def post_message_to_ticket(request):
            await body(request)
            return web.json_response(await actions.message_to_ticket(await admin_only(request), request.match_info["key"]))

        async def post_inbox_settings(request):
            payload = await body(request)
            return web.json_response(await actions.inbox_settings(await admin_only(request), payload))

        r.add_post("/api/admin/messages/{who}/reply", safe(post_message_reply))
        r.add_post("/api/admin/messages/{who}/done", safe(post_message_done))
        r.add_post("/api/admin/message/{key}/to-ticket", safe(post_message_to_ticket))
        r.add_post("/api/admin/inbox", safe(post_inbox_settings))
        r.add_post("/api/admin/cleanup/settings", safe(post_cleanup_settings))
        r.add_post("/api/admin/cleanup/scan", safe(post_cleanup_scan))

    if auth is None:
        r.add_get("/api/invite", lambda request: web.json_response({"valid": False}))
    if auth is not None:
        async def logout(request):
            actions.check_request(request) if actions else None
            return await auth.logout(request)
        r.add_post("/api/logout", logout)
        r.add_get("/auth/discord/login", auth.discord_login)
        r.add_get("/auth/discord/callback", auth.discord_callback)
        r.add_get("/auth/plex/login", auth.plex_login)
        r.add_get("/auth/plex/callback", auth.plex_callback)
        r.add_get("/auth/plex/go", auth.plex_go)
        r.add_post("/auth/plex/pin", auth.plex_pin)
        r.add_get("/auth/plex/check", auth.plex_check)
        r.add_get("/invite/{token}", auth.open_invite)
        r.add_get("/api/invite", safe(auth.invite_info))
        r.add_get("/auth/mobile/start", auth.mobile_start)
        r.add_get("/auth/mobile/return", auth.mobile_return)
        r.add_post("/api/invite/check", safe(auth.invite_check))
        r.add_post("/auth/mobile/token", auth.mobile_token)
        r.add_get("/auth/mobile/confirm", auth.mobile_confirm_page)
        r.add_post("/auth/mobile/confirm", auth.mobile_confirm)

    from core import notify

    async def mobile_info(request):
        # For the app to recognise a server that supports it: the version, the sign-ins
        # on offer, and its own address ("home", WEB_PUBLIC_URL), so a phone that signed
        # in through an old address (WEB_ALIASES) moves itself over.
        from core.config import public_url
        home = public_url(os.getenv("WEB_PUBLIC_URL", ""))
        return web.json_response({"version": MOBILE_API_VERSION, "auth": auth.app_methods() if auth is not None else [],
                                  "push": ["expo"] if not readonly and actions is not None and notify.app_push_on() else [],
                                  "home": home if home.startswith("https://") else None},
                                 headers={"Cache-Control": "no-store"})
    r.add_get("/api/mobile", mobile_info)

    # Images are for signed-in visitors only, and limited per address: each new one
    # is fetched and kept on disk (see portal/images.py).
    image_limit = Limiter(*IMAGE_TRIES)

    def own_picture(user: dict, request: web.Request) -> bool:
        """Whether a profile picture is the visitor's own (the one the site gave them, any version)."""
        mine = str((user.get("user") or {}).get("avatar") or "").split("/")
        return len(mine) == 5 and mine[:4] == request.path.split("/")[:4]

    def signed_in_image(handler, members_only: bool = True):
        async def guarded(request):
            if image_limit.over(request):
                return web.Response(status=429)
            user = await who(request)
            if not user or (members_only and not user.get("member")):
                return web.Response(status=403)
            if not user.get("member") and not own_picture(user, request):
                # Someone without access gets only their own picture. Anyone else's is
                # the same "not found" whether or not that person has access.
                return web.Response(status=404)
            return await handler(request)
        return guarded

    r.add_get("/img/tmdb/{size}/{file}", signed_in_image(images.tmdb))
    r.add_get("/img/ol/{file}", signed_in_image(images.openlibrary))
    r.add_get("/img/plex", signed_in_image(images.plex))
    r.add_get("/img/book/{id}", signed_in_image(images.book))
    r.add_get("/img/avatar/{uid}/{hash}.png", signed_in_image(images.avatar, members_only=False))

    async def plex_avatar(request):
        """Someone's plex.tv picture, through the image proxy. The address names the
        picture's version, so a changed picture is a new address; any version gets
        the current picture."""
        acct = request.match_info["acct"]
        thumb = await auth.plex_thumb(acct) if auth is not None and acct.isdigit() else None
        if not thumb:
            return web.Response(status=404)
        return await images._fetch(thumb, f"plexavatar/{acct}/{request.match_info['ver']}")
    r.add_get("/img/plex-avatar/{acct}/{ver}.png", signed_in_image(plex_avatar, members_only=False))
    r.add_get("/healthz", lambda request: web.json_response({"ok": True}))

    # ------------------------------------------------------------- static
    if dist and not _safe_dist(Path(dist)):
        logger.error(f"WEB_DIST={dist} would serve Plexbie's own settings; serving the API only. "
                     "Point it at the built website (web/dist).")
        dist = None
    if dist:
        from portal import pages
        root = Path(dist).resolve()
        index = pages.IndexPage(root / "index.html")
        operator = str(getattr(services.config, "site_operator", "") or "")
        contact = str(getattr(services.config, "site_contact", "") or "")

        def origin(request) -> str:
            """The site's own address for the page heads' link-preview card. The
            Host header, not X-Forwarded-Host: Cloudflare sets Host, while the other is
            whatever a visitor sends, and these answers can be cached for everyone."""
            from core.config import public_url
            from portal.auth import Auth
            public = public_url(os.getenv("WEB_PUBLIC_URL", ""))
            if public:
                return public
            scheme = Auth.base_url(request).split("://", 1)[0]
            return f"{scheme}://{request.host}"

        async def robots(request):
            return web.Response(text=pages.robots_txt(), headers={"Cache-Control": "public, max-age=3600"})

        async def static(request):
            rel = request.match_info.get("path", "")
            # Control characters and backslashes never belong in a page address, and a
            # browser drops a tab from "/\t/elsewhere" in a redirect: refused outright.
            if any(ord(c) < 0x20 or ord(c) == 0x7f or c == "\\" for c in rel):
                return web.Response(status=404)
            if rel == "index.html":
                raise web.HTTPMovedPermanently("/")
            if rel == "app" or rel.startswith("app/"):
                # The household's pages were under /app until they moved to the root:
                # links in Discord, emails and bookmarks still say so.
                query = f"?{request.query_string}" if request.query_string else ""
                raise web.HTTPMovedPermanently(f"/{rel[4:].strip('/')}{query}")
            target = (root / rel).resolve()
            hidden = any(part.startswith(".") for part in Path(rel).parts)
            if rel and not hidden and target.is_file() and root in target.parents:
                cache = ("public, max-age=31536000, immutable" if rel.startswith("assets/")
                         # The alerts worker and the install manifest: a change must
                         # reach phones at once, not after a cache's hours.
                         else "no-cache" if rel in ("sw.js", "manifest.webmanifest")
                         else "public, max-age=3600")
                return web.FileResponse(target, headers={"Cache-Control": cache})
            last = rel.rsplit("/", 1)[-1]
            if rel.startswith(("api/", "img/", "assets/")) or "." in last:
                return web.Response(status=404)       # a missing file is missing, not the website
            if rel.endswith("/") and rel.strip("/"):
                query = f"?{request.query_string}" if request.query_string else ""
                raise web.HTTPMovedPermanently(f"/{rel.strip('/')}{query}")
            from core.blocking import run_blocking
            status, page = await run_blocking(index.render, rel, origin(request), operator=operator, contact=contact)
            return web.Response(text=page, status=status, content_type="text/html", headers={"Cache-Control": "no-cache"})

        r.add_get("/robots.txt", robots)

        r.add_get("/{path:.*}", static)
    return app
