# path: core/webhook_connect.py
"""Point Seerr's and Tautulli's webhooks at Plexbie, through their own APIs.

So nobody has to copy a URL, a secret and a JSON template into another app:
the setup page's "Connect" buttons call these. What they set up:

  Seerr  Settings > Notifications > Webhook: on, Plexbie's URL, the
             Authorization secret, and webhooks/seerr_handler.PAYLOAD for
             the request events Plexbie uses.
  Tautulli   a Webhook notification agent named "Plexbie": Plexbie's URL, a
             Bearer secret header, and webhooks/tautulli_handler.BODY on the
             playback and server up/down triggers. Reused if it already exists;
             the secret goes in a POST body where Tautulli takes one, and is
             only sent again when it changed.

Plexbie's webhook listener has to be reachable from those apps, so connecting
also makes sure every webhook route has a secret (new_secrets) before the
listener opens to the network (WEBHOOK_BIND=0.0.0.0).
"""
import json
import secrets
from typing import Dict, Mapping

from core.clients import ServiceError

#: Every route the listener serves, by its secret setting.
SECRET_KEYS = ("SONARR_WEBHOOK_SECRET", "RADARR_WEBHOOK_SECRET", "TAUTULLI_WEBHOOK_SECRET",
               "SEERR_WEBHOOK_SECRET", "PLEX_WEBHOOK_SECRET")
TAUTULLI_AGENT_ID = 25          # Tautulli's Webhook agent
#: Triggers earlier versions turned on and this one doesn't want (remote access flaps).
RETIRED_TRIGGERS = ("on_extdown", "on_extup")
AGENT_NAME = "Plexbie"


def new_secrets(current: Mapping[str, str]) -> Dict[str, str]:
    """Secrets for the routes that have none yet, so no route is open once the
    listener accepts connections from the network."""
    return {k: secrets.token_urlsafe(24) for k in SECRET_KEYS if not (current.get(k) or "").strip()}


def _payload(encoding: str) -> str:
    """The JSON template as each version wants it. Version 1.x stores the text it's
    given and later parses it twice, so it needs JSON-encoded JSON; Seerr 2.x and
    later encode it once more themselves, so they need it plain. Sent
    the wrong way, every notification fails ('"[object Object]" is not valid JSON')."""
    from webhooks.seerr_handler import PAYLOAD
    text = json.dumps(PAYLOAD)
    return json.dumps(text) if encoding == "double" else text


async def connect_seerr(services, base_url: str, secret: str, *, tested_only: bool = False) -> str:
    """Point Seerr's webhook at Plexbie.

    The template encoding differs between them (see _payload). The version says
    which to try first; Seerr's own Test then proves it, and the other
    encoding is tried if it fails. When Plexbie can't be reached to test (setup,
    before the listener runs), the version's choice is saved untested, unless
    tested_only (the start-up refresh, when the listener is up: a failed test
    then means something's wrong, and the working settings are left alone)."""
    from webhooks.seerr_handler import TYPES
    try:
        version = str((await services.seerr.get("status") or {}).get("version") or "1")
    except ServiceError:
        version = "1"
    first = "double" if version.split(".")[0] in ("0", "1") else "single"
    order = [first, "single" if first == "double" else "double"]

    def settings(encoding: str) -> dict:
        return {"enabled": True, "types": TYPES,
                "options": {"webhookUrl": f"{base_url}/webhook/seerr", "authHeader": secret,
                            "jsonPayload": _payload(encoding)}}

    for encoding in order:
        try:
            await services.seerr.post("settings/notifications/webhook/test", settings(encoding))
        except ServiceError:
            continue
        await services.seerr.post("settings/notifications/webhook", settings(encoding))
        return "Seerr now tells Plexbie about requests as they happen (tested)."
    if tested_only:
        raise ServiceError("Seerr couldn't reach Plexbie with either payload; its webhook was left as it was")
    await services.seerr.post("settings/notifications/webhook", settings(first))
    return ("Seerr now tells Plexbie about requests as they happen. "
            "It couldn't send a test yet; it's checked again whenever Plexbie starts.")


async def connect_tautulli(services, base_url: str, secret: str) -> str:
    from webhooks.tautulli_handler import BODY, TRIGGERS
    tautulli = services.tautulli
    notifier = await _plexbie_notifier(tautulli)
    stored = {}
    if notifier is not None:
        stored = (await _notifier_config(tautulli, notifier)).get("notify_text")
        stored = stored if isinstance(stored, dict) else {}
    else:
        before = {n.get("id") for n in await tautulli.call("get_notifiers") or []}
        await tautulli.call("add_notifier_config", agent_id=TAUTULLI_AGENT_ID)
        added = [n for n in await tautulli.call("get_notifiers") or []
                 if n.get("id") not in before and n.get("agent_id") == TAUTULLI_AGENT_ID]
        if not added:
            raise ServiceError("Tautulli didn't add the webhook")
        notifier = max(added, key=lambda n: n.get("id") or 0)
    params = {"notifier_id": notifier["id"], "friendly_name": AGENT_NAME,
              "webhook_hook": f"{base_url}/webhook/tautulli", "webhook_method": "POST"}
    headers = json.dumps({"Authorization": f"Bearer {secret}"})
    for trigger in RETIRED_TRIGGERS:
        params[trigger] = 0                         # switched on by earlier versions
    for trigger in TRIGGERS:
        params[trigger] = 1
        text = stored.get(trigger)
        if not isinstance(text, dict) or text.get("subject") != headers:
            params[f"{trigger}_subject"] = headers  # the Webhook agent's "JSON headers", only when changed
        params[f"{trigger}_body"] = BODY            # ...and its "JSON data"
    await tautulli.call("set_notifier_config", post=True, **params)
    return "Tautulli now tells Plexbie about playback and Plex going up or down."


async def refresh_connections(services, base_url: str) -> None:
    """On start: bring webhooks Plexbie set up earlier in line with this version
    (new fields, triggers). Only ones that already point at Plexbie; nothing is
    connected that wasn't. Each keeps the address it already uses (the operator
    may have pointed it somewhere on purpose); base_url is only for when that
    can't be read."""
    from core.logging import get_logger
    log = get_logger(__name__)
    cfg = services.config
    if services.seerr.configured and cfg.seerr_webhook_secret:
        try:
            current = await services.seerr.get("settings/notifications/webhook") or {}
            url = str((current.get("options") or {}).get("webhookUrl", ""))
            # Plexbie's address, new or as older installs set it ("/webhook/overseerr"):
            # refreshed, and moved to the new one.
            ours = next((p for p in ("/webhook/seerr", "/webhook/overseerr") if url.endswith(p)), None)
            if ours:
                await connect_seerr(services, url[:-len(ours)] or base_url,
                                        cfg.seerr_webhook_secret, tested_only=True)
        except ServiceError as e:
            log.warning(f"Couldn't refresh Seerr's webhook: {e}")
    if services.tautulli.configured and cfg.tautulli_webhook_secret:
        try:
            notifier = await _plexbie_notifier(services.tautulli)
            if notifier:
                url = await _tautulli_hook(services.tautulli, notifier)
                base = url[:-len("/webhook/tautulli")] if url.endswith("/webhook/tautulli") else base_url
                await connect_tautulli(services, base or base_url, cfg.tautulli_webhook_secret)
        except ServiceError as e:
            log.warning(f"Couldn't refresh Tautulli's webhook: {e}")


async def _notifier_config(tautulli, notifier) -> dict:
    """The notifier's stored settings ({} if they can't be read)."""
    try:
        cfg = await tautulli.call("get_notifier_config", notifier_id=notifier["id"]) or {}
    except ServiceError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


async def _tautulli_hook(tautulli, notifier) -> str:
    """The address Plexbie's Tautulli notifier sends to now ("" if it can't be read)."""
    cfg = await _notifier_config(tautulli, notifier)
    options = cfg.get("config") if isinstance(cfg.get("config"), dict) else cfg
    return str(options.get("hook") or options.get("webhook_hook") or "")


async def _plexbie_notifier(tautulli):
    for n in await tautulli.call("get_notifiers") or []:
        if n.get("agent_id") == TAUTULLI_AGENT_ID and (n.get("friendly_name") or "") == AGENT_NAME:
            return n
    return None
