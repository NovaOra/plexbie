# path: core/services.py
"""Dependency injection container for bot services"""
import asyncio
import time
from typing import Optional, Tuple

import aiohttp
from plexapi.server import PlexServer

from core.blocking import run_blocking
from core.clients import Arr, Hydra, OpenLibrary, Seerr, Sabnzbd, Tautulli, Tmdb
from core.config import Config
from core.logging import get_logger
from core.security import redact

logger = get_logger(__name__)


#: How long a sessions() snapshot may be reused. Deliberately just under the
#: 10-second interval of the two loops that poll it, so a display never shows
#: data older than its own refresh period while still letting a second caller in
#: the same window reuse the first one's result.
PLEX_SESSIONS_MAX_AGE = 8.0


class BotServices:
    """Service container for dependency injection"""

    def __init__(self, config: Config):
        self.config = config
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.plex_server: Optional[PlexServer] = None
        # Outside services, one shared client each (core/clients.py).
        self.tautulli = Tautulli(self)
        self.sonarr = Arr(self, "sonarr")
        self.radarr = Arr(self, "radarr")
        self.tmdb = Tmdb(self)
        self.sab = Sabnzbd(self)
        self.seerr = Seerr(self)
        self.hydra = Hydra(self)
        self.openlibrary = OpenLibrary(self)
        # Shared snapshot of plex_server.sessions(): (server, monotonic_time,
        # sessions). See plex_sessions() for why this is shared.
        self._sessions_cache: Optional[Tuple[PlexServer, float, list]] = None
        self._sessions_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize all services"""
        # HTTP session
        self.http_session = aiohttp.ClientSession()

        # Plex server. PlexServer() performs an HTTP handshake, so keep it off
        # the event loop even during startup.
        if self.config.plex_url and self.config.plex_token:
            try:
                self.plex_server = await run_blocking(
                    PlexServer,
                    self.config.plex_url,
                    self.config.plex_token,
                )
                logger.info(f"   ✅ Plex: Connected to {redact(self.config.plex_url)}")
            except Exception as e:
                logger.warning(f"   ⚠️  Plex: Connection failed - {str(e)[:50]}")
                self.plex_server = None

        # Check for configured integrations
        integrations = []
        if self.config.seerr_url and self.config.seerr_token:
            integrations.append("Seerr")
        if self.tautulli.configured:
            integrations.append("Tautulli")
        if self.config.tmdb_api_key:
            integrations.append("TMDB")

        if integrations:
            logger.info(f"   ✅ Integrations: {', '.join(integrations)}")

    async def reconnect_plex(self) -> bool:
        """Attempt to reconnect to Plex server. Returns True if successful.

        Async because PlexServer() is a blocking HTTP handshake and this is
        called from task loops that run as often as every 10 seconds - a failing
        reconnect would otherwise stall the loop for plexapi's 30s timeout on
        every tick.
        """
        if not self.config.plex_url or not self.config.plex_token:
            return False
        try:
            self.plex_server = await run_blocking(
                PlexServer,
                self.config.plex_url,
                self.config.plex_token,
            )
            logger.info(f"Plex: Reconnected to {redact(self.config.plex_url)}")
            return True
        except Exception as e:
            logger.debug(f"Plex reconnect failed: {str(e)[:80]}")
            self.plex_server = None
            return False

    def _cached_sessions(self, server: PlexServer, max_age: float) -> Optional[list]:
        """The cached snapshot if it belongs to `server` and is young enough."""
        if self._sessions_cache is None:
            return None
        cached_server, fetched_at, sessions = self._sessions_cache
        if cached_server is not server:
            # A reconnect replaced the server object. What the old one reported
            # says nothing about the new connection, so treat it as absent.
            return None
        if (time.monotonic() - fetched_at) > max_age:
            return None
        return sessions

    async def plex_sessions(
        self,
        max_age: float = PLEX_SESSIONS_MAX_AGE,
        force: bool = False,
    ) -> list:
        """Current Plex sessions, shared by every plugin that polls for them.

        Four consumers want the same fact - who is streaming right now.
        watch_tracking drives it at a hardcoded 10 seconds; service_health probes
        it and watch_party verifies the streamer against it on intervals taken
        from config (HEALTH_CHECK_INTERVAL and WATCH_PARTY_CREDIT_INTERVAL,
        currently 300s each, which is why their @tasks.loop decorator defaults of
        30s and 10s are misleading); /status reads it on demand. Each was issuing
        its own request for one snapshot.

        At today's intervals that overlap is small - watch_tracking accounts for
        almost all of the ~8,900 requests a day. The value here is that the
        overlap no longer scales: turning either interval down, which their
        decorator defaults suggest was once intended, now costs nothing extra.

        Only successful fetches are cached, and the entry is tied to the identity
        of the PlexServer it came from, so a reconnect invalidates it without any
        explicit bookkeeping.

        `force=True` skips *reading* the cache but still writes it: a health probe
        satisfied from cache is not a probe, yet its fresh result is perfectly
        good for everyone else.

        Raises whatever plexapi raises. Callers already guard on plex_server
        being present; failing loudly is better than reporting an empty session
        list, which reads as "nobody is watching".
        """
        server = self.plex_server
        if server is None:
            raise RuntimeError("Plex server is not connected")

        if not force:
            cached = self._cached_sessions(server, max_age)
            if cached is not None:
                return cached

        async with self._sessions_lock:
            # Re-check under the lock. Two loops firing in the same moment is the
            # exact case this method exists to collapse, and without this the
            # second one would still issue its own request.
            server = self.plex_server
            if server is None:
                raise RuntimeError("Plex server is not connected")
            if not force:
                cached = self._cached_sessions(server, max_age)
                if cached is not None:
                    return cached

            sessions = await run_blocking(server.sessions)
            self._sessions_cache = (server, time.monotonic(), sessions)
            return sessions

    async def cleanup(self) -> None:
        """Cleanup services on shutdown"""
        if self.http_session:
            await self.http_session.close()
