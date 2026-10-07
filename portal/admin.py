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
from database.kv_store import kv_get_all, kv_set
from database.session import get_session
from portal.data import Data, _iso, predates_outcomes, tmdb_art
from core.discord_lookup import PUBLIC_BOT_FIX, home_guild, home_health_items

logger = get_logger(__name__)

#: When each request last changed stage (and its download last moved), so Manage can
#: say how long something has sat where it is. Updated whenever requests are looked at.
STAGE_NAMESPACE = "request_stage"
#: What admins did straight from a request (Manage → All requests), newest last.
ACTIVITY_NAMESPACE = "request_activity"
#: How long requests that reached Plex stay in the All requests list.
FINISHED_DAYS = 30
SEARCH_LIMIT = 60
#: Looks stuck when... (hours)
STUCK_SEARCHING = 24       # approved this long ago, and still nothing found
STUCK_STILL = 6            # a download whose percentage hasn't moved
STUCK_ADDING = 2           # unpacking or being added to Plex


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _ts(value: Any) -> float:
    t = _parse(value)
    return t.timestamp() if t else 0.0


def _within(value: Any, now: datetime, days: int) -> bool:
    t = _parse(value)
    return bool(t) and (now - t).total_seconds() <= days * 86400


def _hours_since(value: Any, now: datetime) -> float:
    t = _parse(value)
    return (now - t).total_seconds() / 3600 if t else 0.0


async def _remember_stage(key: str, stage: str, percent: Any, before: Optional[dict], now: datetime) -> dict:
    """The request's stage with when it got there, and when its download last moved."""
    stamp = now.isoformat()
    if not isinstance(before, dict) or before.get("stage") != stage:
        mark = {"stage": stage, "since": stamp, "percent": percent, "moved": stamp}
    elif before.get("percent") != percent:
        mark = {**before, "percent": percent, "moved": stamp}
    else:
        return before
    await kv_set(STAGE_NAMESPACE, key, mark)
    return mark


#: Stages that are over without reaching Plex.
ENDED = ("declined", "closed")


def _all_counts(rows: List[dict]) -> dict:
    """All requests' filters: on its way, looks stuck, waiting for a decision, on Plex, declined."""
    return {"active": sum(r["stage"] not in ("available", "requested", *ENDED) for r in rows),
            "stuck": sum(bool(r["stuck"]) for r in rows),
            "waiting": sum(r["stage"] == "requested" for r in rows),
            "finished": sum(r["stage"] == "available" for r in rows),
            "declined": sum(r["stage"] in ENDED for r in rows)}


def _ticket_row(hid: str, h: dict) -> dict:
    """A ticket on the Tickets list."""
    from portal import help as helpdesk
    thread = helpdesk.thread_of(h)
    last = thread[-1] if thread else {}
    return {
        "id": hid, "requestKey": h.get("request"), "slot": h.get("slot") or 0, "title": h.get("title") or "",
        "kind": h.get("kind") or "", "seasons": h.get("seasons"), "who": h.get("who") or "Someone",
        "reason": h.get("reason") or "", "status": h.get("status") or "open", "waiting": bool(h.get("waiting")),
        "owner": h.get("owner"), "openedBy": h.get("opened_by"), "offer": h.get("offer"),
        "createdAt": h.get("created_at"), "updatedAt": last.get("at") or h.get("created_at"),
        "last": {"by": last.get("by"), "kind": last.get("kind"), "text": str(last.get("text") or "")[:160]} if last else None,
        "count": len(thread),
    }


def stuck_reasons(row: dict, approved_at: Any, mark: dict, now: datetime) -> List[str]:
    """Why a request looks stuck, as an admin would put it (empty when it doesn't)."""
    stage, progress, out = row.get("stage"), row.get("progress") or {}, []
    if row.get("help"):
        out.append(f"Help asked: {row['help'].get('reason')}")
    if progress.get("problem"):
        out.append(progress["problem"])
    if stage in ("approved", "searching") and _hours_since(approved_at, now) >= STUCK_SEARCHING:
        out.append("Nothing found for over a day" if stage == "searching" else "Not picked up for over a day")
    if stage == "downloading" and _hours_since(mark.get("moved"), now) >= STUCK_STILL:
        out.append(f"Download hasn't moved in {STUCK_STILL} hours")
    if stage in ("unpacking", "importing") and _hours_since(mark.get("since"), now) >= STUCK_ADDING:
        out.append("Unpacking for over 2 hours" if stage == "unpacking" else "Not on Plex 2 hours after downloading")
    return out


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

    # ------------------------------------------------------ all requests
    async def all_requests(self, q: str = "", everything: bool = False) -> dict:
        """Manage → All requests: every request from everyone (waiting, approved, declined,
        on Plex) with its live stage as the person who asked sees it, plus who asked and
        whether it looks stuck. Without a search: what was asked for in the last 30 days,
        what's still on its way however old, and what reached Plex in the last 30 days.
        With `everything`: every request since No. 0001. With a search: any request ever,
        by title, who asked or its number."""
        from portal import help as helpdesk
        query = " ".join(str(q or "").lower().split())[:80]
        number = query.removeprefix("no.").removeprefix("#").strip().lstrip("0")
        records = await self.data._slots()
        names = await self._names()
        open_help = await helpdesk.open_for()
        seen = await kv_get_all(STAGE_NAMESPACE)
        books: Dict[str, Any] = {}
        now = datetime.now(timezone.utc)
        rows = []
        for key, rec in records.items():
            who = self._who(names, rec)
            if query:
                media = rec.get("media") or {}
                title = (media.get("title") or media.get("name") or "").lower()
                if not (query in title or query in who.lower() or (number.isdigit() and number == str(rec["_slot"]))):
                    continue
            elif not everything and not (
                    _within(rec.get("timestamp"), now, FINISHED_DAYS)            # asked for lately
                    or _within(rec.get("available_at"), now, FINISHED_DAYS)      # reached Plex lately
                    or (rec.get("status") == "approved" and not rec.get("available_at"))  # maybe still on its way
                    or str(key) in open_help):
                continue
            row = await self._admin_row(key, rec, who, open_help, seen, books, now)
            if not query and not everything and row["stage"] in ("available", "declined", "closed") \
                    and not _within(rec.get("timestamp"), now, FINISHED_DAYS) \
                    and not _within(rec.get("available_at"), now, FINISHED_DAYS) and not row["stuck"]:
                continue                       # an old approved request that turned out to be finished (or over)
            rows.append(row)
        if query:
            rows.sort(key=lambda r: r["slot"], reverse=True)
            rows = rows[:SEARCH_LIMIT]
        else:
            rows.sort(key=lambda r: (not r["stuck"], -r["slot"]))
        return {"rows": rows, "counts": None if query else _all_counts(rows), "query": query or None,
                "everything": bool(everything and not query), "total": len(records)}

    async def request_detail(self, key: str) -> Optional[dict]:
        """One request in full for an admin: the row, where it came from, and every ticket on it."""
        from portal import help as helpdesk
        records = await self.data._slots()
        rec = records.get(str(key))
        if not rec:
            return None
        names = await self._names()
        row = await self._admin_row(str(key), rec, self._who(names, rec), await helpdesk.open_for({str(key)}),
                                    await kv_get_all(STAGE_NAMESPACE), {}, datetime.now(timezone.utc))
        tickets = [{**h, "id": hid} for hid, h in (await kv_get_all(helpdesk.NAMESPACE)).items()
                   if isinstance(h, dict) and h.get("request") == str(key)]
        tickets.sort(key=lambda h: h.get("created_at") or "", reverse=True)
        source = rec.get("source")
        guild, channel = self.config.guild_id, self.config.admin_channel_id
        return {
            **row,
            "via": "Seerr" if source in ("seerr", "overseerr") else "the website" if rec.get("via") == "website" else "Discord",
            "seerrId": rec.get("overseerr_request_id"),
            "discordUrl": f"https://discord.com/channels/{guild}/{channel}/{key}" if guild and channel and str(key).isdigit() else None,
            "tickets": [{k: h.get(k) for k in ("id", "status", "reason", "note", "who", "opened_by", "created_at",
                                                   "resolved_by", "resolved_at", "reply", "actions", "status_then")}
                        for h in tickets],
            "activity": (await kv_get_all(ACTIVITY_NAMESPACE)).get(str(key)) or [],
        }

    async def _admin_row(self, key: str, rec: dict, who: str, open_help: Dict[str, dict], seen: Dict[str, Any],
                         books: Dict[str, Any], now: datetime) -> dict:
        row = await self.data.request_row(key, rec, open_help, books)
        progress = row.get("progress") or {}
        mark = await _remember_stage(key, row["stage"], progress.get("percent"), seen.get(key), now)
        approved_at = rec.get("resolved_at") if rec.get("status") == "approved" else None
        finished = rec.get("available_at") or (mark["since"] if row["stage"] == "available" else None)
        return {
            **row,
            "requester": who,
            "status": rec.get("status", "pending"),
            "approvedBy": rec.get("resolved_by") if approved_at else None,
            "approvedAt": _iso(approved_at) if approved_at else None,
            "stageSince": mark["since"],
            "finishedAt": _iso(finished) if finished else None,
            "stuck": stuck_reasons(row, approved_at, mark, now),
        }

    # ------------------------------------------------------------ messages
    async def message_people(self) -> List[dict]:
        """Manage → Messages: everyone, with their open ticket (for "Add to their ticket")."""
        from core import message_log
        from portal import help as helpdesk
        open_by: Dict[str, dict] = {}
        for hid, h in (await kv_get_all(helpdesk.NAMESPACE)).items():
            if isinstance(h, dict) and h.get("status") == "open":
                row = {"id": hid, "title": h.get("title") or "", "slot": h.get("slot") or 0}
                for k in (f"d{h['discord_id']}" if h.get("discord_id") else None,
                          f"p{(h.get('plex_name') or '').lower()}" if h.get("plex_name") else None):
                    if k:
                        open_by[k] = row
        return [{**p, "ticket": open_by.get(p["id"])} for p in await message_log.people()]

    # ------------------------------------------------------------ tickets
    async def tickets(self) -> dict:
        """Manage → Tickets: every ticket, the ones needing an admin first, then those
        waiting on the member, then the last 50 solved."""
        from portal import help as helpdesk
        rows = []
        for hid, h in (await kv_get_all(helpdesk.NAMESPACE)).items():
            if isinstance(h, dict):
                rows.append(_ticket_row(hid, h))
        newest = lambda rs: sorted(rs, key=lambda r: r["updatedAt"] or "", reverse=True)   # noqa: E731
        action = newest(r for r in rows if r["status"] == "open" and not r["waiting"])
        waiting = newest(r for r in rows if r["status"] == "open" and r["waiting"])
        solved = newest(r for r in rows if r["status"] != "open")[:50]
        return {"rows": action + waiting + solved,
                "counts": {"action": len(action), "waiting": len(waiting), "solved": len(solved)}}

    async def ticket(self, hid: str) -> Optional[dict]:
        """One ticket in full: its timeline, and its request as All requests shows it."""
        from portal import help as helpdesk
        h = (await kv_get_all(helpdesk.NAMESPACE)).get(hid)
        if not isinstance(h, dict):
            return None
        out = {**_ticket_row(hid, h), "note": h.get("note"), "statusThen": h.get("status_then"),
               "quiet": bool(h.get("quiet")), "thread": helpdesk.thread_of(h), "request": None,
               # A download Sonarr/Radarr won't import by themselves (core/blocked_imports).
               "blocked": h.get("blocked") if isinstance(h.get("blocked"), dict) else None}
        rec = (await self.data._slots()).get(str(h.get("request")))
        if rec:
            names = await self._names()
            out["request"] = await self._admin_row(str(h["request"]), rec, self._who(names, rec),
                                                   await helpdesk.open_for({str(h["request"])}),
                                                   await kv_get_all(STAGE_NAMESPACE), {}, datetime.now(timezone.utc))
        return out

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

            app_info: Dict[str, Any] = {}

            async def fetch_app() -> dict:
                # The bot's application, asked for once for both Discord checks below.
                if "error" in app_info:
                    raise app_info["error"]
                if "json" not in app_info:
                    try:
                        async with self.services.http_session.get(
                                "https://discord.com/api/v10/applications/@me", timeout=6,
                                headers={"Authorization": f"Bot {cfg.discord_bot_token}", "User-Agent": "Plexbie"}) as r:
                            if r.status != 200:
                                raise ServiceError(f"Discord answered HTTP {r.status}", r.status)
                            app_info["json"] = await r.json()
                    except Exception as e:
                        app_info["error"] = e
                        raise
                return app_info["json"]

            async def discord_sign_in() -> None:
                # Discord refuses a sign-in whose return address isn't listed, exactly, under
                # OAuth2 > Redirects, before anything reaches Plexbie: say so here instead.
                listed = (await fetch_app()).get("redirect_uris") or []
                if cfg.discord_callback_url not in listed:
                    raise ServiceError(f"Discord doesn't list {cfg.discord_callback_url}. Add it in the Discord Developer "
                                       "Portal: your app > OAuth2 > Redirects.")

            if cfg.plex_url and cfg.plex_token:
                await probe("Plex", plex)
            async def public_bot() -> None:
                # On (Discord's default), anyone with the bot's ID can add it to a server of theirs.
                if (await fetch_app()).get("bot_public") is True:
                    raise ServiceError(f"Public Bot is on, so anyone with Plexbie's ID can add it to their own "
                                       f"server. {PUBLIC_BOT_FIX}")

            if cfg.discord_client_id and cfg.discord_client_secret and cfg.discord_callback_url and cfg.discord_bot_token:
                await probe("Discord sign-in", discord_sign_in)
            if cfg.discord_bot_token:
                try:
                    known = "bot_public" in await fetch_app()
                except Exception:
                    known = False       # Discord didn't answer: the sign-in check, when set up, says so
                if known:
                    await probe("Discord Public Bot", public_bot)
            if self.bot is not None:
                from portal.inbox import threads_ok
                missing = threads_ok(self.bot)
                checks.append({"name": "Discord DM threads", "ok": not missing, "ms": 0,
                               "detail": f"Give Plexbie's role {missing} in the admin channel, so each DM gets its own thread."
                               if missing else None})
                checks.extend(home_health_items(self.bot, cfg))
            for client in (self.services.seerr, self.services.sonarr, self.services.radarr,
                           self.services.tautulli, self.services.sab):
                if client.configured:
                    await probe(client.name, client.ping)
            return checks
        return await self.cache.get("admin:health", 60, load)
