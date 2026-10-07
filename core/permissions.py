# path: core/permissions.py
"""Shared authorization helpers for commands and interactive views.

The codebase historically used two different admin conventions:
  * ``interaction.user.guild_permissions.administrator`` (media_cleanup, user_mgmt, status)
  * a configured ``ADMIN_ROLE_ID`` (invite_tracker)

``is_bot_admin`` accepts either, plus the configured bot owner, so adopting it
does not revoke access from anyone who had it before. Administrator and
``ADMIN_ROLE_ID`` count only in the household's own server (``GUILD_ID``): anyone
can make themselves Administrator of a server of their own and add Plexbie to it.
The bot owner counts everywhere.

``HomeGuildTree`` puts the same server check in front of every slash command.
"""
import functools
import logging
import time
from typing import Optional

import discord
from discord import app_commands

from core.logging import get_logger

logger = get_logger(__name__)

DENIED_MESSAGE = "⛔ You don't have permission to use this."
FOREIGN_MESSAGE = "Plexbie only works in its household's Discord server."
STARTING_MESSAGE = "Plexbie is just starting up. Try again in a moment."
UNBOUND_MESSAGE = ("Plexbie's commands and admin buttons are off until its admin sets which Discord server "
                   "is home (GUILD_ID).")

#: Seconds between WARNING lines about refusals from the same server; the rest go to DEBUG.
REFUSAL_LOG_EVERY = 3600
_refusals_logged: dict = {}


def _config_from_interaction(interaction: discord.Interaction):
    """Resolve the bot config off an interaction, or None if unavailable.

    Read from the client rather than a constructor argument so that persistent
    views registered with no arguments (``bot.add_view(SomeView())``) are still
    able to authorize.
    """
    services = getattr(interaction.client, "services", None)
    return getattr(services, "config", None)


def is_owner(interaction: discord.Interaction) -> bool:
    """True for the configured bot owner, wherever the interaction comes from."""
    config = _config_from_interaction(interaction)
    return bool(config is not None and config.bot_owner_id and interaction.user.id == config.bot_owner_id)


def in_home_guild(interaction: discord.Interaction) -> bool:
    """True when the interaction comes from the household's server (GUILD_ID).

    False in a DM, in any other server, and while GUILD_ID is unknown.
    """
    config = _config_from_interaction(interaction)
    home = getattr(config, "guild_id", None)
    return bool(home) and getattr(interaction, "guild_id", None) == home


def is_bot_admin(interaction: discord.Interaction) -> bool:
    """Return True if the interacting user may perform administrative actions.

    Fails closed: anything unexpected (DM context, missing config, member not
    resolvable, another server, no GUILD_ID) denies access rather than allowing
    it. The bot owner is always permitted so a misconfigured guild cannot lock
    everyone out.
    """
    user = interaction.user
    config = _config_from_interaction(interaction)

    if is_owner(interaction):
        return True

    # Administrator of a server someone added Plexbie to means nothing here.
    if not in_home_guild(interaction):
        return False

    # Guild administrator permission. Absent on discord.User (i.e. in DMs).
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True

    # Explicitly configured admin role.
    if config is not None and config.admin_role_id:
        roles = getattr(user, "roles", None)
        if roles and any(role.id == config.admin_role_id for role in roles):
            return True

    return False


def _unbound_message(interaction: discord.Interaction) -> str:
    """GUILD_ID is blank: still being worked out at start-up, or no server could be proved."""
    if not getattr(getattr(interaction, "client", None), "_home_settled", True):
        return STARTING_MESSAGE
    return UNBOUND_MESSAGE


def refusal_message(interaction: discord.Interaction) -> str:
    """What to tell someone ``is_bot_admin`` turned away.

    With GUILD_ID blank even the household's own admins are refused, so they're
    told that rather than that they lack permission.
    """
    config = _config_from_interaction(interaction) if hasattr(interaction, "client") else None
    if config is None or getattr(config, "guild_id", None):
        return DENIED_MESSAGE
    return _unbound_message(interaction)


async def deny(interaction: discord.Interaction, message: Optional[str] = None) -> None:
    """Send the standard ephemeral refusal, whether or not we already responded."""
    message = message or refusal_message(interaction)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException as e:
        logger.debug(f"Could not deliver permission refusal: {e}")


async def require_admin(interaction: discord.Interaction) -> bool:
    """Guard for command bodies. Returns True if allowed, else denies and returns False.

    Usage:
        if not await require_admin(interaction):
            return
    """
    if is_bot_admin(interaction):
        return True

    logger.warning(
        f"Denied admin action to {interaction.user} ({interaction.user.id}) "
        f"in server {getattr(interaction, 'guild_id', None)}: "
        f"{getattr(interaction.command, 'name', None) or 'component interaction'}"
    )
    await deny(interaction)
    return False


def _log_refusal(interaction: discord.Interaction, config) -> None:
    """One WARNING an hour per server: a stranger's server can't flood the log."""
    guild_id = getattr(interaction, "guild_id", None)
    now = time.monotonic()
    last = _refusals_logged.get(guild_id)
    level = logging.DEBUG
    if last is None or now - last > REFUSAL_LOG_EVERY:
        level = logging.WARNING
        _refusals_logged[guild_id] = now
    name = (getattr(interaction, "data", None) or {}).get("name")
    home = getattr(config, "guild_id", None)
    logger.log(level, f"Refused /{name} from {interaction.user} ({interaction.user.id}) in server "
                      f"{guild_id or 'a DM'}: Plexbie only answers in its household's server "
                      f"({home or 'none: GUILD_ID is blank'}).")


async def home_guild_check(interaction: discord.Interaction) -> bool:
    """Commands and autocomplete answer only in the household's server, or for the bot owner.

    Refused interactions are answered (an empty list for autocomplete, which
    can't show a message) and logged.
    """
    if is_owner(interaction) or in_home_guild(interaction):
        return True

    config = _config_from_interaction(interaction)
    message = FOREIGN_MESSAGE if getattr(config, "guild_id", None) else _unbound_message(interaction)
    try:
        if interaction.type is discord.InteractionType.autocomplete:
            await interaction.response.autocomplete([])
        else:
            await deny(interaction, message)
    except discord.HTTPException as e:
        logger.debug(f"Could not deliver the refusal: {e}")
    _log_refusal(interaction, config)
    return False


class HomeGuildTree(app_commands.CommandTree):
    """The command tree, bound to the household's server.

    discord.py runs ``interaction_check`` first, before it looks the command up,
    so it covers every slash command, autocomplete and context menu, ahead of
    each command's own checks. Admin commands then check ``require_admin`` (or
    ``is_bot_admin``) themselves, never Discord's Administrator permission
    alone, which would refuse ADMIN_ROLE_ID and the bot owner.

    Every command is guild_only and synced to the home server only, so every DM
    is refused except the bot owner's; a command meant for DMs has to be let
    through here on purpose.
    """

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        return await home_guild_check(interaction)


class AdminOnlyView(discord.ui.View):
    """A View whose every component is restricted to administrators.

    discord.py calls ``interaction_check`` before dispatching to any item
    callback, so subclasses get the gate without repeating it per button. This
    is the authoritative check: an ephemeral delivery is not a permission
    boundary, and persistent views outlive the message they were sent with.
    """

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_bot_admin(interaction):
            return True

        logger.warning(
            f"Denied {self.__class__.__name__} interaction to "
            f"{interaction.user} ({interaction.user.id}) in server {getattr(interaction, 'guild_id', None)}"
        )
        await deny(interaction)
        return False


class AdminActionView(AdminOnlyView):
    """An AdminOnlyView whose buttons cannot be acted on twice at once.

    Disabling the buttons and editing the message is not a guard. It is a
    client-side render, and it takes a round-trip - Discord can deliver a second
    interaction before the first edit lands. For a button that has an external
    side effect (submitting to Seerr, queueing a download, inviting a Plex
    user) that means the side effect happens twice.

    The guard is an in-process set of message ids being acted on. Shared per
    subclass rather than per instance, because each request is posted with a fresh
    view while the bot is up but every pending request falls back to the single
    argument-less instance registered at startup - an instance-level set would not
    span both.

    Usage:

        if not await self.claim(interaction):
            return
        try:
            ...
        finally:
            self.release(interaction)
    """

    #: Replaced per subclass by __init_subclass__, so two view types cannot
    #: collide on a shared registry.
    _in_flight: set = set()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._in_flight = set()

    @staticmethod
    def _key(interaction: discord.Interaction):
        message = getattr(interaction, "message", None)
        return getattr(message, "id", None)

    async def claim(self, interaction: discord.Interaction) -> bool:
        """Reserve this message. False if another click is already being handled.

        On a refusal the interaction is answered, so the caller only has to return.
        """
        key = self._key(interaction)
        if key is None:
            # No message to key on; nothing to protect against.
            return True

        if key in self._in_flight:
            logger.info(
                f"Ignoring duplicate {type(self).__name__} action on message {key} "
                f"from {interaction.user}: one is already in progress"
            )
            try:
                await interaction.response.send_message(
                    "That request is already being handled - give it a moment.",
                    ephemeral=True,
                )
            except discord.HTTPException as e:
                logger.debug(f"Could not answer duplicate interaction: {e}")
            return False

        self._in_flight.add(key)
        return True

    def release(self, interaction: discord.Interaction) -> None:
        """Release the reservation. Safe to call even if claim() was never called."""
        key = self._key(interaction)
        if key is not None:
            self._in_flight.discard(key)


def single_flight(handler):
    """Decorate a button callback so two clicks cannot run it concurrently.

    Apply it *under* @discord.ui.button, so the button decorator receives the
    wrapped coroutine:

        @discord.ui.button(label="Approve", custom_id="approve")
        @single_flight
        async def approve(self, interaction, button): ...

    Requires the view to be an AdminActionView (or anything providing claim and
    release). The duplicate click is answered by claim(), so the handler simply
    does not run.
    """

    @functools.wraps(handler)
    async def wrapper(self, interaction, button):
        if not await self.claim(interaction):
            return
        try:
            return await handler(self, interaction, button)
        finally:
            self.release(interaction)

    return wrapper
