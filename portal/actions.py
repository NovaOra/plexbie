# path: portal/actions.py
"""Everything the website can change, routed through the bot's own code paths.

Each action calls the same function the matching Discord command or button
uses (post_media_request, decide_request, post_join_request, remove_plex_user,
the cleanup cog's config), so the website and Discord can never disagree about
what a request, a join or an exemption means.

Safety, in order: a signed-in session (portal.auth); the right role; a custom
header plus an Origin check against cross-site requests; per-person rate limits.
"""
import json
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional
from urllib.parse import quote, urlparse

from aiohttp import web

from core.blocking import run_blocking
from core.logging import get_logger
from database.kv_store import kv_get, kv_get_all, kv_set
from database.request_store import get_request
from portal.data import Data
from core.discord_lookup import admin_channel, home_guild


def discord_escape(text: str) -> str:
    """Someone's name in a Discord message, without its markdown."""
    import discord
    return discord.utils.escape_markdown(text or "")

logger = get_logger(__name__)

#: (max actions, per seconds) by kind.
LIMITS = {"request": (20, 3600), "join": (3, 86400), "admin": (120, 3600), "push_test": (6, 3600), "help": (5, 86400),
          "push": (20, 3600), "lookup": (600, 3600), "browse": (600, 3600), "prefs": (120, 3600)}

#: What the website shows when the cleanup cog won't touch its settings: it hasn't
#: been able to read the stored ones, or couldn't store a change.
CLEANUP_UNREADABLE = "Couldn't read the saved cleanup settings, so nothing was changed. Try again in a moment."
CLEANUP_UNSAVED = "Couldn't save the cleanup settings, so nothing was changed. Try again in a moment."
#: How a message about their ticket reached the member, as an admin is told it.
TOLD_BY = {"discord": "Discord DM", "push": "phone alert", "email": "email"}


def _flag(body: dict, key: str) -> bool:
    """A true/false setting, which must be sent as one: bool("false") is True, so a
    script sending text could turn media cleanup on, or allow @everyone pings."""
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise web.HTTPBadRequest(text=json.dumps({"error": f"{key} must be true or false."}),
                                 content_type="application/json")
    return value


def _whole_number(body: dict, key: str) -> int:
    """A number of days. Text that isn't one is the sender's mistake (400), not a
    service failure."""
    try:
        return int(body[key])
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=json.dumps({"error": f"{key} must be a whole number."}),
                                 content_type="application/json")


class _WebRequester:
    """Stands in for a discord.User when someone without Discord asks for something."""

    def __init__(self, name: str):
        self.id = None
        self.name = name
        self.mention = f"{name} (signed in with Plex)"


class Actions:
    def __init__(self, bot, services, data: Data, public_url: str):
        self.bot = bot
        self.services = services
        self.data = data
        self.config = services.config
        origin = urlparse(public_url or "")
        self.origin = f"{origin.scheme}://{origin.netloc}" if origin.netloc else None
        self.public_url = public_url or ""
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    # ------------------------------------------------------------- guards
    def check_request(self, request: web.Request) -> None:
        """Refuse cross-site writes: a header browsers won't send cross-origin, and the Origin."""
        if request.headers.get("X-Plexbie") != "1":
            raise web.HTTPForbidden(text='{"error":"Missing request header."}', content_type="application/json")
        origin = request.headers.get("Origin")
        allowed = {self.origin, f"{request.scheme}://{request.host}"}
        if origin and origin not in allowed:
            raise web.HTTPForbidden(text='{"error":"Wrong origin."}', content_type="application/json")

    def limit(self, who: str, kind: str) -> None:
        cap, window = LIMITS[kind]
        hits = self._hits[f"{kind}:{who}"]
        now = time.monotonic()
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= cap:
            raise web.HTTPTooManyRequests(text='{"error":"That\'s a lot at once. Try again later."}', content_type="application/json")
        hits.append(now)

    @staticmethod
    def actor(user: dict) -> str:
        return user["user"].get("name") or "an admin"

    async def _discord_user(self, discord_id: Optional[str]):
        if not discord_id:
            return None
        uid = int(discord_id)
        return self.bot.get_user(uid) or await self.bot.fetch_user(uid)

    # ----------------------------------------------------------- requests
    async def _video_media(self, kind: str, tid: str) -> dict:
        r = await self.data._seerr(f"{'movie' if kind == 'movie' else 'tv'}/{tid}", ttl=300)
        media = {
            "id": int(tid),
            "media_type": "movie" if kind == "movie" else "tv",
            "overview": r.get("overview") or "",
            "poster_path": r.get("posterPath"),
            "backdrop_path": r.get("backdropPath"),
            "vote_average": r.get("voteAverage"),
        }
        if kind == "movie":
            media.update(title=r.get("title"), release_date=r.get("releaseDate"))
        else:
            media.update(name=r.get("name"), first_air_date=r.get("firstAirDate"))
        return media, r

    async def _book(self, work_id: str, fmt: str) -> dict:
        ol = self.services.openlibrary
        work = await self.data.cache.get(f"openlibrary:work:{work_id}", 3600,
                                         lambda: ol.get(f"works/{quote(work_id)}.json"))
        author = "Unknown Author"
        authors = work.get("authors") or []
        if authors:
            key = (authors[0].get("author") or {}).get("key")
            if key:
                try:
                    found = await self.data.cache.get(f"openlibrary:author:{key}", 86400, lambda: ol.get(f"{key}.json"))
                    author = (found or {}).get("name") or author
                except Exception:
                    pass
        cover = (work.get("covers") or [None])[0]
        desc = work.get("description")
        return {
            "title": work.get("title") or "Untitled",
            "author": author,
            "year": None,
            "cover_url": f"https://covers.openlibrary.org/b/id/{cover}-M.jpg" if cover else None,
            "isbn": None,
            "open_library_key": f"/works/{work_id}",
            "description": (desc.get("value") if isinstance(desc, dict) else desc or "")[:600],
            "request_format": fmt,
        }

    @staticmethod
    def pick_seasons(seasons: list, pick: Any) -> tuple:
        """Which seasons to send to the admin card, from what's still missing.

        Only "none" and "partial" seasons can be asked for; anything on Plex,
        already requested or not aired yet is left out, so asking for "all" on a
        show with season 1 already there asks for 2 onwards. Returns
        (seasons, monitor) as post_media_request takes them, or raises 409.
        """
        open_ = [s["n"] for s in seasons if s["status"] in ("none", "partial")]
        aired = [s["n"] for s in seasons if s["status"] != "upcoming"]
        if not open_:
            raise web.HTTPConflict(text='{"error":"Every season is already on Plex or requested."}', content_type="application/json")
        if pick == "latest":
            newest = max(aired) if aired else None
            if newest not in open_:
                raise web.HTTPConflict(text='{"error":"The latest season is already on Plex or requested."}', content_type="application/json")
            return [newest], True
        if isinstance(pick, list):
            chosen = sorted({int(n) for n in pick if str(n).lstrip("-").isdigit()} & set(open_))
            if not chosen:
                raise web.HTTPConflict(text='{"error":"Those seasons are already on Plex or requested."}', content_type="application/json")
            return chosen, False
        return ("all" if sorted(open_) == sorted(aired) else open_), False

    async def create_request(self, user: dict, body: dict) -> dict:
        from plugins.media_requests.cog import AdminChannelUnavailable, post_book_request, post_media_request

        kind, tid = body.get("kind"), str(body.get("id") or "")
        who = user.get("discordId") or f"plex:{user.get('plexAccountId')}"
        self.limit(who, "request")
        requester = await self._discord_user(user.get("discordId")) or _WebRequester(user["user"]["name"])
        extra = None if user.get("discordId") else {
            "plex_account_id": user.get("plexAccountId"), "requester_name": user["user"]["name"], "via": "website"}

        if kind != "tv":   # TV is checked season by season below, so more seasons can be asked for later
            mine = await self.data.my_requests(int(user["discordId"]) if user.get("discordId") else None, user.get("plexAccountId"))
            if any(m["title"]["id"] == tid and m["stage"] not in ("declined", "available") for m in mine):
                raise web.HTTPConflict(text='{"error":"You already asked for this one."}', content_type="application/json")

        try:
            if kind in ("movie", "tv"):
                if not tid.isdigit():
                    raise web.HTTPBadRequest(text='{"error":"Unknown title."}', content_type="application/json")
                media, detail = await self._video_media(kind, tid)
                status = (detail.get("mediaInfo") or {}).get("status")
                if status == 6:                 # on Seerr's blocklist
                    raise web.HTTPConflict(text='{"error":"An admin has blocked this title, so it can\'t be requested."}',
                                           content_type="application/json")
                if kind == "movie" and status in (2, 3, 4, 5):
                    raise web.HTTPConflict(text='{"error":"It\'s already on Plex or already requested."}', content_type="application/json")
                seasons, monitor = None, False
                if kind == "tv":
                    seasons, monitor = self.pick_seasons(await self.data.tv_seasons(tid, detail), body.get("seasons", "all"))
                message_id = await post_media_request(self.bot, self.services, requester, media, seasons=seasons, monitor=monitor, extra=extra)
            elif kind in ("audiobook", "ebook"):
                fmt = body.get("format") if body.get("format") in ("ebook", "audiobook", "both") else kind
                book = await self._book(tid, fmt)
                message_id = await post_book_request(self.bot, self.services, requester, book, extra=extra)
            else:
                raise web.HTTPBadRequest(text='{"error":"Unknown kind."}', content_type="application/json")
        except AdminChannelUnavailable:
            raise web.HTTPServiceUnavailable(text='{"error":"Requests are switched off right now. Tell an admin."}', content_type="application/json")

        logger.info(f"Web request {kind}:{tid} by {self.actor(user)} -> message {message_id}")
        mine = await self.data.my_requests(int(user["discordId"]) if user.get("discordId") else None, user.get("plexAccountId"))
        ours = [m for m in mine if m["title"]["id"] == tid]
        return max(ours, key=lambda m: m["slot"]) if ours else {"slot": 0, "stage": "requested"}

    # --------------------------------------------------------------- join
    async def join(self, user: dict, body: dict) -> dict:
        from plugins.user_invites.cog import post_join_request, save_join_request
        from utils.validators import validate_email

        if user.get("member"):
            raise web.HTTPConflict(text='{"error":"You\'re already on Plex."}', content_type="application/json")
        who = user.get("discordId") or f"plex:{user.get('plexAccountId')}"
        self.limit(who, "join")
        if user["user"].get("via") == "plex":
            # Invite-only without Discord: a Plex sign-in can't ask, only use an
            # admin's invite link (portal.invites), so strangers can't queue up.
            raise web.HTTPForbidden(text='{"error":"Joining with Plex is by invite only. Ask whoever runs the server for an invite link."}', content_type="application/json")
        else:
            if not user.get("inGuild"):
                raise web.HTTPForbidden(text='{"error":"Join our Discord server first, or sign in with Plex instead."}', content_type="application/json")
            email = str(body.get("email") or "").strip()
            if not validate_email(email):
                raise web.HTTPBadRequest(text='{"error":"That doesn\'t look like an email address."}', content_type="application/json")
            requester = await self._discord_user(user["discordId"])
            await save_join_request(int(user["discordId"]), email)
            await post_join_request(self.bot, self.services, requester, email)
        logger.info(f"Web join request by {self.actor(user)}")
        return {"ok": True}

    # ------------------------------------------------------------ invites
    async def create_invite(self, user: dict, body: dict, invites, base_url: str) -> dict:
        self.limit(user["user"]["id"], "admin")
        try:
            token, invite = await invites.create(label=str(body.get("label") or ""), email=body.get("email"),
                                                 days=int(body.get("days") or 7), actor=self.actor(user))
        except (TypeError, ValueError) as e:
            raise web.HTTPBadRequest(text=json.dumps({"error": str(e) or "Check the details and try again."}), content_type="application/json")
        # The only time the link exists in readable form: shown once, never stored.
        return {"url": f"{(self.origin or base_url).rstrip('/')}/invite/{token}", "invite": invite}

    # ------------------------------------------------ Plex invites on plex.tv
    async def plex_invites(self, user: dict) -> list:
        """Unaccepted Plex invites, with who each is for when Plexbie knows."""
        from core import plex_invites
        from core.plex_account import can_sign_in
        if not can_sign_in(self.config):
            return []
        rows = await run_blocking(plex_invites.list_pending, self.config)
        known = await self._joiners_by_email()
        return [{**r, "who": known.get(r["email"].lower())} for r in rows]

    async def _joiners_by_email(self) -> dict:
        from sqlalchemy import select
        from database.session import get_session
        from plugins.user_mgmt.models import PlexUser
        from plugins.user_invites.cog import INVITES_NAMESPACE, WEB_JOINS_NAMESPACE
        out = {}
        for namespace in (INVITES_NAMESPACE, WEB_JOINS_NAMESPACE):
            for rec in (await kv_get_all(namespace)).values():
                if isinstance(rec, dict) and rec.get("email"):
                    out.setdefault(rec["email"].lower(), rec.get("username") or rec.get("plex_name"))
        async with get_session() as session:
            for row in (await session.execute(select(PlexUser).where(PlexUser.plex_email.isnot(None)))).scalars():
                out.setdefault(row.plex_email.lower(), row.discord_username or row.plex_username)
        return out

    async def _tell_invite_moved(self, who: dict, old: str, new: str) -> None:
        """The person hears their invite went to a corrected address: a Discord DM, or for
        someone who joined without Discord a phone alert or email (to the new address)."""
        text = (f"📬 Your Plex invite was sent again, to **{new}** this time (it had gone to {old}). "
                "Check that inbox and its spam folder, or open Plex signed in with that email to accept it.")
        from core.admin_mirror import dm_user_id
        if await dm_user_id(self.bot, self.services, who.get("discord_id"), context=f"Plex invite moved to {new}", content=text):
            return
        try:
            from core.notify import notify_member, plain
            await notify_member(self.services, title="Your Plex invite was sent again", body=plain(text),
                                plex_name=who.get("name"), email=new, context=f"Plex invite moved to {new}")
        except Exception as e:
            logger.info(f"Couldn't tell {who.get('name') or new} about the new invite: {e}")

    async def plex_invite_cancel(self, user: dict, body: dict) -> dict:
        from core import plex_invites
        self.limit(user["user"]["id"], "admin")
        email = str(body.get("email") or "").strip()
        if not email:
            return {"ok": False, "message": "Which invite?"}
        try:
            if not await run_blocking(plex_invites.cancel, self.config, email):
                return {"ok": False, "message": "Plex has no unaccepted invite to that address any more."}
        except Exception as e:
            logger.warning(f"Cancelling the Plex invite to {email} failed: {e}")
            return {"ok": False, "message": f"Plex didn't take that back: {str(e).split(';')[0][:120]}"}
        logger.info(f"{self.actor(user)} cancelled the Plex invite to {email} on the website")
        return {"ok": True, "message": f"Plex invite to {email} cancelled."}

    async def plex_invite_change(self, user: dict, body: dict) -> dict:
        """Send an invite to the right address instead of the wrong one, and fix the records."""
        from core import plex_invites
        from plugins.user_invites.cog import correct_invite_email
        self.limit(user["user"]["id"], "admin")
        old, new = str(body.get("email") or "").strip(), str(body.get("new") or "").strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", new):
            return {"ok": False, "message": "That doesn't look like an email address."}
        if new.lower() == old.lower():
            return {"ok": False, "message": "That's the same address."}
        if self.services.plex_server is None:
            return {"ok": False, "message": "Plex isn't connected right now."}
        try:
            await run_blocking(plex_invites.resend, self.config, self.services.plex_server, old, new)
        except Exception as e:
            text = str(e)
            if "already sharing" in text:
                return {"ok": False, "message": f"Your server is already shared with {new}."}
            return {"ok": False, "message": f"Plex didn't take that: {text[:160]}"}
        who = await correct_invite_email(old, new)
        logger.info(f"{self.actor(user)} moved the Plex invite from {old} to {new} on the website")
        await self._tell_invite_moved(who, old, new)
        return {"ok": True, "message": f"Invite sent to {new}" + (f" for {who['name']}" if who.get("name") else "") + "."}

    async def delete_invite(self, user: dict, key: str, invites) -> dict:
        self.limit(user["user"]["id"], "admin")
        if not re.fullmatch(r"[0-9a-f]{64}", key or ""):
            raise web.HTTPNotFound(text='{"error":"No such invite."}', content_type="application/json")
        return await invites.delete(key, self.actor(user))

    async def renew_invite(self, user: dict, key: str, invites, base_url: str) -> dict:
        self.limit(user["user"]["id"], "admin")
        if not re.fullmatch(r"[0-9a-f]{64}", key or ""):
            raise web.HTTPNotFound(text='{"error":"No such invite."}', content_type="application/json")
        try:
            token, invite = await invites.renew(key, self.actor(user))
        except ValueError as e:
            raise web.HTTPNotFound(text=json.dumps({"error": str(e)}), content_type="application/json")
        return {"url": f"{(self.origin or base_url).rstrip('/')}/invite/{token}", "invite": invite}

    async def revoke_invite(self, user: dict, key: str, invites) -> dict:
        self.limit(user["user"]["id"], "admin")
        if not re.fullmatch(r"[0-9a-f]{64}", key or ""):
            raise web.HTTPNotFound(text='{"error":"No such invite."}', content_type="application/json")
        return await invites.revoke(key, self.actor(user))

    # -------------------------------------------------------------- admin
    async def decide_request(self, user: dict, message_id: str, approve: bool) -> dict:
        from plugins.media_requests.cog import decide_request
        self.limit(user["user"]["id"], "admin")
        if not message_id.isdigit() or await get_request(int(message_id)) is None:
            raise web.HTTPNotFound(text='{"error":"No such request."}', content_type="application/json")
        result = await decide_request(self.bot, self.services, int(message_id), approve, self.actor(user))
        return result

    async def decide_join(self, user: dict, message_id: str, approve: bool) -> dict:
        from plugins.user_invites.cog import decide_join_request
        self.limit(user["user"]["id"], "admin")
        if not message_id.isdigit():
            raise web.HTTPNotFound(text='{"error":"No such join request."}', content_type="application/json")
        return await decide_join_request(self.bot, self.services, int(message_id), approve, self.actor(user))

    async def exempt(self, user: dict, body: dict) -> dict:
        self.limit(user["user"]["id"], "admin")
        rk = str(body.get("ratingKey") or "")
        keep = _flag(body, "keep")
        cog = self.bot.get_cog("MediaCleanupCog")
        if not cog or not rk.isdigit():
            raise web.HTTPBadRequest(text='{"error":"Cleanup isn\'t available."}', content_type="application/json")
        # The countdown can take a while, so it comes first: nothing may wait between
        # picking up cog.config and saving it. A failed save elsewhere during that
        # wait has the next load swap in a new dict, and an exemption added to the old
        # one would be reported as kept but never stored.
        clock = await self.data.countdown() if keep else {}
        if not await cog.load_data():
            return {"ok": False, "message": CLEANUP_UNREADABLE}
        items = cog.config.setdefault("exempt_items", {})
        if keep:
            info = clock.get(rk) or {}
            items[rk] = {
                "title": info.get("title") or "Unknown",
                "type": info.get("type"),
                "year": None,
                "added_at": None,
                "exempted_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            items.pop(rk, None)
        if not await cog.save_config():
            return {"ok": False, "message": CLEANUP_UNSAVED}
        self.data.cache.drop("cleanup:countdown")
        logger.info(f"{self.actor(user)} {'exempted' if keep else 'un-exempted'} {rk} from cleanup on the website")
        return {"ok": True, "message": "Kept permanently." if keep else "No longer kept."}

    def _cleanup_cog(self):
        cog = self.bot.get_cog("MediaCleanupCog") if self.bot else None
        if not cog:
            raise web.HTTPServiceUnavailable(text='{"error":"Cleanup isn\'t running."}', content_type="application/json")
        return cog

    async def cleanup_settings(self, user: dict, body: dict) -> dict:
        """Change what the Discord cleanup panel and /cleanup config change, with the same limits."""
        self.limit(user["user"]["id"], "admin")
        cog = self._cleanup_cog()
        if not await cog.load_data():
            return {"ok": False, "message": CLEANUP_UNREADABLE}
        # Checked in full before cog.config changes: a refusal halfway through would
        # otherwise leave the earlier fields (live mode, say) in memory, where the
        # daily scan uses them and the next unrelated save stores them.
        updates, changes = {}, []
        if "enabled" in body:
            updates["enabled"] = _flag(body, "enabled")
            changes.append(f"cleanup {'on' if updates['enabled'] else 'off'}")
        if "practice" in body:
            updates["dry_run"] = _flag(body, "practice")
            changes.append("practice mode" if updates["dry_run"] else "LIVE mode")
        if "inactivityDays" in body:
            days = _whole_number(body, "inactivityDays")
            if not 30 <= days <= 3650:
                raise web.HTTPBadRequest(text='{"error":"Keep it between 30 and 3650 days, for safety."}', content_type="application/json")
            updates["inactivity_days"] = days
            changes.append(f"{days} days")
        if "warnDaysBefore" in body:
            warn = _whole_number(body, "warnDaysBefore")
            if not 1 <= warn < updates.get("inactivity_days", cog.config["inactivity_days"]):
                raise web.HTTPBadRequest(text='{"error":"The warning has to come at least a day before removal."}', content_type="application/json")
            updates["notify_days_before"] = warn
            changes.append(f"warn {warn} days before")
        if "excludedLibraries" in body:
            libs = body["excludedLibraries"]
            if not isinstance(libs, list) or not all(isinstance(x, str) for x in libs):
                raise web.HTTPBadRequest(text='{"error":"Unknown libraries."}', content_type="application/json")
            updates["exclude_libraries"] = sorted(set(libs))
            changes.append(f"skipping {', '.join(updates['exclude_libraries']) or 'nothing'}")
        if "channelId" in body:
            cid = body["channelId"]
            updates["notification_channel_id"] = int(cid) if cid and str(cid).isdigit() else None
            changes.append("notification channel")
        if not changes:
            return {"ok": False, "message": "Nothing to change."}
        cog.config.update(updates)
        if not await cog.save_config():
            return {"ok": False, "message": CLEANUP_UNSAVED}
        self.data.cache.drop("cleanup:countdown")
        self.data.cache.drop("cleanup:config")
        logger.info(f"{self.actor(user)} changed cleanup settings on the website: {changes}")
        return {"ok": True, "message": "Saved: " + ", ".join(changes) + "."}

    async def cleanup_scan(self, user: dict) -> dict:
        from plugins.media_cleanup.cog import CleanupSettingsUnavailable
        self.limit(user["user"]["id"], "admin")
        cog = self._cleanup_cog()
        try:
            result = await cog.scan_now()
        except CleanupSettingsUnavailable:
            return {"ok": False, "message": "Couldn't read the saved cleanup settings, so nothing was scanned. Try again in a moment."}
        if result is None:
            return {"ok": False, "message": "Plex isn't reachable right now, so nothing was scanned."}
        if result.get("skipped") == "disabled":
            return {"ok": False, "message": "Cleanup is off, so nothing was scanned. Turn it on first."}
        if result.get("skipped"):
            return {"ok": False, "message": "A cleanup scan is already running. Try again when it's done."}
        self.data.cache.drop("cleanup:countdown")
        n, d, would = len(result["notify"]), len(result["deleted"]), result["dry_run"]
        kept = result.get("kept", 0)
        logger.info(f"{self.actor(user)} ran a cleanup scan on the website")
        return {"ok": True, "message": (f"Scan done: {n} title{'s' if n != 1 else ''} in the warning window, "
                                        f"{d} {'would have been' if would else 'were'} removed."
                                        + (f" {kept} couldn't be removed and will be tried again at the next check."
                                           if kept else ""))}

    async def link_candidates(self, user: dict) -> dict:
        """Server members not yet linked to a Plex account, for the "Link Discord" picker."""
        from sqlalchemy import select
        from database.session import get_session
        from plugins.user_mgmt.models import PlexUser
        guild = home_guild(self.bot, self.config)
        if not guild:
            return {"discord": []}
        async with get_session() as session:
            linked = {r[0] for r in (await session.execute(select(PlexUser.discord_id).where(PlexUser.discord_id.isnot(None)))).all()}
        members = [{"id": str(m.id), "name": m.display_name, "username": m.name}
                   for m in guild.members if not m.bot and m.id not in linked]
        members.sort(key=lambda m: m["name"].casefold())
        return {"discord": members}

    async def link_person(self, user: dict, body: dict) -> dict:
        self.limit(user["user"]["id"], "admin")
        cog = self.bot.get_cog("UserMgmtCog") if self.bot else None
        name, did = str(body.get("plexName") or ""), str(body.get("discordId") or "")
        if not cog or not name or not did.isdigit():
            raise web.HTTPBadRequest(text='{"error":"Pick a Discord member to link."}', content_type="application/json")
        ok, message = await cog.link_accounts(int(did), name)
        self.data.cache.drop("auth:plex-access")
        return {"ok": ok, "message": message}

    async def match_person(self, user: dict, body: dict) -> dict:
        """"This is Plex account X": fix a tracking row that can't be matched on its own."""
        from portal.admin import Admin
        from plugins.user_mgmt.cog import match_account
        self.limit(user["user"]["id"], "admin")
        name, title = str(body.get("plexName") or ""), str(body.get("account") or "")
        accounts = await Admin(self.data, bot=self.bot).accounts() or []
        same = [a for a in accounts if a["title"] == title and not a["owner"]]
        if not name or not same:
            return {"ok": False, "message": "Pick one of the Plex accounts your server is shared with."}
        if len(same) > 1:
            return {"ok": False, "message": f"More than one Plex account is called {title}, so Plexbie can't tell which."}
        account = same[0]
        try:
            await match_account(name, account)
        except (LookupError, ValueError) as e:
            return {"ok": False, "message": str(e)}
        self.data.cache.drop("auth:plex-access")
        logger.info(f"{self.actor(user)} matched {name} to Plex account {title} on the website")
        return {"ok": True, "message": f"{name} is {title} on Plex now. Watch time and reminders follow that account."}

    async def rename_person(self, user: dict, body: dict) -> dict:
        """Manage → People → Rename: the name shown everywhere, Tautulli included."""
        from plugins.user_mgmt.cog import rename_person
        self.limit(user["user"]["id"], "admin")
        name, new = str(body.get("plexName") or ""), str(body.get("name") or "")
        try:
            tautulli_note = await rename_person(self.services, name, new)
        except (LookupError, ValueError) as e:
            return {"ok": False, "message": str(e)}
        self.data.cache.drop("leaderboard")
        logger.info(f"{self.actor(user)} renamed {name} to {new!r} on the website")
        return {"ok": True, "message": f"Shown as {' '.join(new.split())} from now on. {tautulli_note}"}

    async def keep_person(self, user: dict, body: dict) -> dict:
        """The "Never remove" switch on a person."""
        from plugins.user_mgmt.cog import set_never_remove
        self.limit(user["user"]["id"], "admin")
        name, keep = str(body.get("plexName") or ""), _flag(body, "keep")
        try:
            await set_never_remove(name, keep)
        except LookupError as e:
            return {"ok": False, "message": str(e)}
        logger.info(f"{self.actor(user)} set never-remove {'on' if keep else 'off'} for {name} on the website")
        return {"ok": True, "message": f"{name} will never be removed for not watching." if keep
                else f"{name} is back on the inactivity check, with the usual warning first."}

    async def unlink_person(self, user: dict, body: dict) -> dict:
        self.limit(user["user"]["id"], "admin")
        cog = self.bot.get_cog("UserMgmtCog") if self.bot else None
        name = str(body.get("plexName") or "")
        if not cog or not name:
            raise web.HTTPBadRequest(text='{"error":"User management isn\'t available."}', content_type="application/json")
        ok, message = await cog.unlink_account(name)
        return {"ok": ok, "message": message}

    # ------------------------------------------------------ help requests
    async def ask_help(self, user: dict, request_key: str, body: dict) -> dict:
        """Someone says their request went wrong: tell the admins everything at once."""
        from portal import help as helpdesk
        if not str(request_key).isdigit():
            raise web.HTTPNotFound(text='{"error":"No such request."}', content_type="application/json")
        rec = await get_request(int(request_key))
        if not rec or not helpdesk.owns(rec, user):
            raise web.HTTPNotFound(text='{"error":"No such request."}', content_type="application/json")
        reason = body.get("reason") if body.get("reason") in helpdesk.REASONS else "other"
        note = str(body.get("note") or "").strip()
        if reason == "other" and not note:
            raise web.HTTPBadRequest(text='{"error":"Say a little about what\'s wrong."}', content_type="application/json")
        if await helpdesk.open_for({str(request_key)}):
            raise web.HTTPConflict(text='{"error":"You already asked for help with this one. An admin will be in touch."}', content_type="application/json")
        self.limit(user.get("discordId") or f"plex:{user.get('plexAccountId')}", "help")

        mine = await self.data.my_requests(int(user["discordId"]) if user.get("discordId") else None, user.get("plexAccountId"))
        row = next((m for m in mine if str(m.get("id")) == str(request_key)), None) or {}
        progress = row.get("progress") or {}
        status_now = ", ".join(x for x in (row.get("stage", "").capitalize(), progress.get("detail"),
                                            f"{progress['percent']}%" if progress.get("percent") is not None else None) if x)
        media = rec.get("media") or {}
        title = media.get("title") or media.get("name") or "Untitled"
        seasons = rec.get("seasons")
        h = await helpdesk.create(request_key=str(request_key), slot=row.get("slot") or 0, title=title,
                                  kind=row.get("title", {}).get("kind") or media.get("media_type") or "",
                                  seasons=seasons, user=user, reason=helpdesk.REASONS[reason], note=note,
                                  status_now=status_now or "Unknown")
        from core.message_log import record_from_ticket
        await record_from_ticket(h, text=f"{h['reason']}. {note}".strip() if note else h["reason"], title=f"Something wrong with {title}",
                                 source=str(body.get("source") or "web"), context="Something wrong?")
        try:
            await self._tell_admins_about_help(h)
        except Exception as e:   # the request is saved and shows on Manage either way
            logger.warning(f"Help request {h['id']} saved, but telling the admins failed: {type(e).__name__}: {e}")
        return {"ok": True, "message": "Sent. An admin will take a look and get back to you.", "help": {"id": h["id"], "reason": h["reason"]}}

    async def _tell_admins_about_help(self, h: dict) -> None:
        import discord
        from core import notify
        seasons = h.get("seasons")
        what = f"{h['title']}" + (f" (season {', '.join(str(n) for n in seasons)})" if isinstance(seasons, list) and seasons else "")
        number = f"No. {int(h['slot']):04d}" if h.get("slot") else "A request"
        channel = admin_channel(self.bot, self.config)
        if channel:
            opened = h.get("opened_by")
            embed = discord.Embed(title=f"{'🛠️ Ticket opened' if opened else '🆘 Help asked'} on {number}: {what}", color=discord.Color.orange(),
                                  description=f"**{h['reason']}**" + (f"\n> {h['note']}" if h.get("note") else ""))
            if opened:
                embed.add_field(name="Opened by", value=opened, inline=True)
                embed.add_field(name="Requested by", value=h["who"], inline=True)
            else:
                embed.add_field(name="From", value=h["who"] + (f" (<@{h['discord_id']}>)" if h.get("discord_id") else " (Plex sign-in)"), inline=True)
            embed.add_field(name="Plexbie sees", value=h.get("status_then") or "Unknown", inline=True)
            view = None
            if h.get("offer") == "name":
                from portal.help_view import HelpByNameView, footer
                embed.set_footer(text=footer(h["id"]))
                view = HelpByNameView()
            else:
                embed.set_footer(text="Work on it on the website: Manage → Tickets (the title links there)")
            if self.public_url:
                embed.url = self.ticket_link(h["id"])
            try:
                await channel.send(embed=embed, **({"view": view} if view else {}))
            except Exception as e:
                logger.warning(f"Could not post a help request to the admin channel: {e}")
        guild = home_guild(self.bot, self.config)
        ids = {str(self.config.bot_owner_id)} if self.config.bot_owner_id else set()
        if guild:
            for m in guild.members:
                roles = {r.id for r in m.roles}
                if m.guild_permissions.administrator or (self.config.admin_role_id and self.config.admin_role_id in roles):
                    ids.add(str(m.id))
        from database.kv_store import kv_get
        from portal.auth import OWNER_ID
        owner_id = await kv_get(*OWNER_ID)                # the Plex owner's account, as plex.tv said
        await notify.push_to_admins(discord_ids=ids, plex_account_ids={owner_id} if owner_id else set(),
                                    title=f"{'Ticket opened' if h.get('opened_by') else 'Help asked'} on {number}",
                                    body=f"{h.get('opened_by') or h['who']}: {h['reason']}. {what}",
                                    url="/app/manage?tab=requests", tag=f"help-{h['id']}")

    async def help_search(self, user: dict, hid: str, by_episode: bool = False) -> dict:
        """Search again for a help request's title; by_episode skips the season search."""
        from portal import help as helpdesk
        self.limit(user["user"]["id"], "admin")
        h = next((x for x in await helpdesk.all_help() if x["id"] == hid), None)
        if not h:
            raise web.HTTPNotFound(text='{"error":"No such help request."}', content_type="application/json")
        actor = self.actor(user)

        async def nothing(seasons):
            await helpdesk.note_search(hid, "Plexbie", f"Nothing found for season {', '.join(map(str, seasons))}, "
                                                       "as a whole or episode by episode")

        async def movie(text, found):
            await helpdesk.note_search(hid, "Plexbie", text)
        try:
            if by_episode:
                message = await helpdesk.search_episodes(self.services, h["request"], on_nothing=nothing)
            else:
                message = await helpdesk.search_again(self.services, h["request"], on_nothing=nothing, on_movie=movie)
        except LookupError as e:
            return {"ok": False, "message": str(e)}
        await helpdesk.note_search(hid, actor, message)
        self.data.cache.drop("sonarr:queue")
        self.data.cache.drop("radarr:queue")
        return {"ok": True, "message": message}

    async def help_by_name(self, user: dict, hid: str) -> dict:
        """Search by name (NZBHydra) for a help request's title. It runs in the
        background and reports on the help request and in the admin channel,
        resolving the help request when it grabs something."""
        from portal import help as helpdesk
        self.limit(user["user"]["id"], "admin")
        if not any(x["id"] == hid for x in await helpdesk.all_help()):
            raise web.HTTPNotFound(text='{"error":"No such help request."}', content_type="application/json")
        channel = admin_channel(self.bot, self.config)

        async def tell(text):
            if channel:
                await channel.send(text)
        try:
            message = await helpdesk.name_search_for_help(self.services, hid, self.actor(user), tell=tell)
        except LookupError as e:
            return {"ok": False, "message": str(e)}
        self.data.cache.drop("sonarr:queue")
        self.data.cache.drop("radarr:queue")
        return {"ok": True, "message": message}

    async def help_resolve(self, user: dict, hid: str, body: dict) -> dict:
        from portal import help as helpdesk
        self.limit(user["user"]["id"], "admin")
        reply = str(body.get("reply") or "").strip()
        h = await helpdesk.resolve(hid, self.actor(user), reply)
        if not h:
            raise web.HTTPNotFound(text='{"error":"No such help request."}', content_type="application/json")
        if h.get("already"):
            return {"ok": False, "message": "Someone already resolved this one."}
        if h.get("quiet"):
            logger.info(f"{self.actor(user)} resolved help {hid} on {h['title']} (an admin's own ticket; nobody told)")
            return {"ok": True, "message": "Resolved."}
        text = reply or f"An admin looked into your request for {h['title']} and it should be sorted now."
        how = await self.tell_member(h, text, context=f"help resolved for {h['title']}", reply_button=False,
                                     by=self.actor(user) if reply else None, entry=helpdesk.last_reply(h) if reply else None)
        logger.info(f"{self.actor(user)} resolved help {hid} on {h['title']}, told: {how or 'nobody'}")
        if how:
            return {"ok": True, "told": True, "message": f"Resolved, and {h['who']} has been told ({TOLD_BY[how]})."}
        return {"ok": True, "told": False, "message": f"Resolved, but {self._not_told(h)}"}

    async def tell_member(self, h: dict, text: str, *, context: str, reply_button: bool = True, by: Optional[str] = None,
                          entry: Optional[str] = None) -> Optional[str]:
        """A message to the member a ticket is about: a Discord DM (with a Reply button, so
        they can answer on the ticket from Discord), else (no Discord, or the DM didn't
        arrive) a phone/browser alert or email. `by`: the admin who wrote it, shown as
        "Message from …" under the request's title. Returns how it reached them ("discord",
        "push" or "email"), or None: it reached nobody, which goes on the ticket's thread, and
        the thread's reply entry it was (`entry`, its id) is marked "missed"."""
        if h.get("discord_id") and self.bot:
            from core.admin_mirror import dm_user_id
            from portal.ticket_view import reply_embed, reply_view
            view = reply_view(h["id"]) if reply_button and h.get("id") else None
            if await dm_user_id(self.bot, self.services, h["discord_id"], context=context, embed=reply_embed(h, text, by, reply=bool(view)),
                                view=view, sent_by=by, own_fallback=True):
                return "discord"
        from core.notify import notify_member
        how = await notify_member(self.services, title=f"About your request: {h['title']}", body=f"Message from {by}: {text}" if by else text,
                                  url="/schedule", plex_account_id=h.get("plex_account_id"), plex_name=h.get("plex_name"),
                                  discord_id=h.get("discord_id"), context=context, sent_by=by)
        if how in TOLD_BY:
            return how
        if h.get("id"):
            from portal import help as helpdesk
            await helpdesk.not_reached(h["id"], entry, f"This didn't reach {h['who']}: {self._why_not(h)}")
        return None

    def _why_not(self, h: dict) -> str:
        # "Reached them" rather than "they have": notify_member can't tell nowhere to send
        # apart from a send that failed (an email server that refused it).
        if h.get("discord_id"):
            dm = "Plexbie couldn't DM them on Discord" if self.bot else "Plexbie isn't connected to Discord"
            return f"{dm}, and no phone alert or email reached them either."
        return "no phone alert or email reached them."

    def _not_told(self, h: dict) -> str:
        """The end of an answer whose message reached nobody."""
        return f"it didn't reach {h['who']}: {self._why_not(h)} Tell them another way."

    # ------------------------------------------------- all requests (admin)
    async def _request_for_admin(self, key: str) -> dict:
        if not str(key).isdigit():
            raise web.HTTPNotFound(text='{"error":"No such request."}', content_type="application/json")
        rec = await get_request(int(key))
        if not rec:
            raise web.HTTPNotFound(text='{"error":"No such request."}', content_type="application/json")
        return rec

    async def _note_activity(self, key: str, by: str, did: str) -> None:
        from portal.admin import ACTIVITY_NAMESPACE
        log = (await kv_get(ACTIVITY_NAMESPACE, str(key))) or []
        log = (log if isinstance(log, list) else [])[-29:] + [{"at": datetime.now(timezone.utc).isoformat(), "by": by, "did": did}]
        await kv_set(ACTIVITY_NAMESPACE, str(key), log)

    async def admin_ticket(self, user: dict, key: str, body: dict) -> dict:
        """An admin opens a ticket on someone's request (Manage → All requests). It goes on
        Manage → Tickets like any other; the person who asked hears about it only if body.tell."""
        from portal import help as helpdesk
        from portal.admin import Admin
        self.limit(user["user"]["id"], "admin")
        rec = await self._request_for_admin(key)
        note = str(body.get("note") or "").strip()
        if not note:
            raise web.HTTPBadRequest(text='{"error":"Write what you found, so the ticket says what\'s wrong."}', content_type="application/json")
        if await helpdesk.open_for({str(key)}):
            raise web.HTTPConflict(text='{"error":"There\'s already an open ticket on this request. Add to it on Manage → Tickets."}', content_type="application/json")
        records = await self.data._slots()
        admin = Admin(self.data, self.bot)
        who = Admin._who(await admin._names(), rec)
        row = await self.data.request_row(str(key), records.get(str(key)) or {**rec, "_slot": 0}, {}, {})
        progress = row.get("progress") or {}
        status_now = ", ".join(x for x in (row.get("stage", "").capitalize(), progress.get("detail"),
                                            f"{progress['percent']}%" if progress.get("percent") is not None else None) if x)
        person = {"user": {"name": who}, "discordId": str(rec["user_id"]) if rec.get("user_id") else None,
                  "plexAccountId": rec.get("plex_account_id"), "plexName": rec.get("requester_name")}
        tell = bool(body.get("tell"))
        media = rec.get("media") or {}
        title = media.get("title") or media.get("name") or "Untitled"
        h = await helpdesk.create(request_key=str(key), slot=row.get("slot") or 0, title=title,
                                  kind=row.get("title", {}).get("kind") or media.get("media_type") or "",
                                  seasons=rec.get("seasons"), user=person, reason="Opened by an admin", note=note[:600],
                                  status_now=status_now or "Unknown", opened_by=self.actor(user), quiet=not tell)
        try:
            await self._tell_admins_about_help(h)
        except Exception as e:
            logger.warning(f"Ticket {h['id']} saved, but telling the admins failed: {type(e).__name__}: {e}")
        how = None
        if tell:
            message = str(body.get("message") or "").strip()[:600] or \
                f"An admin is looking into your request for {title}. You'll hear back here when it's sorted."
            h = await helpdesk.add(h["id"], "reply", self.actor(user), message) or h
            how = await self.tell_member(h, message, context=f"ticket opened on {title}", by=self.actor(user) if body.get("message") else None,
                                         entry=helpdesk.last_reply(h))
        logger.info(f"{self.actor(user)} opened ticket {h['id']} on No. {row.get('slot')} ({title}), told: {(how or 'nobody') if tell else 'no'}")
        out = {"ok": True, "message": "Ticket opened. It's on Manage → Tickets.", "help": {"id": h["id"], "reason": h["reason"]}}
        if tell and how:
            out.update(told=True, message=f"Ticket opened, and {who} has been told ({TOLD_BY[how]}). It's on Manage → Tickets.")
        elif tell:
            out.update(told=False, message=f"Ticket opened, but {self._not_told(h)} It's on Manage → Tickets.")
        return out

    # ------------------------------------------------------------ tickets
    async def _ticket(self, hid: str) -> dict:
        from database.kv_store import kv_get as get
        from portal import help as helpdesk
        rec = await get(helpdesk.NAMESPACE, hid)
        if not isinstance(rec, dict):
            raise web.HTTPNotFound(text='{"error":"No such ticket."}', content_type="application/json")
        return {**rec, "id": hid}

    async def ticket_comment(self, user: dict, hid: str, body: dict) -> dict:
        """An admin's note (admins only) or a reply to the member (sent to them)."""
        from portal import help as helpdesk
        self.limit(user["user"]["id"], "admin")
        h = await self._ticket(hid)
        text = str(body.get("text") or "").strip()[:1200]
        if not text:
            raise web.HTTPBadRequest(text='{"error":"Write something first."}', content_type="application/json")
        if body.get("kind") == "reply":
            h = await helpdesk.add(hid, "reply", self.actor(user), text, quiet=False)
            how = await self.tell_member(h, text, context=f"ticket reply on {h['title']}", by=self.actor(user),
                                         entry=helpdesk.last_reply(h))
            if how:
                return {"ok": True, "told": True, "message": f"Sent to {h['who']} ({TOLD_BY[how]})."}
            return {"ok": True, "told": False, "message": f"Added to the ticket, but {self._not_told(h)}"}
        await helpdesk.add(hid, "note", self.actor(user), text)
        return {"ok": True, "message": "Note added. Only admins see it."}

    async def ticket_status(self, user: dict, hid: str, body: dict) -> dict:
        """Open (an admin's move), waiting (on the member's answer) or resolved (with an
        optional last message, which the member gets unless the ticket is a quiet one)."""
        from portal import help as helpdesk
        self.limit(user["user"]["id"], "admin")
        h = await self._ticket(hid)
        actor, to = self.actor(user), body.get("status")
        if to == "resolved":
            return await self.help_resolve(user, hid, {"reply": body.get("message") or ""})
        if to == "waiting":
            if h.get("status") != "open":
                await helpdesk.reopen(hid, actor)
            await helpdesk.add(hid, "status", actor, "Waiting on them", waiting=True)
            return {"ok": True, "message": f"Waiting on {h['who']}'s answer."}
        if to == "open":
            if h.get("status") == "resolved":
                await helpdesk.reopen(hid, actor)
            elif h.get("waiting"):
                await helpdesk.add(hid, "status", actor, "Back with the admins", waiting=False)
            return {"ok": True, "message": "Open."}
        raise web.HTTPBadRequest(text='{"error":"Unknown status."}', content_type="application/json")

    # ------------------------------------------------------- blocked imports
    @staticmethod
    def _blocked_ref(app: str, download_id: str) -> None:
        from core.blocked_imports import APPS, DOWNLOAD_ID
        if app not in APPS or not DOWNLOAD_ID.fullmatch(download_id or ""):
            raise web.HTTPNotFound(text='{"error":"No such download."}', content_type="application/json")

    async def blocked_list(self, user: dict) -> dict:
        """Manage → Health: every download Sonarr/Radarr won't import by themselves."""
        from core import blocked_imports
        self.limit(user["user"]["id"], "admin")
        rows = []
        for b in await blocked_imports.blocked(self.services):
            rows.append({k: b[k] for k in ("app", "downloadId", "title", "year", "release", "messages", "episodes")}
                        | {"ticket": await blocked_imports.ticket_for(b["app"], b["downloadId"])})
        return {"rows": rows}

    async def blocked_preview(self, user: dict, app: str, download_id: str) -> dict:
        """What's in it and what looks off, for an admin to look at before importing."""
        from core import blocked_imports
        self.limit(user["user"]["id"], "admin")
        self._blocked_ref(app, download_id)
        try:
            return blocked_imports.public(await blocked_imports.preview(self.services, app, download_id))
        except LookupError as e:
            raise web.HTTPNotFound(text=json.dumps({"error": str(e)}), content_type="application/json")

    async def blocked_import(self, user: dict, app: str, download_id: str, body: Optional[dict] = None) -> dict:
        """Import it through Sonarr's/Radarr's Manual Import (after the admin held the button),
        with their choices for each file (which episode or film it is, quality, language,
        release group, or skip it)."""
        from core import blocked_imports
        from portal import help as helpdesk
        self.limit(user["user"]["id"], "admin")
        self._blocked_ref(app, download_id)
        actor = self.actor(user)
        choices = (body or {}).get("files")
        if choices is not None and (not isinstance(choices, list) or len(choices) > 300):
            raise web.HTTPBadRequest(text='{"error":"files must be a list."}', content_type="application/json")
        try:
            message = await blocked_imports.do_import(self.services, app, download_id, choices)
        except LookupError as e:
            return {"ok": False, "message": str(e)}
        except (ValueError, RuntimeError) as e:
            return {"ok": False, "message": str(e)}
        hid = await blocked_imports.ticket_for(app, download_id)
        if hid:
            await helpdesk.add(hid, "action", actor, f"Looked it over and imported it. {message}")
        logger.info(f"{actor} imported blocked download {app}:{download_id}: {message}")
        return {"ok": True, "message": message}

    async def arr_library(self, user: dict, app: str, q: str) -> dict:
        """Shows or films in Sonarr/Radarr matching `q`: "Wrong show?" on a blocked import."""
        from core import blocked_imports
        self.limit(user["user"]["id"], "lookup")
        if app not in blocked_imports.APPS:
            raise web.HTTPNotFound(text='{"error":"No such service."}', content_type="application/json")
        return {"rows": await blocked_imports.library(self.services, app, (q or "")[:80])}

    async def arr_episodes(self, user: dict, series_id: int) -> dict:
        """Every episode of a show in Sonarr, to say which one a file is."""
        from core import blocked_imports
        self.limit(user["user"]["id"], "lookup")
        return {"rows": await blocked_imports.episodes_of(self.services, series_id)}

    async def ticket_take(self, user: dict, hid: str) -> dict:
        from portal import help as helpdesk
        self.limit(user["user"]["id"], "admin")
        await self._ticket(hid)
        h = await helpdesk.take(hid, self.actor(user), user.get("discordId"), user.get("plexAccountId"))
        mine = h.get("owner") == self.actor(user)
        return {"ok": True, "message": "It's yours. You'll get an alert when they answer." if mine else "Let go. Anyone can take it."}

    async def member_reply(self, user: dict, request_key: str, body: dict) -> dict:
        """The member answers on their ticket (the website, the app, or Discord's Reply):
        it goes on the ticket, the ticket stops waiting, and its owner (or, with no owner,
        every admin) gets an alert."""
        from portal import help as helpdesk
        if not str(request_key).isdigit():
            raise web.HTTPNotFound(text='{"error":"No such request."}', content_type="application/json")
        rec = await get_request(int(request_key))
        if not rec or not helpdesk.owns(rec, user):
            raise web.HTTPNotFound(text='{"error":"No such request."}', content_type="application/json")
        ticket = (await helpdesk.open_for({str(request_key)})).get(str(request_key))
        if not ticket:
            raise web.HTTPConflict(text='{"error":"This ticket is closed. Ask for help again if it\'s still wrong."}', content_type="application/json")
        text = str(body.get("text") or "").strip()[:1200]
        if not text:
            raise web.HTTPBadRequest(text='{"error":"Write your answer first."}', content_type="application/json")
        self.limit(user.get("discordId") or f"plex:{user.get('plexAccountId')}", "help")
        h = await helpdesk.add(ticket["id"], "member", ticket["who"], text, waiting=False)
        from core.message_log import record_from_ticket
        await record_from_ticket(h, text=text, title=f"Answer about {h['title']}", source=str(body.get("source") or "web"),
                                 context="ticket answer")
        await self._tell_admins_about_answer(h, text)
        return {"ok": True, "message": "Sent. The admins have it."}

    async def _tell_admins_about_answer(self, h: dict, text: str) -> None:
        import discord
        from core import notify
        number = f"No. {int(h['slot']):04d}" if h.get("slot") else "A request"
        channel = admin_channel(self.bot, self.config)
        if channel:
            try:
                # A member's words: never a ping, whatever the bot's defaults (and no link previews).
                await channel.send(f"💬 **{discord.utils.escape_markdown(h['who'])}** answered on {number} ({h['title']}): {text[:300]}\n"
                                   f"{self.ticket_link(h['id'])}", allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            except Exception as e:
                logger.warning(f"Could not post a ticket answer to the admin channel: {e}")
        if h.get("owner_discord_id") or h.get("owner_plex_id"):
            ids, plex = {h.get("owner_discord_id")}, {h.get("owner_plex_id")}
        else:
            ids, plex = self._admin_ids(), set()
            from database.kv_store import kv_get as get
            from portal.auth import OWNER_ID
            owner_id = await get(*OWNER_ID)
            plex = {owner_id} if owner_id else set()
        await notify.push_to_admins(discord_ids={i for i in ids if i}, plex_account_ids={p for p in plex if p},
                                    title=f"{h['who']} answered on {number}", body=text[:140],
                                    url=f"/manage?tab=tickets&ticket={h['id']}", tag=f"help-{h['id']}")

    def ticket_link(self, hid: str) -> str:
        return f"{(self.public_url or '').rstrip('/')}/manage?tab=tickets&ticket={hid}"

    def _admin_ids(self) -> set:
        guild = home_guild(self.bot, self.config)
        ids = {str(self.config.bot_owner_id)} if self.config.bot_owner_id else set()
        if guild:
            for m in guild.members:
                roles = {r.id for r in m.roles}
                if m.guild_permissions.administrator or (self.config.admin_role_id and self.config.admin_role_id in roles):
                    ids.add(str(m.id))
        return ids

    async def request_search(self, user: dict, key: str, how: str) -> dict:
        """Search again (whole, episode by episode, or by name) straight from a request.
        Noted on the request, and on its open ticket if it has one."""
        from portal import help as helpdesk
        self.limit(user["user"]["id"], "admin")
        rec = await self._request_for_admin(key)
        if rec.get("status") != "approved":
            raise web.HTTPConflict(text='{"error":"Only an approved request can be searched for."}', content_type="application/json")
        title = (rec.get("media") or {}).get("title") or (rec.get("media") or {}).get("name") or "A request"
        actor = self.actor(user)
        ticket = (await helpdesk.open_for({str(key)})).get(str(key))
        channel = admin_channel(self.bot, self.config)

        async def note(by: str, text: str) -> None:
            await self._note_activity(key, by, text)
            if ticket:
                await helpdesk.note_search(ticket["id"], by, text)
        try:
            if how == "name":
                async def done(text: str, found: bool) -> None:
                    await note("Plexbie", text)
                    if found and ticket:
                        await helpdesk.resolve(ticket["id"], "Plexbie", text)
                    if channel:
                        try:
                            await channel.send(f"{'✅' if found else '🔎'} **{title}**: {text}")
                        except Exception as e:
                            logger.warning(f"Could not report a name search: {e}")
                message = await helpdesk.search_by_name(self.services, str(key), done)
            elif how == "episodes":
                message = await helpdesk.search_episodes(self.services, str(key))
            else:
                message = await helpdesk.search_again(self.services, str(key))
        except LookupError as e:
            return {"ok": False, "message": str(e)}
        await note(actor, message)
        self.data.cache.drop("sonarr:queue")
        self.data.cache.drop("radarr:queue")
        return {"ok": True, "message": message}

    # ---------------------------------------------------- discord tools
    def _guild(self):
        return home_guild(self.bot, self.config)

    async def say(self, user: dict, body: dict) -> dict:
        """What /say does: post as Plexbie. Mass pings stay off unless asked for."""
        import discord
        self.limit(user["user"]["id"], "admin")
        guild = self._guild()
        text = str(body.get("message") or "").strip()
        cid = str(body.get("channelId") or "")
        if not guild or not cid.isdigit():
            raise web.HTTPBadRequest(text='{"error":"Pick a channel."}', content_type="application/json")
        if not 1 <= len(text) <= 2000:
            raise web.HTTPBadRequest(text='{"error":"Messages are 1 to 2000 characters."}', content_type="application/json")
        channel = guild.get_channel(int(cid))
        if channel is None or not hasattr(channel, "send"):
            raise web.HTTPNotFound(text='{"error":"That channel isn\'t there any more."}', content_type="application/json")
        pings = _flag(body, "allowMassPings")
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(everyone=pings, roles=pings, users=True))
        except discord.Forbidden:
            return {"ok": False, "message": f"Plexbie can't post in #{channel.name}."}
        logger.info(f"{self.actor(user)} posted as Plexbie in #{channel.name} from the website")
        return {"ok": True, "message": f"Posted in #{channel.name}."}

    async def discord_overview(self, user: dict) -> dict:
        """The Manage page's Discord tab: channels for /say, who brought whom, the live watch party."""
        from sqlalchemy import select
        from database.kv_store import kv_get_all
        from database.session import get_session
        from plugins.invite_tracker.models import InviteUse
        from portal.invites import NAMESPACE as INVITES

        guild = self._guild()
        name_of = lambda uid, fallback: (guild.get_member(int(uid)).display_name  # noqa: E731
                                         if guild and str(uid).isdigit() and guild.get_member(int(uid)) else fallback)
        joins = []
        if guild:
            async with get_session() as session:
                rows = (await session.execute(
                    select(InviteUse).where(InviteUse.guild_id == str(guild.id)).order_by(InviteUse.joined_at.desc()).limit(150)
                )).scalars().all()
            for r in rows:
                role = guild.get_role(int(r.role_id)) if (r.auto_role_assigned and r.role_id and str(r.role_id).isdigit()) else None
                joins.append({"who": name_of(r.joiner_id, r.joiner_name or "Someone who left"),
                              "by": name_of(r.inviter_id, r.inviter_name or "Unknown"), "via": "discord",
                              "code": r.invite_code, "at": r.joined_at.isoformat() if r.joined_at else None,
                              "role": role.name if role else None})
        for rec in (await kv_get_all(INVITES)).values():
            if isinstance(rec, dict) and rec.get("used_at"):
                joins.append({"who": rec.get("used_by") or rec.get("label"), "by": rec.get("created_by") or "an admin",
                              "via": "plexbie", "code": rec.get("label"), "at": rec.get("used_at"), "role": None})
        joins.sort(key=lambda j: j["at"] or "", reverse=True)

        party = None
        cog = self.bot.get_cog("WatchPartyCog") if self.bot else None
        active = getattr(cog, "active_party", None)
        if active:
            channel = self.bot.get_channel(active.voice_channel_id)
            party = {
                "channel": channel.name if channel else None,
                "streamer": name_of(active.streamer_discord_id, active.streamer_plex_username or "Someone"),
                "title": active.media_title,
                "startedAt": active.started_at.isoformat() if active.started_at else None,
                "people": [name_of(p.discord_id, p.plex_username) for p in active.participants.values()],
            }
        channels = [{"id": str(c.id), "name": c.name} for c in (guild.text_channels if guild else [])
                    if guild.me is None or c.permissions_for(guild.me).send_messages]
        from portal import inbox
        return {"channels": channels, "joins": joins, "party": party,
                "inbox": {**(await inbox.settings()), "threadsMissing": inbox.threads_ok(self.bot) if self.bot else None}}

    # ------------------------------------------------ DMs: the shared inbox
    async def inbox_settings(self, user: dict, body: dict) -> dict:
        from portal import inbox
        self.limit(user["user"]["id"], "admin")
        s = await inbox.set_settings(autoreply=_flag(body, "autoreply"))
        return {"ok": True, "message": "Plexbie answers new DMs." if s["autoreply"] else "Plexbie won't answer DMs by itself."}

    async def alert_admins_about_dm(self, who: str, name: str, text: str) -> None:
        """Someone DMed Plexbie: a phone/browser alert to every admin who turned alerts on."""
        from core import notify
        from database.kv_store import kv_get as get
        from portal.auth import OWNER_ID
        owner = await get(*OWNER_ID)
        try:
            await notify.push_to_admins(discord_ids=self._admin_ids(), plex_account_ids={owner} if owner else set(),
                                        title=f"{name} messaged Plexbie", body=text[:140],
                                        url=f"/manage?tab=messages&who={quote(who)}", tag=f"dm-{who}")
        except Exception as e:
            logger.info(f"Couldn't alert admins about a DM: {type(e).__name__}: {e}")

    @staticmethod
    def _who(who: str) -> str:
        from portal.inbox import WHO
        if not WHO.fullmatch(who or ""):
            raise web.HTTPNotFound(text='{"error":"No such conversation."}', content_type="application/json")
        return who

    async def message_reply(self, user: dict, who: str, body: dict) -> dict:
        """An admin answers someone as Plexbie, signed with their name: a Discord DM, or for
        people without Discord a phone alert or email. Only to someone Plexbie already has
        a conversation with. Logged with who sent it, copied to the person's DM thread, and
        the conversation is marked done."""
        from core import message_log
        from portal import inbox
        self._who(who)
        self.limit(user["user"]["id"], "admin")
        text = str(body.get("text") or "").strip()
        if not 1 <= len(text) <= 1500:
            raise web.HTTPBadRequest(text='{"error":"Write a message (up to 1500 characters)."}', content_type="application/json")
        ident = await message_log.identity(who)
        if not ident:
            raise web.HTTPNotFound(text='{"error":"No such conversation."}', content_type="application/json")
        admin = self.actor(user)
        content = inbox.signed(text, admin)
        if who.startswith("d"):
            if not ident.get("discord_id") or not self.bot:
                raise web.HTTPConflict(text='{"error":"Plexbie isn\'t connected to Discord right now."}', content_type="application/json")
            from core.admin_mirror import dm_user_id
            sent = await dm_user_id(self.bot, self.services, ident["discord_id"], context="reply from an admin",
                                    content=content, sent_by=admin)
            if not sent:
                raise web.HTTPConflict(text='{"error":"It didn\'t arrive: their Discord DMs are closed to Plexbie. It\'s noted in the conversation."}',
                                       content_type="application/json")
            said = f"Sent to {ident['name']} as a Discord DM."
            await inbox.post(self.bot, who, ident["name"], f"↩️ **{discord_escape(admin)}** replied: {text}")
        else:
            from core.notify import notify_member
            how = await notify_member(self.services, title="A message from your Plex admins", body=content, url="/",
                                      plex_account_id=ident.get("plex_account_id"), plex_name=ident.get("plex_name"),
                                      context="reply from an admin", sent_by=admin)
            if how == "none":
                raise web.HTTPConflict(text='{"error":"It didn\'t arrive: they have no phone alerts and no email. It\'s noted in the conversation."}',
                                       content_type="application/json")
            said = f"Sent to {ident['name']} as a {'phone alert' if how == 'push' else 'email'}."
        await message_log.mark_done(who, admin)
        return {"ok": True, "message": said}

    async def message_done(self, user: dict, who: str, body: dict) -> dict:
        from core import message_log
        self._who(who)
        self.limit(user["user"]["id"], "admin")
        done = _flag(body, "done") if "done" in body else True
        await message_log.mark_done(who, self.actor(user), done)
        return {"ok": True, "message": "Marked done. It stays in the history." if done else "Marked unread."}

    async def message_to_ticket(self, user: dict, key: str) -> dict:
        """A DM someone sent Plexbie goes on their open ticket, as their answer."""
        from core import message_log
        from portal import help as helpdesk
        from portal.inbox import open_ticket_for
        self.limit(user["user"]["id"], "admin")
        e = await message_log.entry(key) if re.fullmatch(r"[0-9T]{20,30}-[0-9a-f]{6}", key or "") else None
        if not e or e.get("direction") != "in":
            raise web.HTTPNotFound(text='{"error":"That message isn\'t one someone sent Plexbie."}', content_type="application/json")
        if e.get("ticket"):
            raise web.HTTPConflict(text='{"error":"It\'s already on their ticket."}', content_type="application/json")
        ticket = await open_ticket_for(e)
        if not ticket:
            raise web.HTTPConflict(text='{"error":"They have no open ticket. Open one from All requests."}', content_type="application/json")
        await helpdesk.add(ticket["id"], "member", ticket.get("who") or "They", e.get("text") or "", waiting=False)
        await helpdesk.add(ticket["id"], "action", self.actor(user), "Added their Discord DM to the ticket")
        await message_log.note_ticket(key, ticket["id"])
        return {"ok": True, "message": f"Added to their ticket on {ticket.get('title') or 'their request'}.", "ticket": ticket["id"]}

    async def remove_person(self, user: dict, body: dict) -> dict:
        self.limit(user["user"]["id"], "admin")
        name = str(body.get("plexName") or "")
        cog = self.bot.get_cog("UserMgmtCog")
        if not cog or not name:
            raise web.HTTPBadRequest(text='{"error":"User management isn\'t available."}', content_type="application/json")
        ok, message = await cog.remove_plex_user(name, self.actor(user))
        return {"ok": ok, "message": message.replace("❌ ", "").replace("✅ ", "")}
