# path: utils/views.py
"""Small helpers for discord.ui views shared by the plugins."""
import discord

from core.logging import get_logger

logger = get_logger(__name__)


async def disable_and_refresh(view: discord.ui.View, interaction: discord.Interaction, *, quiet: bool = False) -> None:
    """Grey out every control on `view` and show that on its message: the action is
    done. A message that's gone, or can't be edited, is fine; the action already
    happened. `quiet` logs an edit failure at debug (cancel buttons) instead of warning."""
    for item in view.children:
        item.disabled = True
    try:
        await interaction.message.edit(view=view)
    except discord.NotFound:
        logger.debug("Original message not found, skipping view update")
    except Exception as e:
        (logger.debug if quiet else logger.warning)(f"Could not update view: {e}")


async def reply_failure(interaction: discord.Interaction, log, what: str, error: Exception, *, prefix: str = "❌ Error") -> None:
    """Log a command that failed (to the caller's logger) and tell whoever ran it, privately."""
    log.error(f"{what}: {error}", exc_info=True)
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await send(f"{prefix}: {error}", ephemeral=True)


class RequesterOnlyView(discord.ui.View):
    """A view only the member who started it can use (set `user_id`); anyone else
    pressing it gets a private note and nothing happens."""

    user_id: int

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("This isn't your request!", ephemeral=True)
        return False
