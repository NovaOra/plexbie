# path: portal/server.py
"""Starts plexbie.com inside the bot, beside the webhook listener.

Off unless WEB_PORT is set. Inside the bot the portal has what the read-only
preview lacks: the Discord client (roles, admin cards, DMs), so sign-in and
every action work. A failure here is logged and never stops the bot starting.
"""
import os
import secrets
from pathlib import Path
from typing import Optional

from aiohttp import web

from core.logging import get_logger
from portal.actions import Actions
from portal.app import build_app
from portal.auth import Auth
from portal.cache import TTLCache
from portal.data import Data
from portal.invites import Invites

logger = get_logger(__name__)


SECRET_FILE = Path("config") / ".web_session_secret"


def _stored_secret() -> Optional[str]:
    """The session-signing secret kept in config/, made on first start.

    So the website works out of the box: set WEB_SESSION_SECRET only to choose
    your own. It never leaves the config directory (owner-only permissions).
    """
    try:
        if SECRET_FILE.is_file():
            value = SECRET_FILE.read_text().strip()
            if len(value) >= 32:
                return value
        value = secrets.token_urlsafe(48)
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Created owner-only from the start, not written and then chmod-ed.
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
        os.chmod(SECRET_FILE, 0o600)           # an older file made under another umask
        logger.info("Made a session secret for the website in config/.web_session_secret")
        return value
    except OSError as e:
        logger.warning(f"Could not read or store config/.web_session_secret: {e}")
        return None


async def start_portal(bot, services) -> Optional[web.AppRunner]:
    cfg = services.config
    if not cfg.web_port:
        return None
    if not cfg.web_session_secret or len(cfg.web_session_secret) < 32:
        from core.blocking import run_blocking
        if cfg.web_session_secret:
            logger.warning("WEB_SESSION_SECRET is shorter than 32 characters, so it isn't used; the one in "
                           "config/.web_session_secret is. Changing the short one signs nobody out.")
        cfg.web_session_secret = await run_blocking(_stored_secret)
        if not cfg.web_session_secret:
            logger.warning("No WEB_SESSION_SECRET and none could be stored in config/; the web portal was not started")
            return None
    cache = TTLCache()
    data = Data(services, cache)

    async def announce_from_plex():
        arrivals = bot.get_cog("NewMediaAddedCog")
        if arrivals:
            await arrivals.check_recently_added()
    data.progress.on_plex = announce_from_plex
    auth = Auth(bot, services, cache)
    actions = Actions(bot, services, data, cfg.web_public_url)
    bot.portal_actions = actions     # for webhooks that act like the website (portal/help)
    if hasattr(bot, "add_view"):
        from portal.help_view import HelpByNameView
        bot.add_view(HelpByNameView())   # "Search by name" on help alerts, across restarts
    if hasattr(bot, "add_dynamic_items"):
        from portal.ticket_view import TicketOpenButton, TicketReplyButton
        bot.add_dynamic_items(TicketOpenButton, TicketReplyButton)   # tickets from members' DMs
        from portal import inbox
        bot.add_dynamic_items(inbox.ReplyButton, inbox.TicketButton, inbox.DoneButton)   # DM threads
        if hasattr(bot, "tree") and not bot.tree.get_command("reply"):
            bot.tree.add_command(inbox.reply_command)                                   # /reply in a DM thread
    invites = Invites()
    auth.invites = invites
    dist = cfg.web_dist if Path(cfg.web_dist).is_dir() else None
    if dist is None:
        logger.warning(f"Web portal: no built site at {cfg.web_dist}; serving the API only")
    app = build_app(services, who=auth.who, readonly=False, dist=dist, image_cache=cfg.web_image_cache,
                    data=data, auth=auth, actions=actions, invites=invites)
    _announce_app_releases()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.web_bind, cfg.web_port).start()
    logger.info(f"Web portal listening on {cfg.web_bind}:{cfg.web_port}"
                + (f" for {cfg.web_public_url}" if cfg.web_public_url else " (address taken from each request)"))
    return runner


_announcer = None
ANNOUNCE_EVERY = 1800


def _announce_app_releases() -> None:
    """Every half hour: a new app version in config/app/ is announced to app users, once."""
    global _announcer
    import asyncio
    from portal import app_release

    async def loop():
        while True:
            try:
                await app_release.announce()
            except Exception as e:
                logger.warning(f"Announcing the app release failed: {e}")
            await asyncio.sleep(ANNOUNCE_EVERY)
    if _announcer is None or _announcer.done():
        _announcer = asyncio.get_running_loop().create_task(loop())
