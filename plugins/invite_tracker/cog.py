# path: plugins/invite_tracker/cog.py
"""Invite tracking plugin - note which invite each new member used, and who made it"""
import asyncio
import time
import discord
from discord import app_commands
from discord.ext import commands
from typing import Dict, Optional, Tuple
from datetime import datetime, timedelta, timezone
from sqlalchemy import select

from core.discord_lookup import home_guild, is_home
from core.permissions import require_admin
from core.logging import get_logger
from core.services import BotServices
from database.session import get_session
from utils.embeds import create_error_embed
from .models import InviteUse

logger = get_logger(__name__)

# How long a deleted invite is remembered, in case the join that used it up is
# handled after the delete.
USED_UP_SECONDS = 60


class InviteTrackerCog(commands.Cog):
    """Track which invite each member joined with"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        self.invite_cache: Dict[str, Dict[str, discord.Invite]] = {}  # {guild_id: {code: invite}}
        # Invites deleted from the cache lately: {guild_id: {code: (invite, monotonic time)}}
        self.deleted_invites: Dict[str, Dict[str, Tuple[discord.Invite, float]]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    async def cog_load(self):
        """Called when cog is loaded - cache all current invites"""
        logger.info("Invite tracker cog loaded, will cache invites when bot is ready")

    @commands.Cog.listener()
    async def on_ready(self):
        """Cache the household server's invites when bot is ready and connected.

        Only that server: invites in any other server Plexbie was added to are
        none of its business. With GUILD_ID blank there's none yet; on_home_server
        loads it once Plexbie settles on one.
        """
        guild = home_guild(self.bot, self.services.config)
        if guild is None:
            logger.info("Invite cache: the household's server isn't known or reachable yet")
            return
        logger.info("Loading invite cache...")
        try:
            await self._update_invite_cache(guild)
            logger.info(f"✅ Invite cache loaded for {guild.name}")
        except Exception as e:
            logger.error(f"Error loading invite cache: {e}")

    @commands.Cog.listener()
    async def on_home_server(self, guild: discord.Guild):
        """Plexbie made a server home, or joined it, after start-up."""
        if is_home(self.services.config, guild):
            await self._update_invite_cache(guild)

    def _lock(self, guild: discord.Guild) -> asyncio.Lock:
        """One listing-and-compare at a time per server."""
        return self._locks.setdefault(str(guild.id), asyncio.Lock())

    async def _update_invite_cache(self, guild: discord.Guild):
        """Update invite cache for a guild (kept in memory only)"""
        try:
            async with self._lock(guild):
                invites = await guild.invites()
                self.invite_cache[str(guild.id)] = {invite.code: invite for invite in invites}
        except discord.Forbidden:
            logger.warning(f"No permission to fetch invites for guild {guild.id}")
        except Exception as e:
            logger.error(f"Error updating invite cache for {guild.name}: {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Note which invite a new member used, and who made it."""
        if member.bot or not is_home(self.services.config, member.guild):
            return

        guild = member.guild
        logger.info(f"Member {member} joined {guild.name}")

        try:
            # One join at a time, each compared with the listing the one before
            # it left; otherwise two people joining together can both be put
            # down to whichever invite moved first.
            async with self._lock(guild):
                current_invites = await guild.invites()
                cached_invites = self.invite_cache.get(str(guild.id), {})

                logger.debug(f"Current invites: {len(current_invites)}, Cached invites: {len(cached_invites)}")

                # This listing is the new cache. Listing again after the write
                # could already hold the next member's use, and lose their join.
                self.invite_cache[str(guild.id)] = {invite.code: invite for invite in current_invites}
                used_invite = self._find_used_invite(guild, cached_invites, current_invites)

            if not used_invite:
                logger.warning(f"Could not determine which invite {member} used (cached: {len(cached_invites)}, current: {len(current_invites)})")
                return

            logger.info(f"{member} joined via invite {used_invite.code} created by {used_invite.inviter}")

            # The columns for an auto-assigned role stay for older databases; no
            # role is handed out any more.
            auto_role_assigned = False
            assigned_role_id = None

            # Record the invite use in one short write.
            async with get_session() as session:
                session.add(InviteUse(
                    guild_id=str(guild.id),
                    invite_code=used_invite.code,
                    inviter_id=str(used_invite.inviter.id) if used_invite.inviter else "0",
                    inviter_name=str(used_invite.inviter) if used_invite.inviter else "Unknown",
                    joiner_id=str(member.id),
                    joiner_name=str(member),
                    joined_at=datetime.now(timezone.utc),
                    auto_role_assigned=auto_role_assigned,
                    role_id=assigned_role_id,
                ))
                await session.commit()

        except discord.Forbidden:
            logger.error(f"No permission to fetch invites for {guild.name}")
        except Exception as e:
            logger.error(f"Error processing member join: {e}", exc_info=e)

    def _find_used_invite(self, guild: discord.Guild, cached_invites: Dict[str, discord.Invite],
                          current_invites) -> Optional[discord.Invite]:
        """The invite whose use count went up since the cached listing.

        Discord deletes an invite with a use limit on its last use, so that join
        shows no count going up. Then it's the one invite, gone since the cached
        listing (or deleted lately), that had exactly one use left. An invite
        whose time ran out is gone too, without a delete event, and wasn't used up.
        """
        for current_invite in current_invites:
            cached_invite = cached_invites.get(current_invite.code)

            if cached_invite and current_invite.uses > cached_invite.uses:
                logger.debug(f"Found used invite: {current_invite.code} (uses: {cached_invite.uses} -> {current_invite.uses})")
                return current_invite

        current_codes = {invite.code for invite in current_invites}
        deleted = self._recently_deleted(guild)
        gone = {code: invite for code, invite in cached_invites.items() if code not in current_codes}
        gone.update((code, invite) for code, (invite, _) in deleted.items())
        now = datetime.now(timezone.utc)
        used_up = [invite for invite in gone.values()
                   if invite.max_uses and invite.uses is not None and invite.uses + 1 == invite.max_uses
                   and not self._expired(invite, now)]
        if len(used_up) != 1:
            return None
        deleted.pop(used_up[0].code, None)
        logger.debug(f"Found used-up invite: {used_up[0].code} (uses: {used_up[0].uses} of {used_up[0].max_uses})")
        return used_up[0]

    @staticmethod
    def _expired(invite: discord.Invite, now: datetime) -> bool:
        """Whether the invite's time has run out (one from an invite event may
        carry only its age, not an expiry time)."""
        expires_at = invite.expires_at
        if expires_at is None and getattr(invite, "max_age", None) and invite.created_at:
            expires_at = invite.created_at + timedelta(seconds=invite.max_age)
        return expires_at is not None and expires_at <= now

    def _recently_deleted(self, guild: discord.Guild) -> Dict[str, Tuple[discord.Invite, float]]:
        """Invites deleted from this guild's cache within USED_UP_SECONDS."""
        deleted = self.deleted_invites.setdefault(str(guild.id), {})
        now = time.monotonic()
        for code, (_, at) in list(deleted.items()):
            if now - at > USED_UP_SECONDS:
                del deleted[code]
        return deleted

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        """Update cache when new invite is created"""
        if not is_home(self.services.config, invite.guild):
            return
        logger.info(f"Invite {invite.code} created in {invite.guild.name}")
        if str(invite.guild.id) not in self.invite_cache:
            await self._update_invite_cache(invite.guild)
            return
        # Only the new invite: listing them all again here could take in a use
        # whose join is still waiting to be matched, and lose it.
        async with self._lock(invite.guild):
            self.invite_cache.setdefault(str(invite.guild.id), {})[invite.code] = invite

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        """Update cache when invite is deleted"""
        if not is_home(self.services.config, invite.guild):
            return
        logger.info(f"Invite {invite.code} deleted from {invite.guild.name}")
        # Waits for a join being matched, which may still need the cached copy.
        async with self._lock(invite.guild):
            cached = self.invite_cache.get(str(invite.guild.id), {}).pop(invite.code, None)
            if cached is not None:
                # Its last use may belong to a join not handled yet.
                self._recently_deleted(invite.guild)[invite.code] = (cached, time.monotonic())

    # Admin Command - gated by require_admin below

    @app_commands.command(name="who-invited", description="Check who invited a specific user")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(member="The member to check")
    async def who_invited(self, interaction: discord.Interaction, member: discord.Member):
        """Check who invited a specific member - Admin only"""
        await interaction.response.defer(ephemeral=True)

        # The shared guard, not a local role-only test. This command is gated to
        # `administrator` at the Discord level, and the old _has_admin_role
        # accepted *only* ADMIN_ROLE_ID - returning False when that is unset - so a
        # server owner who had not configured the role could see the command and
        # then be refused it. is_bot_admin accepts guild administrator, the admin
        # role, or the bot owner, which is what the gate above promises.
        if not await require_admin(interaction):
            return

        try:
            async with get_session() as session:
                result = await session.execute(
                    select(InviteUse).where(
                        InviteUse.guild_id == str(interaction.guild.id),
                        InviteUse.joiner_id == str(member.id)
                    ).order_by(InviteUse.joined_at.desc()).limit(1)
                )
                invite_use = result.scalar_one_or_none()

            if not invite_use:
                embed = discord.Embed(
                    title="❌ No Invite Data",
                    description=f"No invite data found for {member.mention}. They may have joined before invite tracking was enabled.",
                    color=discord.Color.red()
                )
            else:
                embed = discord.Embed(
                    title="🔍 Invite Information",
                    description=f"Information about {member.mention}",
                    color=discord.Color.blue()
                )
                embed.add_field(name="Invited By", value=f"<@{invite_use.inviter_id}>", inline=True)
                embed.add_field(name="Invite Code", value=invite_use.invite_code, inline=True)
                embed.add_field(name="Joined At", value=f"<t:{int(invite_use.joined_at.timestamp())}:F>", inline=False)

                # Legacy: only rows from when a role was handed out on joining.
                if invite_use.auto_role_assigned and invite_use.role_id:
                    role = interaction.guild.get_role(int(invite_use.role_id))
                    role_name = role.name if role else f"Role ID: {invite_use.role_id}"
                    embed.add_field(name="Auto-Role Assigned", value=f"✅ {role_name}", inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error checking who invited: {e}")
            embed = create_error_embed("Failed to Check Invite", str(e))
            await interaction.followup.send(embed=embed, ephemeral=True)


