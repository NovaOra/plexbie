# path: plugins/watch_party/cog.py
"""Watch Party tracking - automatic credit for Discord Go Live Plex streams"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import select, update

from utils.formatting import episode_label
from utils.embeds import truncate_field
from core.discord_lookup import is_home
from core.logging import get_logger
from core.permissions import require_admin
from core.services import BotServices
from database.session import get_session
from plugins.user_mgmt.models import PlexUser
from .models import WatchParty, WatchPartyCredit, WatchPartyParticipant

logger = get_logger(__name__)


async def _credit_row(session, where) -> Optional[WatchPartyCredit]:
    """Someone's watch-party credit row, if they have one."""
    return (await session.execute(select(WatchPartyCredit).where(where))).scalar_one_or_none()

# Configuration
# CREDIT_INTERVAL now from config (watch_party_credit_interval)


@dataclass
class ActiveParticipant:
    """Track a participant in an active watch party"""
    discord_id: int
    plex_username: str
    joined_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_credited_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ActiveWatchParty:
    """In-memory tracking of an active watch party"""
    session_id: int  # Database session ID
    voice_channel_id: int
    streamer_discord_id: int
    streamer_plex_username: Optional[str]
    media_title: Optional[str]
    started_at: datetime
    participants: Dict[int, ActiveParticipant] = field(default_factory=dict)


class WatchPartyCog(commands.Cog):
    """Automatic watch party credit tracking"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        
        # Watch party channel ID from config
        self.watch_party_channel_id = services.config.watch_party_channel_id

        # In-memory tracking of active watch party (only one channel)
        self.active_party: Optional[ActiveWatchParty] = None

        # Cache of discord_id -> plex_username for linked users
        self._linked_users_cache: Dict[int, str] = {}

        # Start background tasks
        self.accumulate_credits.change_interval(seconds=services.config.watch_party_credit_interval)
        self.accumulate_credits.start()
        self.refresh_linked_users_cache.start()

    def cog_unload(self):
        """Cleanup on unload"""
        self.accumulate_credits.cancel()
        self.refresh_linked_users_cache.cancel()

    # ==================== Linked Users Cache ====================

    @tasks.loop(minutes=5)
    async def refresh_linked_users_cache(self):
        """Refresh the linked users cache periodically"""
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(PlexUser).where(
                        PlexUser.discord_id.isnot(None),
                        PlexUser.plex_username.isnot(None)
                    )
                )
                linked_users = result.scalars().all()

            self._linked_users_cache = {
                user.discord_id: user.plex_username
                for user in linked_users
            }
            logger.info(f"Refreshed linked users cache: {len(self._linked_users_cache)} users")
        except Exception as e:
            logger.error(f"Error refreshing linked users cache: {e}")

    @refresh_linked_users_cache.before_loop
    async def before_refresh_cache(self):
        await self.bot.wait_until_ready()

    def _get_plex_username(self, discord_id: int) -> Optional[str]:
        """Get Plex username for a Discord user ID (from cache)"""
        return self._linked_users_cache.get(discord_id)

    def _is_user_linked(self, discord_id: int) -> bool:
        """Check if a Discord user is linked to a Plex account"""
        return discord_id in self._linked_users_cache

    # ==================== Plex Session Verification ====================

    async def _get_streamer_plex_session(self, discord_id: int) -> Optional[dict]:
        """
        Check if the Discord user is currently streaming Plex content.
        Returns session info if found, None otherwise.
        """
        if not self.services.plex_server:
            return None

        plex_username = self._get_plex_username(discord_id)
        if not plex_username:
            return None

        try:
            # Shared snapshot - see services.plex_sessions. While a party is
            # running this loop and watch_tracking's both fire every 10 seconds.
            sessions = await self.services.plex_sessions()
            for session in sessions:
                session_user = session.usernames[0] if session.usernames else None
                if session_user and session_user.lower() == plex_username.lower():
                    player_state = 'unknown'
                    if session.players:
                        player_state = session.players[0].state
                    return {
                        'title': session.title,
                        'type': session.type,
                        'grandparentTitle': getattr(session, 'grandparentTitle', None),
                        'parentIndex': getattr(session, 'parentIndex', None),
                        'index': getattr(session, 'index', None),
                        'player_state': player_state,
                    }
        except Exception as e:
            logger.error(f"Error checking Plex sessions: {e}")

        return None

    def _format_media_title(self, session_info: dict) -> str:
        """Format media title from Plex session info.

        Note the dict is built with getattr(session, 'parentIndex', None), so the
        key exists with value None and .get(key, 0) returns None rather than the
        default - which then raised TypeError when formatted with :02d.
        """
        if session_info.get('type') == 'episode':
            return episode_label(
                session_info.get('grandparentTitle'),
                session_info.get('parentIndex'),
                session_info.get('index'),
                session_info.get('title'),
            )
        return session_info.get('title', 'Unknown Media')

    # ==================== Voice State Event Handler ====================

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):
        """Handle voice state changes for watch party tracking"""
        # Only the household's server: the streamer stopping a stream in another
        # server must not end the party here.
        if not is_home(self.services.config, member.guild):
            return
        logger.info(f"Voice state update: {member.name} - before_channel={before.channel}, after_channel={after.channel}, before_stream={before.self_stream}, after_stream={after.self_stream}")

        # Only care about the designated watch party channel
        before_in_party = before.channel and before.channel.id == self.watch_party_channel_id
        after_in_party = after.channel and after.channel.id == self.watch_party_channel_id

        # Case 1: User started streaming (Go Live) in watch party channel
        if after_in_party and not before.self_stream and after.self_stream:
            await self._handle_stream_start(member, after.channel)

        # Case 2: User stopped streaming
        elif before.self_stream and not after.self_stream:
            await self._handle_stream_stop(member)

        # Case 3: User joined watch party channel
        elif not before_in_party and after_in_party:
            await self._handle_user_joined(member, after.channel)

        # Case 4: User left watch party channel
        elif before_in_party and not after_in_party:
            await self._handle_user_left(member)

        # Case 5: Streamer disconnected completely
        elif before_in_party and after.channel is None:
            if self.active_party and member.id == self.active_party.streamer_discord_id:
                await self._handle_stream_stop(member)

    async def _handle_stream_start(self, member: discord.Member, channel: discord.VoiceChannel):
        """Handle when a user starts streaming in watch party channel"""
        # Ignore if already have an active party
        if self.active_party:
            logger.debug(f"Watch party already active, ignoring stream from {member.name}")
            return

        # Check if streamer is linked to Plex
        if not self._is_user_linked(member.id):
            logger.info(f"Streamer {member.name} not linked to Plex, ignoring")
            return

        # Check if they're actually streaming Plex
        plex_session = await self._get_streamer_plex_session(member.id)
        if not plex_session:
            logger.info(f"Streamer {member.name} not watching Plex, ignoring")
            return

        plex_username = self._get_plex_username(member.id)
        media_title = self._format_media_title(plex_session)

        logger.info(f"Watch party started: {member.name} streaming '{media_title}' in {channel.name}")

        # Create database session record
        try:
            async with get_session() as session:
                db_session = WatchParty(
                    voice_channel_id=channel.id,
                    voice_channel_name=channel.name,
                    streamer_discord_id=member.id,
                    streamer_plex_username=plex_username,
                    media_title=media_title,
                    media_type=plex_session.get('type'),
                    is_active=True
                )
                session.add(db_session)
                await session.flush()
                session_id = db_session.id
                await session.commit()
        except Exception as e:
            logger.error(f"Error creating watch party session: {e}")
            return

        # Create in-memory tracking
        self.active_party = ActiveWatchParty(
            session_id=session_id,
            voice_channel_id=channel.id,
            streamer_discord_id=member.id,
            streamer_plex_username=plex_username,
            media_title=media_title,
            started_at=datetime.now(timezone.utc),
        )

        # Add all current linked users in the channel as participants
        for vc_member in channel.members:
            if vc_member.id == member.id:
                continue  # Don't credit the streamer
            if self._is_user_linked(vc_member.id):
                participant = ActiveParticipant(
                    discord_id=vc_member.id,
                    plex_username=self._get_plex_username(vc_member.id)
                )
                self.active_party.participants[vc_member.id] = participant
                logger.info(f"Added participant {vc_member.name} to watch party")

    async def _handle_stream_stop(self, member: discord.Member):
        """Handle when a user stops streaming"""
        if not self.active_party or self.active_party.streamer_discord_id != member.id:
            return

        logger.info(f"Watch party ended: {member.name} stopped streaming")

        # Finalize credits for all participants
        await self._finalize_watch_party()

    async def _handle_user_joined(self, member: discord.Member, channel: discord.VoiceChannel):
        """Handle when a user joins the watch party channel"""
        if not self.active_party:
            return

        # Don't add the streamer as a participant
        if member.id == self.active_party.streamer_discord_id:
            return

        # Only add linked users
        if not self._is_user_linked(member.id):
            logger.debug(f"User {member.name} joined but not linked to Plex")
            return

        # Add as participant if not already tracking
        if member.id not in self.active_party.participants:
            participant = ActiveParticipant(
                discord_id=member.id,
                plex_username=self._get_plex_username(member.id)
            )
            self.active_party.participants[member.id] = participant
            logger.info(f"User {member.name} joined watch party")

    async def _handle_user_left(self, member: discord.Member):
        """Handle when a user leaves the watch party channel"""
        if not self.active_party:
            return

        # If streamer left, end the party
        if member.id == self.active_party.streamer_discord_id:
            logger.info(f"Streamer {member.name} left channel, ending watch party")
            await self._finalize_watch_party()
            return

        # Remove participant and finalize their credit
        participant = self.active_party.participants.pop(member.id, None)
        if participant:
            await self._credit_participant(self.active_party.session_id, participant)
            logger.info(f"User {member.name} left watch party")

    # ==================== Credit Accumulation ====================

    @tasks.loop(seconds=10)  # Default, changed in __init__ from config
    async def accumulate_credits(self):
        """Periodically accumulate credits for active watch party participants"""
        if not self.active_party:
            return

        now = datetime.now(timezone.utc)

        # Verify streamer is still streaming Plex
        plex_session = await self._get_streamer_plex_session(self.active_party.streamer_discord_id)
        if not plex_session:
            # Streamer stopped Plex, end the party
            logger.info("Streamer stopped Plex, ending watch party")
            await self._finalize_watch_party()
            return

        # Skip crediting if paused
        if plex_session.get("player_state") == "paused":
            logger.debug("Streamer paused, skipping credit accumulation")
            return

        # Credit each participant for elapsed time
        for discord_id, participant in self.active_party.participants.items():
            elapsed = (now - participant.last_credited_at).total_seconds()
            if elapsed >= self.services.config.watch_party_credit_interval:
                await self._increment_credit(
                    discord_id=participant.discord_id,
                    plex_username=participant.plex_username,
                    seconds=int(elapsed)
                )
                participant.last_credited_at = now

    @accumulate_credits.before_loop
    async def before_accumulate(self):
        await self.bot.wait_until_ready()
        # Wait for cache to be populated
        await asyncio.sleep(10)

    async def _increment_credit(self, discord_id: int, plex_username: str, seconds: int):
        """Increment watch party credit for a user"""
        try:
            async with get_session() as session:
                # Check if record exists
                existing = await _credit_row(session, WatchPartyCredit.plex_username == plex_username)

                if existing:
                    # Update existing record
                    existing.total_duration += seconds
                    existing.last_credited_at = datetime.now(timezone.utc)
                else:
                    # Create new record
                    new_credit = WatchPartyCredit(
                        discord_id=discord_id,
                        plex_username=plex_username,
                        total_duration=seconds,
                        total_sessions=0,
                        last_credited_at=datetime.now(timezone.utc)
                    )
                    session.add(new_credit)

                await session.commit()
                logger.debug(f"Credited {seconds}s to {plex_username}")
        except Exception as e:
            logger.error(f"Error incrementing credit for {plex_username}: {e}")

    async def _credit_participant(self, session_id: int, participant: ActiveParticipant):
        """Finalize credit for a participant leaving a watch party"""
        now = datetime.now(timezone.utc)
        total_seconds = int((now - participant.joined_at).total_seconds())

        try:
            async with get_session() as session:
                # Record participation
                participation = WatchPartyParticipant(
                    session_id=session_id,
                    discord_id=participant.discord_id,
                    plex_username=participant.plex_username,
                    joined_at=participant.joined_at,
                    left_at=now,
                    duration_credited=total_seconds
                )
                session.add(participation)

                # Increment session count for the user
                credit = await _credit_row(session, WatchPartyCredit.plex_username == participant.plex_username)
                if credit:
                    credit.total_sessions += 1

                await session.commit()
        except Exception as e:
            logger.error(f"Error recording participation: {e}")

    async def _finalize_watch_party(self):
        """Finalize a watch party session"""
        if not self.active_party:
            return

        # Credit all remaining participants
        for participant in list(self.active_party.participants.values()):
            await self._credit_participant(self.active_party.session_id, participant)

        # Mark session as ended
        try:
            async with get_session() as session:
                await session.execute(
                    update(WatchParty)
                    .where(WatchParty.id == self.active_party.session_id)
                    .values(
                        is_active=False,
                        ended_at=datetime.now(timezone.utc)
                    )
                )
                await session.commit()
        except Exception as e:
            logger.error(f"Error finalizing watch party session: {e}")

        # Clear active party
        self.active_party = None

    # ==================== Slash Commands ====================

    @app_commands.command(name="watchparty-stats", description="View your watch party statistics")
    @app_commands.guild_only()
    async def watchparty_stats(self, interaction: discord.Interaction):
        """Show user's watch party statistics"""
        await interaction.response.defer(ephemeral=True)

        try:
            async with get_session() as session:
                credit = await _credit_row(session, WatchPartyCredit.discord_id == interaction.user.id)

            if not credit:
                await interaction.followup.send(
                    "You haven't earned any watch party credits yet!\n"
                    "Join the Watch Party voice channel when someone is streaming Plex to start earning.",
                    ephemeral=True
                )
                return

            hours = credit.total_duration // 3600
            minutes = (credit.total_duration % 3600) // 60

            embed = discord.Embed(
                title="🎬 Your Watch Party Stats",
                color=discord.Color.purple()
            )
            embed.add_field(
                name="Total Watch Time",
                value=f"{hours}h {minutes}m",
                inline=True
            )
            embed.add_field(
                name="Sessions Attended",
                value=str(credit.total_sessions),
                inline=True
            )
            if credit.last_credited_at:
                embed.add_field(
                    name="Last Activity",
                    value=f"<t:{int(credit.last_credited_at.timestamp())}:R>",
                    inline=True
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error showing watchparty stats: {e}")
            await interaction.followup.send("Error retrieving stats.", ephemeral=True)

    @app_commands.command(name="watchparty-active", description="Show active watch party")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def watchparty_active(self, interaction: discord.Interaction):
        """Admin command to view active watch party"""
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        if not self.active_party:
            await interaction.followup.send("No active watch party.", ephemeral=True)
            return

        channel = self.bot.get_channel(self.active_party.voice_channel_id)
        channel_name = channel.name if channel else "Unknown"

        participant_names = []
        for p in self.active_party.participants.values():
            member = interaction.guild.get_member(p.discord_id)
            name = member.display_name if member else p.plex_username
            participant_names.append(name)

        # Get streamer name
        streamer = interaction.guild.get_member(self.active_party.streamer_discord_id)
        streamer_name = streamer.display_name if streamer else "Unknown"

        embed = discord.Embed(
            title="🎬 Active Watch Party",
            color=discord.Color.green()
        )
        embed.add_field(
            name="Channel",
            value=f"#{channel_name}",
            inline=True
        )
        embed.add_field(
            name="Streamer",
            value=streamer_name,
            inline=True
        )
        embed.add_field(
            name="Media",
            value=self.active_party.media_title or "Unknown",
            inline=False
        )
        embed.add_field(
            name="Started",
            value=f"<t:{int(self.active_party.started_at.timestamp())}:R>",
            inline=True
        )
        embed.add_field(
            name=f"Participants ({len(participant_names)})",
            value=truncate_field(", ".join(participant_names) if participant_names else "None"),
            inline=False
        )

        await interaction.followup.send(embed=embed, ephemeral=True)


