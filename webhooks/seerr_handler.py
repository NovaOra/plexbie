# path: webhooks/seerr_handler.py
"""Seerr webhook: requests made or decided outside Plexbie.

  pending / auto-approved / approved   a request made in Seerr's own site or
                                       app becomes a Plexbie request too: live
                                       progress, the help button, arrival DMs.
                                       Pending ones are announced to the admins.
  approved / declined                  decided in Seerr: the requester hears
                                       the outcome the usual way (Discord DM, else
                                       phone alert or email).
  failed                               opens a help request for the admins, with
                                       Seerr's reason, as if the requester had
                                       asked.
  available                            noted on the request.

Plexbie's own approvals also reach Seerr (through its API key), so Seerr
echoes them back. remember_submission() and the request number Plexbie stores
from Seerr's reply keep those from being mirrored a second time.
Plexbie sets the Seerr side up itself (core/webhook_connect.py) with PAYLOAD.
"""
import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

from core.clients import ServiceError
from core.discord_lookup import admin_channel
from core.logging import get_logger
from core.webhooks import read_json_object
from database.request_store import SEERR_SOURCES, all_requests, find_by_seerr_id, get_request, save_request, set_fields

logger = get_logger(__name__)

#: The JSON Seerr sends (its own {{...}} template variables).
PAYLOAD = {
    "notification_type": "{{notification_type}}",
    "subject": "{{subject}}",
    "message": "{{message}}",
    "{{media}}": {"media_type": "{{media_type}}", "tmdbId": "{{media_tmdbid}}", "status": "{{media_status}}"},
    "{{request}}": {"request_id": "{{request_id}}", "requestedBy_email": "{{requestedBy_email}}",
                    "requestedBy_username": "{{requestedBy_username}}",
                    "requestedBy_settings_discordId": "{{requestedBy_settings_discordId}}",
                    # Seerr renamed it (a list); whichever app doesn't know a name
                    # leaves the {{...}} text as it is.
                    "requestedBy_settings_discordIds": "{{requestedBy_settings_discordIds}}"},
    "{{extra}}": [],
}
#: Seerr's notification types Plexbie wants (a bitmask): pending 2, approved 4,
#: available 8, failed 16, declined 64, auto-approved 128.
TYPES = 2 | 4 | 8 | 16 | 64 | 128

#: (media type, TMDB id) -> when Plexbie itself last sent it to Seerr.
_OWN: Dict[Tuple[str, int], float] = {}
OWN_WINDOW = 300


def remember_submission(media_type: str, tmdb_id: Any) -> None:
    """Plexbie is about to request this in Seerr: its echo isn't a new request."""
    try:
        _OWN[(media_type, int(tmdb_id))] = time.monotonic()
    except (TypeError, ValueError):
        pass


def _ours(media_type: str, tmdb_id: int) -> bool:
    at = _OWN.get((media_type, tmdb_id))
    return at is not None and time.monotonic() - at < OWN_WINDOW


def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _seasons(extra: List[dict], media_type: str) -> Any:
    for item in extra or []:
        if (item.get("name") or "").lower().startswith("requested seasons"):
            nums = sorted({_int(x) for x in str(item.get("value") or "").split(",") if _int(x)})
            if nums:
                return nums
    return "all" if media_type == "tv" else None


class SeerrEvents:
    def __init__(self, bot):
        self.bot = bot

    @property
    def services(self):
        return self.bot.services

    async def handle(self, request: web.Request) -> web.Response:
        data = await read_json_object(request)
        if isinstance(data, web.Response):
            return data
        kind = str(data.get("notification_type") or "")
        logger.info(f"Seerr event: {kind or 'unknown'} {data.get('subject') or ''}".strip())
        try:
            await self.dispatch(kind, data)
        except Exception as e:
            logger.error(f"Seerr event {kind} failed: {e}", exc_info=True)
        return web.json_response({"status": "ok"})

    async def dispatch(self, kind: str, data: dict) -> None:
        if kind == "TEST_NOTIFICATION":
            return
        if kind == "MEDIA_AVAILABLE":
            # Seerr's own Plex scan saw it: check Plex now instead of at the next
            # sweep. (It sends nothing for a show that is still airing - "partially
            # available" - which is why the sweep runs regardless.)
            arrivals = self.bot.get_cog("NewMediaAddedCog")
            if arrivals:
                asyncio.create_task(arrivals.check_recently_added())
        req = data.get("request") or {}
        media = data.get("media") or {}
        rid, tmdb = _int(req.get("request_id")), _int(media.get("tmdbId"))
        media_type = "movie" if media.get("media_type") == "movie" else "tv"
        if not rid or not tmdb:
            return

        if kind in ("MEDIA_APPROVED", "MEDIA_AUTO_APPROVED") and _ours(media_type, tmdb):
            # Plexbie just approved this itself; Seerr is echoing it back.
            found = await find_by_seerr_id(rid)
            if found is None:
                await self._attach_own(rid, media_type, tmdb)
            return
        found = await find_by_seerr_id(rid)
        if found is None and _ours(media_type, tmdb):
            await self._attach_own(rid, media_type, tmdb)
            return
        if found is None:
            if kind not in ("MEDIA_PENDING", "MEDIA_APPROVED", "MEDIA_AUTO_APPROVED", "MEDIA_AUTO_REQUESTED"):
                return
            found = await self._mirror(rid, media_type, tmdb, req, _seasons(data.get("extra"), media_type), data)
        key, rec = found

        if kind in ("MEDIA_APPROVED", "MEDIA_AUTO_APPROVED", "MEDIA_DECLINED"):
            await apply_decision(self.bot, int(key), kind != "MEDIA_DECLINED", "Seerr")
        elif kind == "MEDIA_FAILED":
            await self._failed(int(key), rec, data.get("message") or "")
        elif kind == "MEDIA_AVAILABLE":
            await set_fields(int(key), available_at=datetime.now(timezone.utc).isoformat())

    async def _attach_own(self, rid: int, media_type: str, tmdb: int) -> None:
        """Plexbie's own request echoed back: remember Seerr's number for it."""
        newest = None
        for key, rec in (await all_requests()).items():
            m = (rec or {}).get("media") or {}
            if _int(m.get("id")) == tmdb and not rec.get("overseerr_request_id") and rec.get("source") not in SEERR_SOURCES:
                if newest is None or (rec.get("timestamp") or "") > (newest[1].get("timestamp") or ""):
                    newest = (key, rec)
        if newest:
            await set_fields(int(newest[0]), overseerr_request_id=rid)

    async def _mirror(self, rid: int, media_type: str, tmdb: int, req: dict, seasons: Any, data: dict) -> tuple:
        """Make a Plexbie request for one made in Seerr, keyed by Seerr's number."""
        details: Dict[str, Any] = {}
        try:
            details = await self.services.seerr.get(f"{media_type}/{tmdb}") or {}
        except ServiceError as e:
            logger.info(f"Seerr details for {media_type} {tmdb} unavailable: {e}")
        title = details.get("title") or details.get("name") or (data.get("subject") or "Untitled").rsplit(" (", 1)[0]
        media = {"id": tmdb, "media_type": media_type, "poster_path": details.get("posterPath"),
                 "overview": details.get("overview") or ""}
        if media_type == "movie":
            media.update(title=title, release_date=details.get("releaseDate") or "")
        else:
            media.update(name=title, first_air_date=details.get("firstAirDate") or "")
        who = await self._requester(req, rid)
        await save_request(rid, user_id=who["user_id"], media=media, seasons=seasons, media_type=media_type,
                           extra={"source": "seerr", "overseerr_request_id": rid,
                                  "requester_name": who["name"], "plex_account_id": who["plex_account_id"]})
        logger.info(f"Seerr request {rid} for {title} from {who['name']} added to Plexbie")
        if data.get("notification_type") == "MEDIA_PENDING":
            await self._announce(rid, title, who, seasons)
        return str(rid), await get_request(rid)

    async def _requester(self, req: dict, rid: Optional[int] = None) -> dict:
        """Who asked: the Plex account Seerr says made the request, and the Discord
        member Plexbie has linked to it.

        Not the payload's Discord id or display name: every Seerr user types
        those in themselves, so either could pin a request, and the messages about it,
        on someone else. The webhook secret only proves Seerr sent the event."""
        name = (req.get("requestedBy_username") or "").strip() or "Someone"
        out = {"user_id": None, "plex_account_id": None, "name": name}
        plex_id = None
        if rid is not None and self.services.seerr.configured:
            try:
                detail = await self.services.seerr.get(f"request/{rid}") or {}
                plex_id = (detail.get("requestedBy") or {}).get("plexId")
            except ServiceError as e:
                logger.info(f"Couldn't ask Seerr who made request {rid}: {e}")
        if not str(plex_id or "").isdigit():
            return out
        out["plex_account_id"] = str(plex_id)
        try:
            from database.people import sole_person_for_account
            from database.session import get_session
            async with get_session() as session:
                row = await sole_person_for_account(session, plex_id)
            if row:
                out["name"] = row.display_name or row.plex_username
                if row.discord_id:
                    out["user_id"] = int(row.discord_id)
        except Exception as e:
            logger.info(f"Couldn't look up Plex account {plex_id}: {e}")
        return out

    async def _announce(self, rid: int, title: str, who: dict, seasons: Any) -> None:
        cfg = self.services.config
        channel = admin_channel(self.bot, cfg)
        if channel is None:
            return
        import discord
        what = title + (f" (season {', '.join(map(str, seasons))})" if isinstance(seasons, list) else "")
        embed = discord.Embed(title=f"📥 New request in Seerr: {what}", color=discord.Color.blurple(),
                              description="Approve or decline it on the website (Manage → Requests) or in Seerr.")
        embed.add_field(name="From", value=who["name"] + (f" (<@{who['user_id']}>)" if who["user_id"] else ""))
        try:
            msg = await channel.send(embed=embed)
            await set_fields(rid, admin_card_id=msg.id)
        except Exception as e:
            logger.warning(f"Couldn't announce Seerr request {rid}: {e}")

    async def _failed(self, key: int, rec: dict, reason: str) -> None:
        """A download failed in Seerr: open a help request for the admins."""
        from portal import help as helpdesk
        actions = getattr(self.bot, "portal_actions", None)
        if await helpdesk.open_for({str(key)}):
            return
        media = rec.get("media") or {}
        title = media.get("title") or media.get("name") or "Untitled"
        slot = 0
        if actions:
            try:
                slot = (await actions.data._slots()).get(str(key), {}).get("_slot", 0)
            except Exception:
                pass
        user = {"user": {"name": rec.get("requester_name") or "Seerr"}, "discordId": rec.get("user_id"),
                "plexAccountId": rec.get("plex_account_id"), "plexName": rec.get("requester_name")}
        h = await helpdesk.create(request_key=str(key), slot=slot, title=title, kind=media.get("media_type") or "",
                                  seasons=rec.get("seasons"), user=user, reason=helpdesk.REASONS["stuck"],
                                  note=("Seerr reported the download failed. " + reason).strip()[:600],
                                  status_now="Failed in Seerr")
        if actions:
            try:
                await actions._tell_admins_about_help(h)
            except Exception as e:
                logger.warning(f"Help request {h['id']} saved, but telling the admins failed: {e}")


async def apply_decision(bot, key: int, approved: bool, actor: str) -> bool:
    """A decision made in Seerr (or sent there from the website): record it and tell
    the requester. False when the request was already decided."""
    from plugins.media_requests.cog import _DECISION_LOCKS
    # The same lock as the Discord buttons and the website: one decision per request.
    async with _DECISION_LOCKS.setdefault(int(key), asyncio.Lock()):
        return await _apply_decision(bot, key, approved, actor)


async def _apply_decision(bot, key: int, approved: bool, actor: str) -> bool:
    from plugins.media_requests.cog import AdminApprovalView, _after_decision
    services = bot.services
    rec = await get_request(key)
    if not rec or rec.get("status", "pending") != "pending":
        return False
    media = rec.get("media") or {}
    title = media.get("title") or media.get("name") or "your request"
    good_news = f"✅ **Good news!** Your request for **{title}** was approved and should be available shortly."
    await _after_decision(bot, services, key, approved=approved, actor=actor, user_id=rec.get("user_id"), title=title,
                          what="media", user_message=good_news if approved else "")
    if approved:
        view = AdminApprovalView()
        if await view._load_from_saved(key, bot):
            await view._register_with_tracking()
    if rec.get("admin_card_id") and services.config.admin_channel_id:
        try:
            import discord
            channel = admin_channel(bot, services.config)
            msg = await channel.fetch_message(int(rec["admin_card_id"]))
            embed = msg.embeds[0]
            embed.color = discord.Color.green() if approved else discord.Color.red()
            embed.add_field(name="✅ Approved" if approved else "❌ Declined", value=f"by {actor}", inline=False)
            await msg.edit(embed=embed)
        except Exception as e:
            logger.info(f"Couldn't update the admin card for request {key}: {e}")
    logger.info(f"Request {key} ({title}) {'approved' if approved else 'declined'} by {actor}")
    return True


def register_seerr_webhook(webhook_server, bot) -> SeerrEvents:
    events = SeerrEvents(bot)
    webhook_server.add_validated_post("/webhook/seerr", events.handle, "seerr")
    # The address older installs gave Seerr; it keeps working until Plexbie re-points it.
    webhook_server.add_validated_post("/webhook/overseerr", events.handle, "seerr")
    logger.info("✅ Registered Seerr webhook handler at /webhook/seerr")
    return events
