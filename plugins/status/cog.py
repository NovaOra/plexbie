# path: plugins/status/cog.py
"""/say: a message from Plexbie in any channel (admins)."""
import discord
from discord import app_commands
from discord.ext import commands

from core.logging import get_logger
from core.permissions import require_admin
from core.services import BotServices

logger = get_logger(__name__)


class StatusCog(commands.Cog):
    """Admin messages as Plexbie."""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
    
    @app_commands.command(name="say", description="Make Plexbie send a message")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(
        channel="The channel to send the message to",
        message="The message content"
    )
    async def say_command(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str
    ):
        """Make the bot send a message to a channel"""
        if not await require_admin(interaction):
            return

        try:
            # Send the message to the specified channel
            # A Plexbie admin's own words: their pings work, as typed.
            await channel.send(message, allowed_mentions=discord.AllowedMentions.all())

            # Confirm to the user (privately)
            await interaction.response.send_message(
                f"Message sent to {channel.mention}!",
                ephemeral=True
            )

            logger.info(f"{interaction.user} used /say in {channel.name}: {message}")

        except discord.Forbidden:
            await interaction.response.send_message(
                f"I don't have permission to send messages in {channel.mention}.",
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in say command: {e}")
            await interaction.response.send_message(
                f"Failed to send message: {str(e)}",
                ephemeral=True
            )


