# path: plugins/service_health/cog.py
"""Service Health Monitor - monitors Plex, Tautulli, and other services with auto-retry and alerting"""
import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional
from dataclasses import dataclass

import discord
from discord.ext import commands, tasks
from plexapi.server import PlexServer

from core.blocking import run_blocking
from core.clients import ServiceError
from core.logging import get_logger
from core.services import BotServices
from core.discord_lookup import resolve_channel

logger = get_logger(__name__)

# Configuration
# Configuration now loaded from services.config:
# - health_check_interval, health_alert_cooldown, health_max_failures


class ServiceStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class ServiceHealth:
    """Track health status for a service"""
    name: str
    status: ServiceStatus = ServiceStatus.UNKNOWN
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    consecutive_failures: int = 0
    last_alert_sent: Optional[datetime] = None
    last_error: Optional[str] = None
    was_down: bool = False  # Track if service was down for recovery alerts


class ServiceHealthCog(commands.Cog):
    """Monitor service health and alert on failures"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services

        # Track health for each service
        self.health: Dict[str, ServiceHealth] = {
            "plex": ServiceHealth(name="Plex"),
            "tautulli": ServiceHealth(name="Tautulli"),
        }

        # Admin channel for alerts
        self.admin_channel: Optional[discord.TextChannel] = None

        # Interval from config; cog_load starts the loop
        self.health_check_loop.change_interval(seconds=services.config.health_check_interval)

    async def cog_load(self):
        # Started here, not in __init__: add_cog runs this, so a plugin that
        # never loads never starts it.
        self.health_check_loop.start()

    def cog_unload(self):
        """Cleanup on unload"""
        self.health_check_loop.cancel()

    async def _get_admin_channel(self) -> Optional[discord.TextChannel]:
        """Get the admin channel for alerts"""
        if self.admin_channel:
            return self.admin_channel

        channel_id = self.services.config.admin_channel_id
        if not channel_id:
            logger.warning("ADMIN_CHANNEL_ID not configured - health alerts disabled")
            return None

        try:
            self.admin_channel = await resolve_channel(self.bot, channel_id)
            return self.admin_channel
        except Exception as e:
            logger.error(f"Failed to get admin channel: {e}")
            return None

    # ==================== Health Checks ====================

    async def _check_plex(self) -> tuple[ServiceStatus, Optional[str]]:
        """Check Plex server health - reuses existing connection when possible"""
        try:
            if not self.services.config.plex_url or not self.services.config.plex_token:
                return ServiceStatus.UNKNOWN, "Not configured"

            # Try to use existing connection first
            if self.services.plex_server:
                try:
                    # force=True: a liveness probe answered from cache is not a
                    # probe. It still seeds the shared snapshot, so the polling
                    # loops can reuse this request. See services.plex_sessions.
                    _ = await self.services.plex_sessions(force=True)
                    return ServiceStatus.HEALTHY, None
                except Exception as e:
                    logger.debug(f"Plex connection stale, reconnecting: {e}")
                    pass

            # Create new connection (only if no existing or existing failed).
            # PlexServer() is a blocking handshake; this runs on a 30s task loop,
            # so an unreachable Plex would otherwise stall the loop each tick.
            plex = await run_blocking(
                PlexServer,
                self.services.config.plex_url,
                self.services.config.plex_token,
                timeout=10,
            )

            # Verify the connection before publishing it. Deliberately a direct
            # call, not services.plex_sessions(): `plex` is not yet the shared
            # plex_server, and the whole point is to test this object rather than
            # whatever the container currently holds.
            _ = await run_blocking(plex.sessions)

            # Update the shared plex_server reference
            self.services.plex_server = plex
            logger.info("Plex connection established/restored")

            return ServiceStatus.HEALTHY, None

        except Exception as e:
            error_msg = str(e)[:200]
            # Mark plex_server as None so other plugins know it's down
            self.services.plex_server = None
            return ServiceStatus.DOWN, error_msg

    async def _check_tautulli(self) -> tuple[ServiceStatus, Optional[str]]:
        """Check Tautulli health"""
        tautulli = self.services.tautulli
        if not tautulli.configured:
            return ServiceStatus.UNKNOWN, "Not configured"
        try:
            await tautulli.ping(timeout=10)
            return ServiceStatus.HEALTHY, None
        except ServiceError as e:
            # Reached, but Tautulli itself reported a problem.
            if e.status is None and str(e).startswith("Tautulli said"):
                return ServiceStatus.DEGRADED, str(e)[:200]
            return ServiceStatus.DOWN, str(e)[:200]

    # ==================== Alert System ====================

    async def _send_alert(self, service: ServiceHealth, is_recovery: bool = False):
        """Send an alert to the admin channel"""
        channel = await self._get_admin_channel()
        if not channel:
            return

        # Check cooldown (don't spam)
        now = datetime.now(timezone.utc)
        if not is_recovery and service.last_alert_sent:
            if (now - service.last_alert_sent).total_seconds() < self.services.config.health_alert_cooldown:
                return

        if is_recovery:
            # Recovery alert
            downtime = "Unknown"
            if service.last_failure:
                downtime_delta = now - service.last_failure
                minutes = int(downtime_delta.total_seconds() / 60)
                if minutes < 60:
                    downtime = f"{minutes} minutes"
                else:
                    hours = minutes // 60
                    mins = minutes % 60
                    downtime = f"{hours}h {mins}m"

            embed = discord.Embed(
                title=f"✅ {service.name} - RECOVERED",
                description=f"**{service.name}** is back online!",
                color=discord.Color.green(),
                timestamp=now
            )
            embed.add_field(name="Downtime", value=downtime, inline=True)
            embed.add_field(name="Status", value="Healthy", inline=True)

        else:
            # Failure alert
            embed = discord.Embed(
                title=f"🚨 {service.name} - SERVICE DOWN",
                description=f"**{service.name}** is not responding!",
                color=discord.Color.red(),
                timestamp=now
            )
            embed.add_field(name="Status", value=service.status.value.upper(), inline=True)
            embed.add_field(name="Consecutive Failures", value=str(service.consecutive_failures), inline=True)

            if service.last_success:
                last_ok = f"<t:{int(service.last_success.timestamp())}:R>"
                embed.add_field(name="Last Successful Check", value=last_ok, inline=True)

            if service.last_error:
                # Truncate error for embed
                error_text = service.last_error[:500]
                embed.add_field(name="Error Details", value=f"```\n{error_text}\n```", inline=False)

        embed.set_footer(text="Service Health Monitor")

        try:
            await channel.send(embed=embed)
            service.last_alert_sent = now
            logger.info(f"Sent {'recovery' if is_recovery else 'failure'} alert for {service.name}")
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")

    # ==================== Main Health Check Loop ====================

    @tasks.loop(seconds=30)  # Default, changed in __init__ from config
    async def health_check_loop(self):
        """Periodically check all services.

        Every check is individually guarded. discord.ext.tasks stops a Loop on an
        unhandled exception, so anything escaping here would silently kill the
        monitor - which is what happened when the failure path referenced two
        config attributes that did not exist: the monitor died at the exact moment
        of the first outage and never checked again until a restart. A monitor that
        fails quietly is worse than no monitor.
        """
        now = datetime.now(timezone.utc)

        for name, check in (
            ("plex", self._check_plex),
            ("tautulli", self._check_tautulli),
        ):
            try:
                status, error = await check()
                await self._update_health(name, status, error, now)
            except Exception as e:
                logger.error(
                    f"Health check for {name} raised; monitor continues: {e}",
                    exc_info=True,
                )

    async def report_event(self, service_name: str, down: bool, detail: Optional[str] = None) -> None:
        """A pushed status (Tautulli's Plex down/up events): alert at once instead of
        after HEALTH_MAX_FAILURES polls."""
        health = self.health.get(service_name)
        if health is None:
            return
        now = datetime.now(timezone.utc)
        if down:
            health.consecutive_failures = max(health.consecutive_failures, self.services.config.health_max_failures - 1)
            await self._update_health(service_name, ServiceStatus.DOWN, detail or "Reported down", now)
        else:
            await self._update_health(service_name, ServiceStatus.HEALTHY, None, now)

    async def _update_health(self, service_name: str, status: ServiceStatus, error: Optional[str], now: datetime):
        """Update health status and trigger alerts if needed"""
        health = self.health[service_name]
        health.status = status

        if status == ServiceStatus.HEALTHY:
            # Service is healthy
            was_down = health.was_down

            health.last_success = now
            health.consecutive_failures = 0
            health.last_error = None

            # Send recovery alert if it was down before
            if was_down:
                health.was_down = False
                await self._send_alert(health, is_recovery=True)

        elif status in (ServiceStatus.DOWN, ServiceStatus.DEGRADED):
            # Service is failing
            health.consecutive_failures += 1
            health.last_failure = now
            health.last_error = error

            # Alert after consecutive failures threshold
            if health.consecutive_failures >= self.services.config.health_max_failures:
                if not health.was_down:
                    health.was_down = True
                    await self._send_alert(health, is_recovery=False)
                else:
                    # Already alerted, but check cooldown for repeat alerts
                    await self._send_alert(health, is_recovery=False)

            logger.warning(f"{service_name} health check failed ({health.consecutive_failures}x): {error}")

    @health_check_loop.before_loop
    async def before_health_check(self):
        await self.bot.wait_until_ready()
        # Initial delay to let other services start
        await asyncio.sleep(15)
        logger.info("Service health monitoring started")


