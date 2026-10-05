# path: webhooks/tautulli_handler.py
"""Tautulli webhook: playback and server events, so Plexbie reacts instead of polling.

What each event does:
  play / pause / resume / stop  the Now Watching board refreshes within seconds
  stop / watched                the leaderboard and streak boards refresh (batched)
  play                          someone warned for not watching is told they're
                                all set at once; an unknown viewer starts linking
  intdown                       Plex is reported down to the admins straight away
  intup                         ...and back up (remote-access ext* events are left
                                out: they flap without Plex being down)
  created                       "recently added": announced like Plex's own webhook
                                (which needs Plex Pass); one announcement either way

Once events have arrived, the boards and account linking poll far less often
(see WatchTrackingCog.events_connected and UserMgmtCog.events_connected); the
slower polls remain as a safety net. Plexbie sets the Tautulli side up itself
(core/webhook_connect.py), with BODY below as the JSON data of every trigger.
"""
import asyncio
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict

from aiohttp import web

from core.logging import get_logger
from core.webhooks import read_json_object
from database.kv_store import kv_get, kv_set

logger = get_logger(__name__)

#: The JSON data Tautulli sends for every trigger ({...} are Tautulli's own fields).
# {user} is the name shown in Tautulli (what Plexbie stores), {username} the Plex login.
BODY = ('{"event": "{action}", "user": "{user}", "username": "{username}", "user_id": "{user_id}", '
        '"email": "{user_email}", "title": "{title}", "rating_key": "{rating_key}", "media_type": "{media_type}"}')
#: The triggers Plexbie listens to.
#: Not extdown/extup: remote access flapping isn't Plex being down.
TRIGGERS = ("on_play", "on_stop", "on_pause", "on_resume", "on_watched", "on_created", "on_intdown", "on_intup")

#: Events count as "connected" for this long after the last one arrived.
LIVE_DAYS = 7
_LIVE = ("webhooks", "tautulli")


async def recently_live() -> bool:
    """Whether Tautulli events have arrived lately (so the polls can stay slow)."""
    rec = await kv_get(*_LIVE)
    try:
        last = datetime.fromisoformat((rec or {}).get("last"))
    except (TypeError, ValueError):
        return False
    return (datetime.now(timezone.utc) - last).days < LIVE_DAYS


class TautulliEvents:
    def __init__(self, bot):
        self.bot = bot
        self._soon: Dict[str, asyncio.Task] = {}
        self._live_marked = False

    def _later(self, key: str, delay: float, fn: Callable[[], Awaitable]) -> None:
        """Run fn once, `delay` seconds from the first event of a burst."""
        task = self._soon.get(key)
        if task and not task.done():
            return

        async def run():
            await asyncio.sleep(delay)
            try:
                await fn()
            except Exception as e:
                logger.warning(f"Tautulli event follow-up '{key}' failed: {e}")
        self._soon[key] = asyncio.create_task(run())

    async def _mark_live(self) -> None:
        await kv_set(*_LIVE, {"last": datetime.now(timezone.utc).isoformat()})
        if not self._live_marked:
            self._live_marked = True
            for name in ("WatchTrackingCog", "UserMgmtCog"):
                cog = self.bot.get_cog(name)
                if cog and hasattr(cog, "events_connected"):
                    cog.events_connected()

    async def handle(self, request: web.Request) -> web.Response:
        data = await read_json_object(request)
        if isinstance(data, web.Response):
            return data
        event = str(data.get("event") or data.get("event_type") or data.get("action") or "").lower()
        logger.info(f"Tautulli event: {event or 'unknown'}" + (f" ({data.get('user')})" if data.get("user") else ""))
        try:
            await self.dispatch(event, data)
        except Exception as e:
            logger.error(f"Tautulli event {event} failed: {e}", exc_info=True)
        return web.json_response({"status": "ok"})

    async def dispatch(self, event: str, data: dict) -> None:
        await self._mark_live()
        watch = self.bot.get_cog("WatchTrackingCog")
        if watch and event in ("play", "pause", "resume", "stop", "buffer"):
            # Tautulli fires a little before Plex's session list settles.
            self._later("now", 2, watch.refresh_now_watching)
        if watch and event in ("stop", "watched"):
            self._later("boards", 60, watch.refresh_boards)
        if event == "play":
            users = self.bot.get_cog("UserMgmtCog")
            if users:
                await users.on_playback(data.get("user") or "", data.get("email") or "", data.get("user_id") or "",
                                        login=data.get("username") or "")
        if event == "created":
            arrivals = self.bot.get_cog("NewMediaAddedCog")
            if arrivals and data.get("rating_key"):
                await arrivals.announce_rating_key(data["rating_key"])
        if event in ("intdown", "intup"):
            health = self.bot.get_cog("ServiceHealthCog")
            if health:
                down = event == "intdown"
                await health.report_event("plex", down, "Tautulli reports Plex is down." if down else None)


def register_tautulli_webhook(webhook_server, bot) -> TautulliEvents:
    events = TautulliEvents(bot)
    webhook_server.add_validated_post("/webhook/tautulli", events.handle, "tautulli")
    logger.info("✅ Registered Tautulli webhook handler at /webhook/tautulli")
    return events
