# path: core/webhooks.py
"""Webhook server for external service notifications with signature validation"""

import json
from typing import Any, Dict, Union

from aiohttp import web

from core.logging import get_logger
from core.services import BotServices
from core.webhook_security import WebhookValidator, create_validated_handler

logger = get_logger(__name__)


async def read_json_object(request: web.Request) -> Union[Dict[str, Any], web.Response]:
    """A webhook's JSON body as a dict, or the 400 to answer with instead.

    Reads the bytes the signature check already consumed when there are any."""
    try:
        body = request.get("_validated_body")
        data = json.loads(body) if body else await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not isinstance(data, dict):
        return web.json_response({"error": "Expected an object"}, status=400)
    return data


async def _plain_server_header(request, response) -> None:
    response.headers["Server"] = "Plexbie"           # not which Python and aiohttp versions


class WebhookServer:
    """HTTP server for webhooks from Sonarr, Radarr, Plex, Seerr and Tautulli."""

    def __init__(self, services: BotServices):
        self.services = services
        self.app = web.Application()
        self.app.on_response_prepare.append(_plain_server_header)
        self.runner = None
        self.validator = WebhookValidator(services.config)
        self.bound_addresses = []
        # Services whose routes actually run through add_validated_post, so the
        # startup banner can report what is really protected.
        self._registered_services = set()
        self.setup_routes()

    def add_validated_post(self, path: str, handler, service: str):
        """Register a POST webhook route behind signature validation.

        Every inbound webhook route must be registered through here. Calling
        ``self.app.router.add_post`` directly silently bypasses authentication,
        which is how /webhook/sonarr, /webhook/radarr and /webhook/plex ended up
        unauthenticated while the startup banner claimed otherwise.
        """
        from core.webhook_security import KNOWN_SERVICES
        if service not in KNOWN_SERVICES:
            # A typo here used to leave the route open, since no secret could match it.
            raise ValueError(f"Unknown webhook service {service!r}; expected one of {KNOWN_SERVICES}")
        self.app.router.add_post(
            path,
            create_validated_handler(handler, self.validator, service)
        )
        self._registered_services.add(service)

    def setup_routes(self):
        """Configure webhook endpoints with validation"""
        # Service routes are added by their handlers (webhooks/*_handler.py)
        # before start(), through add_validated_post.
        self.app.router.add_get("/health", self._handle_health)

    def _bind_addresses(self):
        """Parse WEBHOOK_BIND into an ordered list of addresses."""
        raw = getattr(self.services.config, "webhook_bind", "127.0.0.1") or "127.0.0.1"
        addresses = [a.strip() for a in raw.split(",") if a.strip()]
        return addresses or ["127.0.0.1"]

    async def start(self):
        """Start the webhook server.

        Binds only the configured addresses (loopback by default) rather than
        0.0.0.0. With network_mode: host, 0.0.0.0 published every /webhook/* route
        to the entire LAN, and those routes pass unauthenticated requests through
        whenever the matching secret is unset.
        """
        port = self.services.config.webhook_port
        addresses = self._bind_addresses()

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        bound = []
        for address in addresses:
            try:
                await web.TCPSite(self.runner, address, port).start()
                bound.append(address)
            except OSError as e:
                logger.error(f"Could not bind webhook server to {address}:{port} - {e}")

        if not bound:
            raise RuntimeError(
                f"Webhook server could not bind any of {addresses} on port {port}"
            )

        self.bound_addresses = bound
        listening = ", ".join(f"{a}:{port}" for a in bound)
        logger.info(f"Webhook server listening on {listening}")

        # A route without a secret refuses every request, wherever the listener is:
        # on loopback too, since a local tunnel or proxy forwards strangers from there.
        # (bot.ensure_webhook_secrets makes any that are missing on start.)
        open_routes = sorted(s for s in self._registered_services if not self.validator._get_secret(s))
        if open_routes:
            logger.warning(
                f"   ⚠️  Webhooks for {', '.join(r.title() for r in open_routes)} have no secret, so they refuse "
                "every request. Set their *_WEBHOOK_SECRET in config/.env, or press Connect on the setup page."
            )

        self._log_auth_status()

    def _log_auth_status(self):
        """Report per-route auth status.

        A route is only authenticated when it was registered through
        add_validated_post AND a secret is configured for it, so report the
        intersection. Reporting merely-configured secrets previously told
        operators that Sonarr/Radarr were protected when their routes did not
        run the validator at all.
        """
        protected = []
        unprotected = []

        for service in sorted(self._registered_services):
            if self.validator._get_secret(service):
                protected.append(service.title())
            else:
                unprotected.append(service.title())

        if protected:
            logger.info(f"   🔒 Webhook auth enforced for: {', '.join(protected)}")
        if unprotected:
            logger.warning(
                f"   ⚠️  Unauthenticated webhook routes (no secret set): {', '.join(unprotected)}"
            )
        if not protected:
            logger.warning("   No webhook secrets configured - all webhooks are unauthenticated")

    async def stop(self):
        """Stop the webhook server"""
        if self.runner:
            await self.runner.cleanup()

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint"""
        return web.json_response({"status": "healthy"})
