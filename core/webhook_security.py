# path: core/webhook_security.py
"""Webhook signature validation for secure webhook handling"""
import hashlib
import hmac
from typing import Optional, Tuple

from aiohttp import web

from core.logging import get_logger
from core.security import secure_equals  # noqa: F401  (imported from here by the webhook tests)

logger = get_logger(__name__)


#: Every service a webhook route may be registered for (core/webhooks.add_validated_post).
KNOWN_SERVICES = ("sonarr", "radarr", "tautulli", "seerr", "plex")

#: Whether this run has already said Tautulli's secret arrives on the URL.
_warned_query_secret = False


class WebhookValidator:
    """Validates webhook signatures from various services"""

    def __init__(self, config):
        self.config = config

    async def validate_request(
        self,
        request: web.Request,
        service: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Validate a webhook request signature.
        
        Returns:
            Tuple of (is_valid, error_message, body_bytes)
            - If no secret is configured, returns (False, error, None): refused
            - If validation fails, returns (False, error_message, None)
            - If validation succeeds, returns (True, None, body)
        """
        if service not in KNOWN_SERVICES:
            logger.error(f"Webhook for unknown service '{service}' refused: it can't be validated")
            return False, "Unvalidatable service", None
        secret = self._get_secret(service)

        if not secret:
            # Without a secret a route can't tell who's calling, wherever it listens:
            # a tunnel or proxy on this machine forwards strangers from 127.0.0.1 too.
            logger.warning(f"{service.title()} webhook refused: no secret is set for it")
            return False, f"No secret is set for the {service} webhook", None

        # Read body
        body = await request.read()
        
        # Validate based on service type
        if service in ("sonarr", "radarr"):
            return self._validate_arr_signature(request, body, secret, service)
        elif service == "tautulli":
            return self._validate_tautulli_signature(request, body, secret)
        elif service == "seerr":
            return self._validate_seerr_signature(request, body, secret)
        elif service == "plex":
            return self._validate_plex_signature(request, body, secret)
        else:
            # A secret is configured but we have no validator for this service.
            # Fail closed: silently accepting would defeat the operator's intent.
            logger.error(
                f"Webhook secret configured for unknown service '{service}' - "
                f"rejecting request because it cannot be validated"
            )
            return False, "Unvalidatable service", None

    def _get_secret(self, service: str) -> Optional[str]:
        """Get webhook secret for a service from config"""
        secret_map = {
            "sonarr": getattr(self.config, "sonarr_webhook_secret", None),
            "radarr": getattr(self.config, "radarr_webhook_secret", None),
            "tautulli": getattr(self.config, "tautulli_webhook_secret", None),
            "seerr": getattr(self.config, "seerr_webhook_secret", None),
            "plex": getattr(self.config, "plex_webhook_secret", None),
        }
        return secret_map.get(service)

    def _validate_arr_signature(
        self,
        request: web.Request,
        body: bytes,
        secret: str,
        service: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Validate Sonarr/Radarr webhook signature.
        
        Sonarr/Radarr use X-Api-Key header for authentication.
        """
        # Check X-Api-Key header (standard Sonarr/Radarr auth)
        api_key = request.headers.get("X-Api-Key")
        
        if api_key:
            return _check(api_key, secret, body, f"{service.title()} webhook: Invalid API key", "Invalid API key")

        # Check custom signature header (if configured)
        signature = request.headers.get("X-Webhook-Signature") or request.headers.get("X-Signature-256")
        
        if signature:
            expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            return _check(signature.replace("sha256=", ""), expected, body,
                          f"{service.title()} webhook: Invalid signature", "Invalid signature")

        # No auth header found
        logger.warning(f"{service.title()} webhook: No authentication header found")
        return False, "Missing authentication", None

    def _validate_tautulli_signature(
        self,
        request: web.Request,
        body: bytes,
        secret: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Validate Tautulli webhook signature.
        
        Tautulli can send a custom header with a shared secret.
        Configure in Tautulli: Settings > Notifications > Webhook > Headers
        """
        # Check for custom auth header
        auth_header = (
            request.headers.get("X-Tautulli-Signature") or
            request.headers.get("X-Webhook-Secret") or
            request.headers.get("Authorization")
        )
        
        if auth_header:
            return _check(_strip_bearer(auth_header), secret, body, "Tautulli webhook: Invalid signature/secret", "Invalid signature")

        # Check query parameter as fallback
        query_secret = request.query.get("secret")
        if query_secret:
            result = _check(query_secret, secret, body, "Tautulli webhook: Invalid query secret", "Invalid secret")
            if result[0]:
                _warn_query_secret()
            return result

        logger.warning("Tautulli webhook: No authentication found")
        return False, "Missing authentication", None

    def _validate_seerr_signature(
        self,
        request: web.Request,
        body: bytes,
        secret: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Validate Seerr webhook signature.
        
        Seerr supports webhook authentication via Authorization header.
        """
        auth_header = request.headers.get("Authorization")
        
        if auth_header:
            return _check(_strip_bearer(auth_header), secret, body, "Seerr webhook: Invalid authorization", "Invalid authorization")

        logger.warning("Seerr webhook: No authorization header")
        return False, "Missing authorization", None

    def _validate_plex_signature(
        self,
        request: web.Request,
        body: bytes,
        secret: str
    ) -> Tuple[bool, Optional[str], Optional[bytes]]:
        """
        Validate Plex webhook.
        
        Plex webhooks include the Plex token, which we can validate.
        """
        plex_token = request.query.get("token") or request.headers.get("X-Plex-Token")

        if plex_token:
            return _check(plex_token, secret, body, "Plex webhook: Invalid token", "Invalid token")

        # Fail closed. This branch is only reached when a secret IS configured,
        # and previously returned True - meaning an attacker bypassed validation
        # simply by omitting the token. If no secret is configured,
        # validate_request short-circuits before ever calling this.
        logger.warning("Plex webhook: No token provided but a secret is configured")
        return False, "Missing token", None


def _warn_query_secret() -> None:
    """Once a run: a secret in the URL ends up in proxy and access logs."""
    global _warned_query_secret
    if _warned_query_secret:
        return
    _warned_query_secret = True
    logger.warning(
        "Tautulli sends its webhook secret in the URL, where proxies and access logs "
        "can keep it. Send it in a header instead: press Connect live updates on the setup page and "
        "remove the hand-made webhook, or set that webhook's JSON headers to "
        '{"Authorization": "Bearer <secret>"}.'
    )


def _strip_bearer(value: str) -> str:
    """The token from an Authorization header, with or without "Bearer "."""
    return value[7:] if value.startswith("Bearer ") else value


def _check(provided: str, secret: str, body: bytes, log: str, error: str) -> Tuple[bool, Optional[str], Optional[bytes]]:
    """Compare a presented secret in constant time: (True, None, body) on a match;
    otherwise log `log` and refuse with `error`."""
    if secure_equals(provided, secret):
        return True, None, body
    logger.warning(log)
    return False, error, None


def create_validated_handler(original_handler, validator: WebhookValidator, service: str):
    """
    Decorator factory to wrap webhook handlers with signature validation.
    
    Usage:
        validated_handler = create_validated_handler(my_handler, validator, "sonarr")
    """
    async def validated_wrapper(request: web.Request) -> web.Response:
        is_valid, error, body = await validator.validate_request(request, service)
        
        if not is_valid:
            logger.warning(f"Rejected {service} webhook: {error}")
            return web.json_response(
                {"error": error, "service": service},
                status=401
            )
        
        # Store the pre-read body for the handler to use
        request["_validated_body"] = body
        
        return await original_handler(request)
    
    return validated_wrapper
