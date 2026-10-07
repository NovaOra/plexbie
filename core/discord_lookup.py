"""The home guild, the admin channel and the admins, looked up the same way everywhere.

Each returns None when the setting is unset, the bot is missing, or the bot
can't see it; callers decide what that means for them (skip, log, or refuse).
require_admin_channel is for callers that refuse: it raises, saying which.

Also how Plexbie works out its household's server when GUILD_ID is blank, and
what Manage → Health says about the servers it's in.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Set, Tuple

import discord


def home_guild(bot, config):
    """The configured guild from the bot's cache, or None."""
    return bot.get_guild(config.guild_id) if (bot and config.guild_id) else None


def is_home(config, guild) -> bool:
    """True for the household's server (a guild or its id). False for any other,
    for no server at all (a DM), and while GUILD_ID is blank."""
    home = getattr(config, "guild_id", None)
    gid = getattr(guild, "id", guild)
    return bool(home) and gid is not None and int(gid) == int(home)


async def is_household_member(bot, config, user_id: int, ask: bool = True) -> bool:
    """True when the user is a member of the household's server.

    The member cache first (full while the Server Members intent is on), else
    Discord is asked (ask=False: the cache only). Discord is asked for the server
    too while it isn't cached (start-up, or Discord briefly without it). False
    while GUILD_ID is blank, when Plexbie isn't in that server, and when Discord
    says no or can't be asked.
    """
    guild = home_guild(bot, config)
    if guild is not None and guild.get_member(user_id) is not None:
        return True
    if not ask or not (bot and config.guild_id):
        return False
    try:
        if guild is None:
            guild = await bot.fetch_guild(config.guild_id)
        await guild.fetch_member(user_id)
        return True
    except Exception:       # NotFound, Forbidden, any other HTTP error or no connection: not shown to be a member
        return False


def admin_channel(bot, config):
    """The admin channel from the bot's cache, or None."""
    return bot.get_channel(int(config.admin_channel_id)) if (bot and config.admin_channel_id) else None


def admin_channel_of(bot):
    """The admin channel, with the settings taken from the bot itself, or None."""
    config = getattr(getattr(bot, "services", None), "config", None)
    return admin_channel(bot, config) if config else None


class AdminChannelUnavailable(RuntimeError):
    """The admin channel is unset or the bot cannot see it, so nothing was posted."""


def require_admin_channel(bot, config):
    """The admin channel, else AdminChannelUnavailable: not configured, or not found."""
    if not config.admin_channel_id:
        raise AdminChannelUnavailable("Admin channel not configured")
    channel = admin_channel(bot, config)
    if channel is None:
        raise AdminChannelUnavailable(f"Admin channel {config.admin_channel_id} not found")
    return channel


def admin_discord_ids(bot, config) -> Set[str]:
    """Discord ids of whoever gets the admins' alerts: the bot owner, and the household
    server's Administrators and ADMIN_ROLE_ID holders (none while it can't be seen)."""
    owner = getattr(config, "bot_owner_id", None)
    role = getattr(config, "admin_role_id", None)
    ids = {str(owner)} if owner else set()
    guild = home_guild(bot, config)
    for m in (guild.members if guild else []):
        if m.guild_permissions.administrator or (role and role in {r.id for r in m.roles}):
            ids.add(str(m.id))
    return ids


async def resolve_channel(bot, channel_id: int):
    """A channel from the cache, else fetched from Discord (which may raise)."""
    return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)


# ------------------------------------------------------------ which server is home
#: Settings that name something inside the household's server.
CHANNEL_KEYS = ("admin_channel_id", "updates_channel_id", "stats_channel_id", "watch_party_channel_id")
ROLE_KEYS = ("admin_role_id", "plex_member_role_id", "arrivals_role_id")

PUBLIC_BOT_FIX = ("Turn it off: Discord Developer Portal → your app → Installation → Install Link: None → Save, "
                  "then Bot → Public Bot off → Save.")


def servers(guilds: Iterable, limit: int = 5) -> str:
    """'Name' (id), ... for a log line or Health: a few of them, names cut short."""
    guilds = list(guilds)
    shown = ", ".join(f"'{(g.name or '')[:40]}' ({g.id})" for g in guilds[:limit])
    more = len(guilds) - limit
    return (shown + (f", +{more} more" if more > 0 else "")) or "none"


def evidence_guilds(guilds: Iterable, config) -> Tuple[Set[int], bool]:
    """The servers holding a channel or role from the settings, and whether any is set.

    IDs are unique across Discord, so a stranger's server can't contain the
    household's channels or roles.
    """
    channels = [int(v) for v in (getattr(config, k, None) for k in CHANNEL_KEYS) if v]
    roles = [int(v) for v in (getattr(config, k, None) for k in ROLE_KEYS) if v]
    found = {g.id for g in guilds
             if any(g.get_channel(c) for c in channels) or any(g.get_role(r) for r in roles)}
    return found, bool(channels or roles)


async def pick_home_guild(bot, config) -> Tuple[Optional["discord.Guild"], str]:
    """With GUILD_ID blank: the household's server if it can be proved, and why.

    The reason is one of: settings, owner (picked); conflict, elsewhere,
    not_owner, owner_unknown, none, several (not picked: see problem_text).
    Proof is the settings' channels or roles being in exactly one of Plexbie's
    servers, or, with none of those set, Plexbie being in a single server that
    the bot's owner owns. Nothing else: the first stranger to add a Public Bot
    must not become its household. Owning, not just belonging: anyone who runs
    a server the owner happens to be in could add Plexbie there.
    """
    guilds = list(bot.guilds)
    if not guilds:
        return None, "none"
    found, any_ids = evidence_guilds(guilds, config)
    if len(found) > 1:
        return None, "conflict"
    if found:
        return next(g for g in guilds if g.id in found), "settings"
    if any_ids:
        return None, "elsewhere"
    if len(guilds) > 1:
        return None, "several"
    guild = guilds[0]
    try:
        app = await bot.application_info()
    except Exception:
        return None, "owner_unknown"    # fail closed: no owner, no pick
    owners = {getattr(config, "bot_owner_id", None), getattr(getattr(app, "owner", None), "id", None),
              getattr(getattr(app, "team", None), "owner_id", None)} - {None}
    if guild.owner_id is not None and int(guild.owner_id) in {int(uid) for uid in owners}:
        return guild, "owner"
    return None, "not_owner"


def problem_text(reason: str, guilds: Iterable) -> str:
    """Why commands are off with GUILD_ID blank, and the fix: for the log and Health."""
    guilds = list(guilds)
    listed = servers(guilds)
    if reason == "none":
        return ("GUILD_ID is blank and Plexbie isn't in any Discord server yet, so commands and admin buttons are "
                "off. Add it to your household's server (setup page → Add Plexbie to my server). They come on once "
                "it joins a server the bot's owner owns, or set GUILD_ID and restart.")
    if reason == "several":
        return (f"GUILD_ID is blank and Plexbie is in {len(guilds)} servers ({listed}), so it can't tell which is "
                "your household's. Commands and admin buttons are off until you set GUILD_ID to one of these "
                "and restart.")
    if reason == "elsewhere":
        return ("GUILD_ID is blank, and the channels and roles in config/.env belong to a server Plexbie isn't in. "
                f"Commands and admin buttons are off. Add Plexbie to that server, or set GUILD_ID and restart. Plexbie is in: {listed}.")
    if reason == "conflict":
        return (f"GUILD_ID is blank and your channel/role settings point at more than one server ({listed}), so "
                "commands and admin buttons are off. Set GUILD_ID and restart.")
    if reason == "not_owner":
        return (f"GUILD_ID is blank and Plexbie is only in {listed}, but the bot's owner doesn't own that server, so "
                "Plexbie won't treat it as your household's and commands and admin buttons are off. Set GUILD_ID "
                "and restart.")
    return ("GUILD_ID is blank and Plexbie couldn't ask Discord who owns the bot, so it won't pick a server and "
            "commands and admin buttons are off. It will try again at the next start, or set GUILD_ID.")


def not_in_text(guild_id: int, guilds: Iterable) -> str:
    """GUILD_ID names a server Plexbie isn't in."""
    return (f"GUILD_ID={guild_id} is a server Plexbie isn't in. Its commands and admin buttons won't work until "
            "Plexbie is added there (setup page link); they appear as soon as it joins. "
            f"Plexbie is in: {servers(guilds)}.")


def home_health_items(bot, config) -> List[dict]:
    """Manage → Health: the server Plexbie answers in, and any others it's in."""
    gid = config.guild_id
    ready = getattr(bot, "is_ready", None)
    if ready is not None and not ready():
        return [{"name": "Discord server", "ok": False, "ms": 0, "detail": "Plexbie hasn't connected to Discord yet."}]
    guilds = list(bot.guilds)
    home = bot.get_guild(gid) if gid else None
    if not gid:
        detail, ok = getattr(bot, "_home_problem", None) or (
            "GUILD_ID is blank, so Plexbie's commands and admin buttons are off. Set it on the setup page or in "
            "config/.env and restart."), False
    elif home is None:
        detail, ok = not_in_text(gid, guilds), False
    elif getattr(bot, "_synced", None) is None:
        # In the server, but Discord refused the commands (or was down) at start-up.
        detail, ok = (f"Plexbie is in '{(home.name or '')[:40]}' but couldn't add its commands there (the log says "
                      "why), so they may be missing. If it was added without the applications.commands scope, add "
                      "it again with the setup page's link, then restart Plexbie."), False
    else:
        detail, ok = None, True
    items = [{"name": "Discord server", "ok": ok, "ms": 0, "detail": detail}]
    others = [g for g in guilds if g.id != gid] if gid else []
    if others:
        items.append({"name": "Other Discord servers", "ok": False, "ms": 0, "detail": (
            f"Plexbie is also in {servers(others)}. It ignores commands and admin buttons there. Moving your "
            "household? Set GUILD_ID to the new server's ID and restart. Didn't add it? Remove Plexbie from that "
            "server and turn off Public Bot.")})
    return items


async def clear_global_commands(bot) -> int:
    """Remove every global command from Discord; how many there were.

    Straight to Discord rather than tree.clear_commands(guild=None) + tree.sync():
    that would empty the in-memory command set too, which copy_global_to copies
    from, so a later sync to the household's server (Plexbie joining it after
    start-up) would push no commands at all.
    """
    stale = await bot.tree.fetch_commands()
    if stale:
        await bot.http.bulk_upsert_global_commands(bot.application_id, payload=[])
    return len(stale)
