# path: portal/admin.py
"""What the Manage page shows: the same facts the admin slash commands report.

Read-only here. The actions (approve, decline, invite, remove, exempt, ...) go
through the bot's own code paths and live with the in-bot portal, so a click on
the website and a click in Discord do the same thing.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from core.clients import ServiceError
from core.logging import get_logger
from database.kv_store import kv_get_all
from database.session import get_session
from portal.data import Data, _iso, predates_outcomes, tmdb_art
from core.discord_lookup import home_guild

logger = get_logger(__name__)


class Admin:
    def __init__(self, data: Data, bot=None):
        self.data = data
        self.bot = bot
        self.services = data.services
        self.config = data.config
        self.cache = data.cache

    async def _names(self) -> Dict[str, str]:
        """Who's who: "d<discord id>" and "p<plex account id>" -> the name to show.
        A name an admin gave someone (People → Rename) wins."""
        from plugins.user_mgmt.models import PlexUser
        async with get_session() as session:
            rows = (await session.execute(select(PlexUser))).scalars().all()
        out = {}
        for r in rows:
            shown = r.display_name or r.discord_username or r.plex_username
            if r.discord_id:
                out[f"d{r.discord_id}"] = shown
            if r.plex_user_id:
                out[f"p{r.plex_user_id}"] = r.display_name or r.plex_username
        return out

    @staticmethod
    def _who(names: Dict[str, str], rec: Any) -> str:
        """The requester: by their Discord account, else (signed in with Plex on the
        website, or mirrored from Seerr) by their Plex account or stored name.
        Takes a request record, or just a Discord id (the join list)."""
        if not isinstance(rec, dict):
            rec = {"user_id": rec}
        uid = rec.get("user_id")
        if uid:
            try:
                return names.get(f"d{int(uid)}") or f"Discord user …{str(int(uid))[-4:]}"
            except (TypeError, ValueError):
                pass
        pid = rec.get("plex_account_id")
        return names.get(f"p{pid}") or rec.get("requester_name") or "Unknown"

    # ---------------------------------------------------------- requests
    async def requests(self) -> dict:
        records = await self.data._slots()
        names = await self._names()
        pending, older, decided = [], [], []
        guild, channel = self.config.guild_id, self.config.admin_channel_id
        for key, rec in records.items():
            media = rec.get("media") or {}
            is_book = rec.get("media_type") in ("ebook", "audiobook", "both") or "open_library_key" in media
            item = {
                "id": key,
                "slot": rec["_slot"],
                "title": media.get("title") or media.get("name") or "Untitled",
                "kind": (media.get("request_format") or rec.get("media_type") or "ebook") if is_book else ("movie" if media.get("media_type") == "movie" else "tv"),
                "poster": None if is_book else tmdb_art(media.get("poster_path"), "w185"),
                "seasons": None if is_book else rec.get("seasons"),
                "requester": self._who(names, rec),
                "requestedAt": _iso(rec.get("timestamp")),
                "status": rec.get("status", "pending"),
                "resolvedBy": rec.get("resolved_by"),
                "resolvedAt": _iso(rec.get("resolved_at")) if rec.get("resolved_at") else None,
                "discordUrl": f"https://discord.com/channels/{guild}/{channel}/{key}" if guild and channel and key.isdigit() else None,
            }
            if item["status"] == "closed":
                continue  # the cleared backlog: neither waiting nor a decision
            if item["status"] != "pending":
                decided.append(item)
            elif predates_outcomes(rec):
                older.append(item)
            else:
                pending.append(item)
        pending.sort(key=lambda r: r["slot"], reverse=True)
        older.sort(key=lambda r: r["slot"], reverse=True)
        decided = sorted((d for d in decided if d["resolvedAt"]), key=lambda r: r["resolvedAt"], reverse=True)[:20]
        return {"pending": pending, "older": older, "recent": decided}

    # ------------------------------------------------------------- joins
    async def joins(self) -> List[dict]:
        records = await kv_get_all("plex_invites")
        web_records = await kv_get_all("web_plex_joins")
        names = await self._names()
        out = [{
            "key": f"d{uid}",
            "messageId": str(rec.get("message_id") or ""),
            "name": self._who(names, uid),
            "via": "discord",
            "email": rec.get("email"),
            "status": rec.get("status", "pending"),
            "askedAt": _iso(rec.get("timestamp")),
        } for uid, rec in records.items() if isinstance(rec, dict)]
        out += [{
            "key": f"p{pid}",
            "messageId": str(rec.get("message_id") or ""),
            "name": f"{rec.get('plex_name')} (Plex sign-in)",
            "via": "plex",
            "email": rec.get("email"),
            "status": rec.get("status", "pending"),
            "askedAt": _iso(rec.get("timestamp")),
        } for pid, rec in web_records.items() if isinstance(rec, dict)]
        out.sort(key=lambda j: (j["status"] != "pending", j["askedAt"]), reverse=False)
        return out

    # ------------------------------------------------------------ people
    async def _shared(self) -> Optional[set]:
        """Everyone the server is shared with right now (plex.tv), or None if unknown."""
        from core.blocking import run_blocking
        from plugins.user_mgmt.cog import _fetch_shared_usernames
        from core.plex_account import can_sign_in
        cfg = self.config
        if not can_sign_in(cfg):
            return None

        async def load():
            return sorted(await run_blocking(_fetch_shared_usernames, cfg))
        try:
            return set(await self.cache.get("admin:shared", 300, load))
        except Exception as e:
            logger.info(f"portal admin: share list unavailable ({e})")
            return None

    async def _owner(self) -> Optional[str]:
        """The server owner's account name (systemAccounts id 1), as the bot finds it.

        The owner never appears in their own share list, so without this the
        owner would show as "no longer on Plex".
        """
        from core.blocking import run_blocking
        server = getattr(self.services, "plex_server", None)
        if not server:
            return None

        async def load():
            accounts = await run_blocking(server.systemAccounts)
            return next((a.name for a in accounts if a.id == 1), None)
        try:
            return await self.cache.get("admin:owner", 3600, load)
        except Exception as e:
            logger.info(f"portal admin: owner unavailable ({e})")
            return None

    async def accounts(self) -> Optional[List[dict]]:
        """Everyone the server is shared with on plex.tv, with ids and emails (cached)."""
        from core.blocking import run_blocking
        from core.plex_account import can_sign_in, shared_accounts
        if not can_sign_in(self.config):
            return None
        try:
            return await self.cache.get("admin:accounts", 300, lambda: run_blocking(shared_accounts, self.config))
        except Exception as e:
            logger.info(f"portal admin: plex.tv accounts unavailable ({e})")
            return None

    async def people(self) -> List[dict]:
        """Everyone with access, plus anyone still tracked who has lost it.

        The tracking table only holds accounts the inactivity check has picked
        up, so on its own it undercounts; the share list on plex.tv is the
        source of truth for who is actually on the server.
        """
        from plugins.user_mgmt.cog import reconcile_accounts
        from plugins.user_mgmt.models import PlexUser
        accounts = await self.accounts()
        if accounts:
            try:
                await reconcile_accounts(accounts)       # learn who each tracked row really is
            except Exception as e:
                logger.info(f"portal admin: couldn't match people to Plex accounts ({e})")
        async with get_session() as session:
            rows = (await session.execute(select(PlexUser))).scalars().all()
        shared = await self._shared()
        owner = await self._owner()
        claimed = {r.plex_username.lower() for r in rows}
        claimed_ids = {r.plex_user_id for r in rows if r.plex_user_id}
        unclaimed = sorted((a["title"] for a in accounts or [] if not a["owner"] and a["title"].lower() not in claimed
                            and a["id"] not in claimed_ids), key=str.lower)
        ids = {a["id"] for a in accounts or []}
        if shared is not None and owner:
            shared = shared | {owner}
        warn_after = self.config.inactivity_warning_days
        remove_after = self.config.inactivity_removal_days
        out, seen = [], set()
        for r in rows:
            idle = r.days_inactive or 0
            seen.add(r.plex_username)
            out.append({
                "plexName": r.plex_username,
                "discordName": r.discord_username,
                "discordId": str(r.discord_id) if r.discord_id else None,
                "linked": bool(r.discord_id),
                "lastWatched": _iso(r.last_watched) if r.last_watched else None,
                "daysIdle": idle,
                "warned": bool(r.warning_sent),
                "topThree": bool(r.is_top_watcher),
                "removalIn": None if (r.is_top_watcher or r.never_remove) else max(0, remove_after - idle),
                "neverRemove": bool(r.never_remove),
                "displayName": r.display_name,
                "warnAfter": warn_after,
                "tracked": True,
                "hasAccess": None if shared is None else (r.plex_username in shared or (r.plex_user_id in ids)),
                "owner": r.plex_username == owner,
            })
            if out[-1]["hasAccess"] is False:
                out[-1]["candidates"] = unclaimed
        for name in sorted(shared or (), key=str.lower):
            if name in seen:
                continue
            out.append({
                "plexName": name, "discordName": None, "linked": False, "lastWatched": None,
                "daysIdle": 0, "warned": False, "topThree": False, "removalIn": None,
                "warnAfter": warn_after, "tracked": False, "hasAccess": True, "owner": name == owner,
            })
        out.sort(key=lambda p: (not p["tracked"], p["removalIn"] is None, p["removalIn"] or 0, p["plexName"].lower()))
        return out

    # ----------------------------------------------------------- cleanup
    async def cleanup(self) -> dict:
        config = await self.data.cleanup_config()
        try:
            clock = await self.data.countdown()
        except Exception as e:
            logger.info(f"portal admin: countdown unavailable ({e})")
            clock = {}
        exempt_meta = config.get("exempt_items", {}) or {}
        leaving = sorted((c for c in clock.values() if not c.get("exempt")), key=lambda c: c["daysLeft"])
        return {
            "settings": {
                "enabled": bool(config.get("enabled", True)),
                "practice": bool(config.get("dry_run")),
                "inactivityDays": config.get("inactivity_days"),
                "warnDaysBefore": config.get("notify_days_before"),
                "excludedLibraries": config.get("exclude_libraries", []),
                "channelId": str(config.get("notification_channel_id") or "") or None,
            },
            "libraries": await self._library_names(),
            "channels": self._text_channels(),
            "warning": [self._row(c) for c in leaving if c.get("warning")],
            "upcoming": [self._row(c) for c in leaving if not c.get("warning")][:30],
            "exempt": [{"ratingKey": rk, "title": (m or {}).get("title") or (clock.get(rk) or {}).get("title") or "Unknown",
                        "type": (m or {}).get("type")} for rk, m in exempt_meta.items()],
        }

    async def _library_names(self) -> List[str]:
        """Plex library names, for choosing which ones cleanup skips."""
        from core.blocking import run_blocking
        server = self.services.plex_server
        if not server:
            return []

        async def load():
            return sorted(s.title for s in await run_blocking(server.library.sections))
        try:
            return await self.cache.get("admin:libraries", 600, load)
        except Exception:
            return []

    def _text_channels(self) -> List[dict]:
        """Text channels the bot can post in, for the cleanup notices."""
        guild = home_guild(self.bot, self.config)
        if not guild:
            return []
        me = guild.me
        return [{"id": str(c.id), "name": c.name} for c in guild.text_channels
                if me is None or c.permissions_for(me).send_messages]

    @staticmethod
    def _row(c: dict) -> dict:
        return {"ratingKey": c["ratingKey"], "title": c["title"], "type": c["type"], "daysLeft": c["daysLeft"],
                "reason": c["reason"], "lastActivity": c.get("lastActivity")}

    # ------------------------------------------------------------ health
    async def health(self) -> List[dict]:
        async def load():
            cfg = self.config
            checks = []

            async def probe(name: str, ping) -> None:
                started = datetime.now(timezone.utc)
                try:
                    await ping()
                    ok, detail = True, None
                except ServiceError as e:
                    ok, detail = False, f"HTTP {e.status}" if e.status else str(e)
                except Exception as e:
                    ok, detail = False, type(e).__name__
                ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                checks.append({"name": name, "ok": ok, "ms": ms, "detail": detail})

            async def plex() -> None:
                async with self.services.http_session.get(f"{cfg.plex_url.rstrip('/')}/identity",
                                                          headers={"X-Plex-Token": cfg.plex_token}, timeout=6) as r:
                    if r.status != 200:
                        raise ServiceError(f"Plex answered HTTP {r.status}", r.status)

            async def discord_sign_in() -> None:
                # Discord refuses a sign-in whose return address isn't listed, exactly, under
                # OAuth2 > Redirects, before anything reaches Plexbie: say so here instead.
                async with self.services.http_session.get(
                        "https://discord.com/api/v10/applications/@me", timeout=6,
                        headers={"Authorization": f"Bot {cfg.discord_bot_token}", "User-Agent": "Plexbie"}) as r:
                    if r.status != 200:
                        raise ServiceError(f"Discord answered HTTP {r.status}", r.status)
                    listed = (await r.json()).get("redirect_uris") or []
                if cfg.discord_callback_url not in listed:
                    raise ServiceError(f"Discord doesn't list {cfg.discord_callback_url}. Add it in the Discord Developer "
                                       "Portal: your app > OAuth2 > Redirects.")

            if cfg.plex_url and cfg.plex_token:
                await probe("Plex", plex)
            if cfg.discord_client_id and cfg.discord_client_secret and cfg.discord_callback_url and cfg.discord_bot_token:
                await probe("Discord sign-in", discord_sign_in)
            for client in (self.services.seerr, self.services.sonarr, self.services.radarr,
                           self.services.tautulli, self.services.sab):
                if client.configured:
                    await probe(client.name, client.ping)
            return checks
        return await self.cache.get("admin:health", 60, load)
