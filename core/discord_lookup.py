"""The home guild and the admin channel, looked up the same way everywhere.

Each returns None when the setting is unset, the bot is missing, or the bot
can't see it; callers decide what that means for them (skip, log, or refuse).
"""
from __future__ import annotations


def home_guild(bot, config):
    """The configured guild from the bot's cache, or None."""
    return bot.get_guild(config.guild_id) if (bot and config.guild_id) else None


def admin_channel(bot, config):
    """The admin channel from the bot's cache, or None."""
    return bot.get_channel(int(config.admin_channel_id)) if (bot and config.admin_channel_id) else None


async def resolve_channel(bot, channel_id: int):
    """A channel from the cache, else fetched from Discord (which may raise)."""
    return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
