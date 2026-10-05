# path: webhooks/arr_handler.py
"""What the Radarr and Sonarr webhooks share: reading the event, handing it to
that app's handler for it, the reply, and registering the route behind its
secret (WebhookServer.add_validated_post, never the router directly)."""
from typing import Any, Awaitable, Callable, Dict

from aiohttp import web

from core.logging import get_logger

logger = get_logger(__name__)

Handler = Callable[[Dict[str, Any]], Awaitable[None]]


def register_arr_webhook(webhook_server, app: str, handlers: Dict[str, Handler]) -> None:
    """Serve /webhook/<app> for one of the *arr apps. `handlers` maps its eventType
    ("Grab", "Download", ...) to a coroutine taking the payload; "Test" is answered
    here. A handler's own failure is logged and the app still gets a 200, so it
    doesn't retry an event Plexbie has already seen."""
    name = app.lower()

    async def endpoint(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            event = payload.get("eventType")
            logger.debug(f"Received {app} webhook: {event}")
            if event == "Test":
                logger.info(f"{app} webhook test successful")
            elif event in handlers:
                try:
                    await handlers[event](payload)
                except Exception as e:
                    logger.error(f"Error handling {app} {event} event: {e}", exc_info=True)
            return web.json_response({"status": "ok"})
        except Exception as e:
            logger.error(f"Error processing {app} webhook: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=400)

    webhook_server.add_validated_post(f"/webhook/{name}", endpoint, name)
    logger.info(f"✅ Registered {app} webhook handler at /webhook/{name}")
