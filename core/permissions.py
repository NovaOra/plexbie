# path: core/permissions.py
"""Shared authorization helpers for commands and interactive views.

The codebase historically used two different admin conventions:
  * ``interaction.user.guild_permissions.administrator`` (media_cleanup, user_mgmt, status)
  * a configured ``ADMIN_ROLE_ID`` (invite_tracker)

``is_bot_admin`` accepts either, plus the configured bot owner, so adopting it
does not revoke access from anyone who had it before.
"""
import functools

import discord

from core.logging import get_logger

logger = get_logger(__name__)

DENIED_MESSAGE = "⛔ You don't have permission to use this."


def _config_from_interaction(interaction: discord.Interaction):
    """Resolve the bot config off an interaction, or None if unavailable.

    Read from the client rather than a constructor argument so that persistent
    views registered with no arguments (``bot.add_view(SomeView())``) are still
    able to authorize.
    """
    services = getattr(interaction.client, "services", None)
    return getattr(services, "config", None)


def is_bot_admin(interaction: discord.Interaction) -> bool:
    """Return True if the interacting user may perform administrative actions.

    Fails closed: anything unexpected (DM context, missing config, member not
    resolvable) denies access rather than allowing it. The bot owner is always
    permitted so a misconfigured guild cannot lock everyone out.
    """
    user = interaction.user
    config = _config_from_interaction(interaction)

    if config is not None and config.bot_owner_id and user.id == config.bot_owner_id:
        return True

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


async def deny(interaction: discord.Interaction) -> None:
    """Send the standard ephemeral refusal, whether or not we already responded."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(DENIED_MESSAGE, ephemeral=True)
        else:
            await interaction.response.send_message(DENIED_MESSAGE, ephemeral=True)
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
        f"Denied admin action to {interaction.user} ({interaction.user.id}): "
        f"{getattr(interaction.command, 'name', None) or 'component interaction'}"
    )
    await deny(interaction)
    return False


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
            f"{interaction.user} ({interaction.user.id})"
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
