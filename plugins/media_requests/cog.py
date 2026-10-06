# path: plugins/media_requests/cog.py
"""Media request system matching original JS bot workflow"""
import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from core.blocking import run_blocking
from core.clients import ServiceError
from core.logging import get_logger
from core.permissions import AdminActionView, single_flight
from core.services import BotServices
from core import notify
from core.admin_mirror import dm_user_id
from utils.embeds import truncate_field
from utils.views import RequesterOnlyView, reply_failure
from core.discord_lookup import admin_channel
from database.request_store import (
    SEERR_SOURCES,
    STATUS_APPROVED, STATUS_DECLINED, get_request, mark_resolved, pending_requests, save_request, set_fields,
)

logger = get_logger(__name__)

#: Discord allows 25 embed fields; keep well under it so the footer survives.
MAX_LISTED_REQUESTS = 15



# Language names that may indicate a non-English release. Matched as whole
# tokens only - two-letter codes like ".it." were removed because they collide
# with ordinary English words ("Make.It.Stick").
LANGUAGE_TAGS = frozenset({
    "german", "deutsch", "french", "francais", "spanish", "espanol",
    "italian", "italiano", "portuguese", "dutch", "nederlands", "swedish",
    "norwegian", "danish", "finnish", "hungarian", "polish", "czech",
    "russian", "chinese", "japanese", "korean", "arabic", "turkish",
})


def result_title(item) -> str:
    """Read a newznab <item>'s title, tolerating a malformed entry.

    ElementTree find() returns None when the child is absent, so the original
    `item.find("title").text` raised AttributeError - which unwound the entire
    submission and discarded every other result because one entry was malformed.
    """
    element = item.find("title")
    if element is None or not element.text:
        return ""
    return element.text.strip()


def looks_foreign_language(release_title: str) -> bool:
    """Whether a release name carries a non-English language tag.

    Whole-token matching, so "Make.It.Stick" is not flagged by a stray "it".

    This is deliberately only a RANKING signal, never a filter. A language word
    in a release name is ambiguous - "The.Dutch.House" and "Der.Schwarm.German"
    are indistinguishable by title alone - and the previous code dropped any
    match outright, so legitimate English books (The Dutch House, The German
    Wife, Russian Roulette, even "Norwegian.Wood...English.EPUB") were discarded
    and the requester was told no results existed. Penalising instead means a
    false positive costs a place in the ordering, not the whole request.
    """
    tokens = set(re.split(r"[^a-z]+", release_title.lower()))
    return bool(tokens & LANGUAGE_TAGS)



#: What admins are told about a show with no TheTVDB entry.
NO_TVDB_NOTE = ("⚠️ This show isn't on TheTVDB, so Sonarr can't take it and Seerr would drop the request. "
                "It wasn't sent to Seerr: it needs a hand download, then Plexbie sees it on Plex.")


async def no_tvdb_entry(services, tmdb_id) -> bool:
    """Whether a show (by TMDB id) has no TheTVDB ID, as Seerr sees it. False when Seerr
    can't say (not set up, unreachable): then the request goes through as usual."""
    if not tmdb_id or not services.seerr.configured:
        return False
    try:
        tv = await services.seerr.get(f"tv/{int(tmdb_id)}")
    except Exception as e:
        logger.info(f"Couldn't check TheTVDB for TMDB {tmdb_id}: {e}")
        return False
    return isinstance(tv, dict) and bool(tv.get("name")) and not ((tv.get("externalIds") or {}).get("tvdbId"))

class AdminChannelUnavailable(RuntimeError):
    """The admin channel is unset or the bot cannot see it, so nothing was posted."""


def _admin_channel(bot, services):
    if not services.config.admin_channel_id:
        raise AdminChannelUnavailable("Admin channel not configured")
    channel = admin_channel(bot, services.config)
    if channel is None:
        raise AdminChannelUnavailable(f"Admin channel {services.config.admin_channel_id} not found")
    return channel


async def post_media_request(bot, services, user, media: dict, seasons=None, monitor: bool = False, extra: Optional[Dict[str, Any]] = None) -> int:
    """Post the Approve/Decline card for a TV or film request and record it.

    Shared by /request and the website, so the admin sees one kind of request
    whichever way it arrived. Returns the approval message id, which is the
    request's key in the store. Raises AdminChannelUnavailable when it cannot post.
    """
    admin_channel = _admin_channel(bot, services)

    title = media.get('title') or media.get('name', 'Unknown')
    media_type = media.get('media_type', 'unknown')

    embed = discord.Embed(
        title="📥 New Media Request",
        description=f"**{title}**",
        color=discord.Color.gold()
    )

    embed.add_field(name="Requested by", value=f"{user.mention}", inline=True)
    embed.add_field(name="Type", value=media_type.title(), inline=True)
    embed.add_field(name="TMDB ID", value=media.get('id', 'N/A'), inline=True)

    # For TV shows, add season information
    if media_type == 'tv' and seasons:
        if seasons == "all":
            season_text = "All Seasons"
        else:
            season_text = ", ".join([f"S{s}" for s in sorted(seasons)])

        if monitor:
            season_text += " 🔔 (Monitor enabled)"

        embed.add_field(name="Seasons", value=season_text, inline=False)

    if media.get('overview'):
        embed.add_field(
            name="Overview",
            value=media['overview'][:200] + "..." if len(media['overview']) > 200 else media['overview'],
            inline=False
        )

    if media.get('poster_path'):
        embed.set_thumbnail(url=f"https://image.tmdb.org/t/p/w500{media['poster_path']}")

    embed.timestamp = datetime.now(timezone.utc)

    view = AdminApprovalView(media, user.id, services, seasons, monitor)
    message = await admin_channel.send(embed=embed, view=view)
    await save_request(message.id, user_id=user.id, media=media, seasons=seasons, monitor=monitor, extra=extra)
    what = f"{title} ({media_type.title() if media_type != 'tv' else 'TV'}"
    what += f", {season_text.split(' 🔔')[0]})" if media_type == 'tv' and seasons else ")"
    notify.alert_admins_soon(bot, services.config, title=f"{_who(user)} asked for {title}", body=f"{what}. Approve or decline it.",
                             url="/manage?tab=requests", tag=f"request-{message.id}")
    return message.id


async def post_book_request(bot, services, user, book: dict, extra: Optional[Dict[str, Any]] = None) -> int:
    """Post the Approve/Decline card for a book request and record it.

    Shared by /request and the website. Returns the approval message id.
    Raises AdminChannelUnavailable when it cannot post.
    """
    admin_channel = _admin_channel(bot, services)

    title = book.get('title', 'Unknown')
    author = book.get('author', 'Unknown Author')
    format_type = book.get('request_format', 'ebook')
    format_labels = {"ebook": "📖 Ebook", "audiobook": "🎧 Audiobook", "both": "📖+🎧 Both"}

    embed = discord.Embed(
        title="📥 New Book Request",
        description=f"**{title}**",
        color=discord.Color.gold()
    )

    embed.add_field(name="Requested by", value=f"{user.mention}", inline=True)
    embed.add_field(name="Format", value=format_labels.get(format_type, format_type), inline=True)
    embed.add_field(name="Author", value=author, inline=True)

    if book.get('year'):
        embed.add_field(name="Year", value=str(book['year']), inline=True)
    if book.get('isbn'):
        embed.add_field(name="ISBN", value=book['isbn'], inline=True)

    if book.get('description'):
        desc = book['description']
        embed.add_field(
            name="Description",
            value=desc[:200] + "..." if len(desc) > 200 else desc,
            inline=False
        )

    if book.get('cover_url'):
        embed.set_thumbnail(url=book['cover_url'])

    embed.timestamp = datetime.now(timezone.utc)

    view = BookAdminApprovalView(book, user.id, services)
    message = await admin_channel.send(embed=embed, view=view)

    # One keyed write, not a rewrite of every request ever made.
    await save_request(message.id, user_id=user.id, media=book, media_type=format_type, extra=extra)
    kind = {"ebook": "ebook", "audiobook": "audiobook", "both": "ebook and audiobook"}.get(format_type, format_type)
    notify.alert_admins_soon(bot, services.config, title=f"{_who(user)} asked for {title}", body=f"{title} by {author} ({kind}). Approve or decline it.",
                             url="/manage?tab=requests", tag=f"request-{message.id}")
    return message.id


def _who(user) -> str:
    return getattr(user, "display_name", None) or getattr(user, "name", None) or "Someone"


_DECISION_LOCKS: Dict[int, asyncio.Lock] = {}


async def _close_admin_card(bot, services, message_id: int, *, approved: bool, by: str, note: str = "") -> None:
    """Mark a request's admin card decided, the way the Discord buttons do."""
    try:
        message = await _admin_channel(bot, services).fetch_message(int(message_id))
    except Exception as e:
        logger.warning(f"Could not update admin card {message_id}: {e}")
        return
    embed = message.embeds[0] if message.embeds else discord.Embed(description="Request")
    await message.edit(embed=_stamp_decision(embed, approved=approved, by=by, note=note, where=" (on the website)"), view=None)


def _stamp_decision(embed: discord.Embed, *, approved: bool, by: str, note: str = "", where: str = "") -> discord.Embed:
    """Colour the admin card and add who decided it, when, and anything to know."""
    embed.color = discord.Color.green() if approved else discord.Color.red()
    stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')
    embed.add_field(name="✅ Approved by" if approved else "❌ Declined by",
                    value=f"{by} at {stamp}{where}\n{note}".strip(), inline=False)
    return embed


async def _after_decision(bot, services, message_id, *, approved: bool, actor: str, user_id, title: str,
                          what: str, user_message: str = "") -> None:
    """After a decision, wherever it was made: record it, then tell the requester
    (a DM, or a phone alert or email for someone without Discord)."""
    await mark_resolved(message_id, STATUS_APPROVED if approved else STATUS_DECLINED, actor)
    await _notify_web_requester(services, message_id, approved, user_message)
    dm = user_message if approved else f"❌ Sorry, your {'book ' if what == 'book' else ''}request for **{title}** was declined."
    # No user_id: signed in with Plex only, and told above instead.
    view = None
    if approved and user_id:
        from portal.ticket_view import open_view
        view = open_view(str(message_id))      # "Something wrong? Open a ticket", until it's on Plex
    sent = await dm_user_id(bot, services, user_id, context=f"{what} request {'approved' if approved else 'declined'} for {title}",
                            content=dm, view=view)
    if view is not None and getattr(sent, "id", None):
        try:
            await set_fields(int(message_id), approval_dm={"channel": str(sent.channel.id), "message": str(sent.id), "text": dm})
        except Exception as e:
            logger.info(f"Couldn't remember the approval DM for {title}: {e}")


async def _notify_web_requester(services, message_id, approved: bool, detail: str = "") -> None:
    """A request made on the website by someone without Discord: tell them by phone alert or email.

    Does nothing for Discord requesters (they get the DM), so it can follow
    every decision, from the website or the Discord buttons alike.
    """
    try:
        record = await get_request(int(message_id))
        if not record or record.get("user_id"):
            return
        pid, name = record.get("plex_account_id"), record.get("requester_name")
        if not (pid or name):
            return
        from core.notify import notify_member, plain
        media = record.get("media") or {}
        title = media.get("title") or media.get("name") or "your request"
        body = (plain(detail) if detail else f"{title} is on its way. You'll hear again when it's ready.") if approved \
            else f"Sorry, {title} wasn't added this time."
        await notify_member(services, title=f"Request {'approved' if approved else 'declined'}: {title}", body=body,
                            url="/app/schedule", plex_account_id=pid, plex_name=name,
                            context=f"request {'approved' if approved else 'declined'} for {title}")
    except Exception as e:
        logger.warning(f"Could not tell a website requester about {message_id}: {e}")


async def decide_request(bot, services, message_id: int, approve: bool, actor: str) -> Dict[str, Any]:
    """Approve or decline a request from the website, exactly as the Discord buttons do.

    Refuses anything already decided, so a click in Discord and a click on the
    website can never both act. Returns {"ok": bool, "message": str}.
    """
    lock = _DECISION_LOCKS.setdefault(int(message_id), asyncio.Lock())
    async with lock:
        record = await get_request(message_id)
        if record is None:
            return {"ok": False, "message": "That request no longer exists."}
        if record.get("status", "pending") != "pending":
            return {"ok": False, "message": f"Already {record.get('status')}."}
        if record.get("source") in SEERR_SOURCES:
            # Made in Seerr: decide it there, then record it here.
            from webhooks.seerr_handler import _apply_decision as apply_decision   # this lock is held already
            try:
                await services.seerr.post(f"request/{record['overseerr_request_id']}/{'approve' if approve else 'decline'}", {})
            except ServiceError as e:
                return {"ok": False, "message": f"Seerr didn't take that: {e}"}
            await apply_decision(bot, int(message_id), approve, actor)
            title = (record.get("media") or {}).get("title") or (record.get("media") or {}).get("name") or "it"
            return {"ok": True, "message": f"{'Approved' if approve else 'Declined'} {title} in Seerr."}
        media = record.get("media") or {}
        is_book = record.get("media_type") in ("ebook", "audiobook", "both") or "open_library_key" in media

        if is_book:
            view = BookAdminApprovalView()
            if not await view._load_from_saved(message_id, bot):
                return {"ok": False, "message": "Could not load that request."}
            title = view.book.get('title', 'Unknown')
            if approve:
                result = await view._approve_core()
                await _close_admin_card(bot, services, message_id, approved=True, by=actor, note=result["note"])
                await _after_decision(bot, services, message_id, approved=True, actor=actor, user_id=view.user_id,
                                      title=title, what="book", user_message=result.get("user_message", ""))
                return {"ok": True, "message": result["followup_message"]}
            await _close_admin_card(bot, services, message_id, approved=False, by=actor)
            await _after_decision(bot, services, message_id, approved=False, actor=actor, user_id=view.user_id, title=title, what="book")
            return {"ok": True, "message": f"Declined {title}."}

        view = AdminApprovalView()
        if not await view._load_from_saved(message_id, bot):
            return {"ok": False, "message": "Could not load that request."}
        title = view._title()
        if approve:
            result = await view._fulfill_request()
            if not result.get("success"):
                return {"ok": False, "message": result.get("followup_message", "Approval failed.")}
            await _close_admin_card(bot, services, message_id, approved=True, by=actor, note=result.get("admin_note", ""))
            await _after_decision(bot, services, message_id, approved=True, actor=actor, user_id=view.user_id,
                                  title=title, what="media", user_message=result.get("user_message", ""))
            await view._register_with_tracking()
            return {"ok": True, "message": result["followup_message"]}
        await _close_admin_card(bot, services, message_id, approved=False, by=actor)
        await _after_decision(bot, services, message_id, approved=False, actor=actor, user_id=view.user_id, title=title, what="media")
        return {"ok": True, "message": f"Declined {title}."}


class MediaTypeSelectView(RequesterOnlyView):
    """Initial view with buttons to choose between TV/Movie and Audiobook/Ebook"""
    def __init__(self, cog: 'MediaRequestsCog', user_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id

    @discord.ui.button(label="TV & Movie", style=discord.ButtonStyle.primary, emoji="🎬")
    async def tv_movie(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = TVMovieRequestModal(cog=self.cog)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Audiobook & Ebook", style=discord.ButtonStyle.primary, emoji="📚")
    async def audiobook_ebook(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = BookRequestModal(cog=self.cog)
        await interaction.response.send_modal(modal)


class TVMovieRequestModal(discord.ui.Modal, title="Request TV Show or Movie"):
    """Modal for collecting TV/Movie search query"""
    search_query = discord.ui.TextInput(
        label="What would you like to request?",
        placeholder="Movie or TV show name",
        style=discord.TextStyle.short,
        required=True
    )

    def __init__(self, cog: 'MediaRequestsCog'):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        query = self.search_query.value.strip()
        await interaction.response.send_message("🔍 Searching...", ephemeral=True)

        try:
            results = await self.cog._search_tmdb(query)

            if not results:
                await interaction.edit_original_response(
                    content="No results found. Please try a different search term or use `/request` again."
                )
                return

            view = MediaSelectView(results, interaction.user.id, self.cog.services)
            await interaction.edit_original_response(
                content="**I found these titles:**",
                view=view
            )
        except Exception as e:
            logger.error(f"Search error after modal submit: {e}")
            await interaction.edit_original_response(
                content="An error occurred. Please try again later."
            )


class MediaSelectView(RequesterOnlyView):
    """Dropdown for search results"""
    def __init__(self, results: List[dict], user_id: int, services: BotServices):
        super().__init__(timeout=300)
        self.results = results
        self.user_id = user_id
        self.services = services
        self.selected_media = None
        
        # Build dropdown options
        options = []
        for i, result in enumerate(results[:10]):  # Max 10 results
            title = result.get('title') or result.get('name', 'Unknown')
            year = self._extract_year(result)
            media_type = result.get('media_type', 'unknown')
            
            label = f"{title}"
            if year:
                label += f" ({year})"
            
            description = f"{'🎬 Movie' if media_type == 'movie' else '📺 TV Show'}"
            
            options.append(discord.SelectOption(
                label=label[:100],  # Discord limit
                description=description[:100],
                value=str(i)
            ))
        
        self.select_menu = discord.ui.Select(
            placeholder="Choose a title",
            options=options
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)
    
    def _extract_year(self, result: dict) -> Optional[str]:
        """Extract year from release date"""
        date_field = result.get('release_date') or result.get('first_air_date')
        if date_field:
            return date_field[:4]
        return None
    
    async def select_callback(self, interaction: discord.Interaction):
        """Handle selection"""
        index = int(self.select_menu.values[0])
        self.selected_media = self.results[index]

        # If TV show, fetch season info and show season selection
        if self.selected_media.get('media_type') == 'tv':
            await interaction.response.edit_message(
                content="📺 Fetching season information...",
                view=None
            )

            # Fetch TV show details from TMDB
            tv_details = await self._fetch_tv_details(self.selected_media.get('id'))

            if tv_details:
                self.selected_media.update(tv_details)
                embed = self._create_media_embed(self.selected_media)
                view = SeasonSelectionView(self.selected_media, self.user_id, self.services)

                await interaction.edit_original_response(
                    content="**Select which seasons you want:**",
                    embed=embed,
                    view=view
                )
            else:
                await interaction.edit_original_response(
                    content="❌ Failed to fetch season info. Please try again.",
                    view=None
                )
        else:
            # Movie - show confirmation directly
            embed = self._create_media_embed(self.selected_media)
            view = ConfirmationView(self.selected_media, self.user_id, self.services, None)

            await interaction.response.edit_message(
                content="**Confirm your request:**",
                embed=embed,
                view=view
            )

    async def _fetch_tv_details(self, tv_id: int) -> Optional[dict]:
        """Fetch TV show details including seasons from TMDB"""
        if not self.services.config.tmdb_api_key:
            return None

        try:
            return await self.services.tmdb.get(f"tv/{tv_id}")
        except Exception as e:
            logger.error(f"Failed to fetch TV details: {e}")

        return None
    
    def _create_media_embed(self, media: dict) -> discord.Embed:
        """Create rich embed for media"""
        title = media.get('title') or media.get('name', 'Unknown')
        media_type = media.get('media_type', 'unknown')
        overview = media.get('overview', 'No description available.')

        embed = discord.Embed(
            title=title,
            description=overview[:500] if len(overview) > 500 else overview,
            color=discord.Color.blue()
        )

        # Add fields
        embed.add_field(
            name="Type",
            value="🎬 Movie" if media_type == "movie" else "📺 TV Show",
            inline=True
        )

        year = self._extract_year(media)
        if year:
            embed.add_field(name="Year", value=year, inline=True)

        if media.get('vote_average'):
            embed.add_field(
                name="Rating",
                value=f"⭐ {media['vote_average']:.1f}/10",
                inline=True
            )

        # For TV shows, add season information
        if media_type == 'tv' and media.get('seasons'):
            # Filter out Season 0 (specials) for display
            regular_seasons = [s for s in media['seasons'] if s.get('season_number', 0) > 0]
            if regular_seasons:
                latest_season = max(regular_seasons, key=lambda s: s.get('season_number', 0))
                embed.add_field(
                    name="Seasons",
                    value=f"{len(regular_seasons)} season{'s' if len(regular_seasons) != 1 else ''}",
                    inline=True
                )
                embed.add_field(
                    name="Latest Season",
                    value=f"Season {latest_season.get('season_number')} ({latest_season.get('episode_count', '?')} episodes)",
                    inline=True
                )

                # Show status
                if media.get('status'):
                    status_emoji = "📺" if media['status'] in ['Returning Series', 'In Production'] else "✅"
                    embed.add_field(
                        name="Status",
                        value=f"{status_emoji} {media['status']}",
                        inline=True
                    )

        # Add poster
        if media.get('poster_path'):
            poster_url = f"https://image.tmdb.org/t/p/w500{media['poster_path']}"
            embed.set_thumbnail(url=poster_url)

        return embed


class SeasonSelectionView(RequesterOnlyView):
    """Season selection for TV shows"""
    def __init__(self, media: dict, user_id: int, services: BotServices):
        super().__init__(timeout=300)
        self.media = media
        self.user_id = user_id
        self.services = services
        self.selected_seasons = None

        # Get regular seasons (exclude Season 0/specials)
        regular_seasons = [s for s in media.get('seasons', []) if s.get('season_number', 0) > 0]

        if regular_seasons:
            # Build dropdown options for each season
            options = []
            for season in regular_seasons:
                season_num = season.get('season_number')
                episode_count = season.get('episode_count', '?')
                options.append(discord.SelectOption(
                    label=f"Season {season_num}",
                    description=f"{episode_count} episodes",
                    value=str(season_num)
                ))

            # Add "All Seasons" option
            options.insert(0, discord.SelectOption(
                label="All Seasons",
                description=f"All {len(regular_seasons)} seasons",
                value="all",
                emoji="📺"
            ))

            self.season_select = discord.ui.Select(
                placeholder="Choose season(s)...",
                options=options,
                min_values=1,
                max_values=len(options)
            )
            self.season_select.callback = self.season_select_callback
            self.add_item(self.season_select)

        # Add "Latest Season + Monitor" button for ongoing shows
        latest_season = max(regular_seasons, key=lambda s: s.get('season_number', 0))
        self.latest_season_num = latest_season.get('season_number')

        # Check if show is ongoing
        is_ongoing = media.get('status') in ['Returning Series', 'In Production']

        if is_ongoing:
            monitor_button = discord.ui.Button(
                label=f"Latest (S{self.latest_season_num}) + Monitor",
                style=discord.ButtonStyle.success,
                emoji="🔔",
                row=1
            )
            monitor_button.callback = self.latest_with_monitor_callback
            self.add_item(monitor_button)

        # Add submit button (initially disabled until selection is made)
        self.submit_button = discord.ui.Button(
            label="Submit Request",
            style=discord.ButtonStyle.primary,
            emoji="✅",
            disabled=True,
            row=1 if not is_ongoing else 2
        )
        self.submit_button.callback = self.submit_callback
        self.add_item(self.submit_button)

    async def season_select_callback(self, interaction: discord.Interaction):
        """Handle season selection from dropdown"""
        selected_values = self.season_select.values

        # Check if "all" is selected
        if "all" in selected_values:
            self.selected_seasons = "all"
            seasons_text = "All Seasons"
        else:
            self.selected_seasons = [int(v) for v in selected_values]
            seasons_text = ", ".join([f"S{s}" for s in sorted(self.selected_seasons)])

        # Enable submit button now that user has made a selection
        self.submit_button.disabled = False

        # Update message to show selection
        await interaction.response.edit_message(
            content=f"**Selected:** {seasons_text}\n\nClick **Submit Request** to continue, or select different seasons.",
            view=self
        )

    async def submit_callback(self, interaction: discord.Interaction):
        """Handle submit button after season selection"""
        if not self.selected_seasons:
            await interaction.response.send_message("Please select at least one season first!", ephemeral=True)
            return

        # Build season text for confirmation
        if self.selected_seasons == "all":
            seasons_text = "All Seasons"
        else:
            seasons_text = ", ".join([f"Season {s}" for s in sorted(self.selected_seasons)])

        # Show confirmation
        embed = discord.Embed(
            title=f"📺 {self.media.get('name', 'Unknown')}",
            description=f"**Requesting:** {seasons_text}",
            color=discord.Color.blue()
        )

        view = ConfirmationView(self.media, self.user_id, self.services, self.selected_seasons)

        await interaction.response.edit_message(
            content="**Confirm your request:**",
            embed=embed,
            view=view
        )

    async def latest_with_monitor_callback(self, interaction: discord.Interaction):
        """Handle latest season + monitor selection"""
        self.selected_seasons = [self.latest_season_num]
        monitor = True

        # Show confirmation
        embed = discord.Embed(
            title=f"📺 {self.media.get('name', 'Unknown')}",
            description=f"**Selected:** Season {self.latest_season_num} + Monitor for new episodes 🔔",
            color=discord.Color.green()
        )

        view = ConfirmationView(self.media, self.user_id, self.services, self.selected_seasons, monitor=monitor)

        await interaction.response.edit_message(
            content="**Confirm your request:**",
            embed=embed,
            view=view
        )


class BookRequestModal(discord.ui.Modal, title="Request Audiobook or Ebook"):
    """Modal for collecting book search query"""
    search_query = discord.ui.TextInput(
        label="What audiobook or ebook are you looking for?",
        placeholder="Book title or author",
        style=discord.TextStyle.short,
        required=True
    )

    def __init__(self, cog: 'MediaRequestsCog'):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        query = self.search_query.value.strip()
        await interaction.response.send_message("🔍 Searching...", ephemeral=True)

        try:
            results = await self.cog._search_open_library(query)

            if not results:
                await interaction.edit_original_response(
                    content="No results found. Please try a different search term or use `/request` again."
                )
                return

            view = BookSelectView(results, interaction.user.id, self.cog.services)
            await interaction.edit_original_response(
                content="**I found these titles:**",
                view=view
            )
        except Exception as e:
            logger.error(f"Book search error after modal submit: {e}")
            await interaction.edit_original_response(
                content="An error occurred. Please try again later."
            )


class BookSelectView(RequesterOnlyView):
    """Dropdown for book search results"""
    def __init__(self, results: List[dict], user_id: int, services: BotServices):
        super().__init__(timeout=300)
        self.results = results
        self.user_id = user_id
        self.services = services

        options = []
        for i, result in enumerate(results[:10]):
            title = result.get('title', 'Unknown')
            author = result.get('author', 'Unknown Author')
            year = result.get('year')

            label = title
            if year:
                label += f" ({year})"

            description = f"by {author}"[:100]

            options.append(discord.SelectOption(
                label=label[:100],
                description=description,
                value=str(i)
            ))

        self.select_menu = discord.ui.Select(
            placeholder="Choose a book",
            options=options
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)

    async def select_callback(self, interaction: discord.Interaction):
        index = int(self.select_menu.values[0])
        selected_book = self.results[index]

        # Fetch description from Open Library
        await interaction.response.edit_message(content="📖 Fetching book details...", view=None)
        description = await self._fetch_description(selected_book.get('open_library_key'))
        if description:
            selected_book['description'] = description

        embed = self._create_book_embed(selected_book)
        view = BookFormatView(selected_book, self.user_id, self.services)

        await interaction.edit_original_response(
            content="**Choose your format:**",
            embed=embed,
            view=view
        )

    async def _fetch_description(self, work_key: str) -> Optional[str]:
        """Fetch book description from Open Library works API"""
        if not work_key:
            return None
        try:
            data = await self.services.openlibrary.get(f"{work_key}.json")
            desc = data.get('description')
            if isinstance(desc, dict):
                return desc.get('value', '')
            elif isinstance(desc, str):
                return desc
        except Exception as e:
            logger.error(f"Failed to fetch book description: {e}")
        return None

    def _create_book_embed(self, book: dict) -> discord.Embed:
        title = book.get('title', 'Unknown')
        author = book.get('author', 'Unknown Author')
        year = book.get('year')

        embed = discord.Embed(
            title=title,
            description=book.get('description', 'No description available.')[:500],
            color=discord.Color.orange()
        )

        embed.add_field(name="Author", value=author, inline=True)
        if year:
            embed.add_field(name="Year", value=str(year), inline=True)
        if book.get('pages'):
            embed.add_field(name="Pages", value=str(book['pages']), inline=True)
        if book.get('editions'):
            embed.add_field(name="Editions", value=str(book['editions']), inline=True)

        cover_url = book.get('cover_url')
        if cover_url:
            embed.set_thumbnail(url=cover_url)

        return embed


class BookFormatView(RequesterOnlyView):
    """Format selection buttons for book requests"""
    def __init__(self, book: dict, user_id: int, services: BotServices):
        super().__init__(timeout=180)
        self.book = book
        self.user_id = user_id
        self.services = services

    @discord.ui.button(label="Ebook", style=discord.ButtonStyle.primary, emoji="📖")
    async def ebook(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_confirmation(interaction, "ebook")

    @discord.ui.button(label="Audiobook", style=discord.ButtonStyle.primary, emoji="🎧")
    async def audiobook(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_confirmation(interaction, "audiobook")

    @discord.ui.button(label="Both", style=discord.ButtonStyle.success, emoji="📚")
    async def both(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_confirmation(interaction, "both")

    async def _show_confirmation(self, interaction: discord.Interaction, format_type: str):
        format_labels = {"ebook": "📖 Ebook", "audiobook": "🎧 Audiobook", "both": "📖+🎧 Ebook & Audiobook"}
        self.book['request_format'] = format_type

        embed = discord.Embed(
            title=f"📚 {self.book.get('title', 'Unknown')}",
            description=f"**Format:** {format_labels[format_type]}\n**Author:** {self.book.get('author', 'Unknown')}",
            color=discord.Color.orange()
        )

        if self.book.get('cover_url'):
            embed.set_thumbnail(url=self.book['cover_url'])

        view = BookConfirmationView(self.book, self.user_id, self.services)
        await interaction.response.edit_message(
            content="**Confirm your request:**",
            embed=embed,
            view=view
        )


class _ConfirmRequestView(RequesterOnlyView):
    """"✅ Yes / ❌ No" before a request goes to the admins. Only the person asking
    can answer; each kind of request says how it's posted (_post)."""

    def __init__(self, user_id: int, services: BotServices):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.services = services

    async def _post(self, interaction: discord.Interaction) -> None:
        raise NotImplementedError

    @discord.ui.button(label="✅ Yes", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="✅ Request submitted! Waiting for admin approval...", embed=None, view=None)
        await self._send_to_admins(interaction)

    @discord.ui.button(label="❌ No", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="Request cancelled. Use `/request` to try again.", embed=None, view=None)

    async def _send_to_admins(self, interaction: discord.Interaction):
        """Send the request to the admin channel for approval."""
        try:
            await self._post(interaction)
        except AdminChannelUnavailable as e:
            logger.error(str(e))
            if not self.services.config.admin_channel_id:
                await interaction.followup.send("❌ Admin channel not configured. Please contact an administrator.", ephemeral=True)


class BookConfirmationView(_ConfirmRequestView):
    """Confirmation buttons for a book request."""

    def __init__(self, book: dict, user_id: int, services: BotServices):
        super().__init__(user_id, services)
        self.book = book

    async def _post(self, interaction: discord.Interaction) -> None:
        await post_book_request(interaction.client, self.services, interaction.user, self.book)


class _RequestApprovalBase(AdminActionView):
    """What the film/TV and book approval cards share: the error reply, and
    restoring the request from the database when a button is pressed."""

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.error(f"{type(self).__name__} error on {item.custom_id}: {error}", exc_info=True)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Error: {error}", ephemeral=True)
        except Exception:
            pass

    async def _load_from_saved(self, message_id: int, bot) -> bool:
        """Restore this view's request from the database (one keyed read). True if found."""
        try:
            record = await get_request(message_id)
            if record:
                self.user_id = record.get('user_id')
                self.requester_plex_id = record.get('plex_account_id')
                self.requester_plex_name = record.get('requester_name')
                self.services = bot.services
                self._restore(record, message_id, bot)
                return True
        except Exception as e:
            logger.error(f"Failed to load saved request {message_id}: {e}")
        return False

    def _restore(self, record: Dict[str, Any], message_id: int, bot) -> None:
        """Set the subclass's own fields from the saved record."""
        raise NotImplementedError


class BookAdminApprovalView(_RequestApprovalBase):
    """Admin approval buttons for book requests.

    Admin-gated: approving submits downloads to NZBHydra/SABnzbd.
    """
    def __init__(self, book: dict = None, user_id: int = None, services: BotServices = None):
        super().__init__(timeout=None)
        self.book = book
        # These two used to be assigned at the end of on_error, one indent level
        # out, which meant __init__ silently dropped both arguments *and* on_error
        # raised NameError on two free names - so whenever a button failed, the
        # handler meant to report it failed instead. Masked because approve and
        # decline both call _load_from_saved first, which sets all three.
        self.user_id = user_id
        self.services = services

    def _restore(self, record: Dict[str, Any], message_id: int, bot) -> None:
        self.book = record.get('media', {})

    async def _approve_core(self) -> Dict[str, Any]:
        """Send an approved book to NZBHydra/SABnzbd, per format.

        Shared by the Discord button and the website, so both do exactly the
        same thing. Returns the per-format outcome, the admin-card note and the
        requester's DM, without touching any Discord message.
        """
        ebook_ok = None
        audio_ok = None
        format_type = self.book.get('request_format', 'ebook')
        try:
            if format_type == 'both':
                self.book['request_format'] = 'ebook'
                ebook_ok = await self._submit_to_download()
                self.book['request_format'] = 'audiobook'
                audio_ok = await self._submit_to_download()
                self.book['request_format'] = 'both'
                download_success = ebook_ok or audio_ok
            else:
                download_success = await self._submit_to_download()
                if format_type == 'ebook':
                    ebook_ok = download_success
                else:
                    audio_ok = download_success
        except Exception as e:
            logger.error(f"Download submission error: {e}", exc_info=True)
            download_success = False

        logger.info(f"Download result: {download_success} (ebook={ebook_ok}, audiobook={audio_ok})")

        if format_type == 'both':
            note = (f"📖 Ebook: {'✅ Sent to SABnzbd' if ebook_ok else '❌ Not available'}"
                    f"\n🎧 Audiobook: {'✅ Sent to SABnzbd' if audio_ok else '❌ Not available'}")
        elif download_success:
            note = "📥 Sent to SABnzbd for download"
        else:
            note = "⚠️ Not available on indexers"

        title = self.book.get('title', 'Unknown')
        if format_type == 'both':
            msg = f"✅ **Your request for **{title}** has been approved!**\n"
            msg += "📖 Ebook: Sent for download\n" if ebook_ok else "📖 Ebook: ⚠️ Not available on indexers\n"
            msg += "🎧 Audiobook: Sent for download" if audio_ok else "🎧 Audiobook: ⚠️ Not available on indexers"
        else:
            format_label = "📖 Ebook" if format_type == 'ebook' else "🎧 Audiobook"
            if download_success:
                msg = f"✅ **Good news!** Your {format_label.lower()} request for **{title}** has been approved and sent for download!"
            else:
                msg = f"✅ Your request for **{title}** has been approved, but ⚠️ no {format_type} version was found on indexers."

        return {
            "download_success": download_success,
            "note": note,
            "user_message": msg,
            "followup_message": f"Book request approved{' and sent to SABnzbd' if download_success else ' (some formats not available)'}!",
        }

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, custom_id="approve_book_request")
    @single_flight
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Book approve button clicked by {interaction.user} for message {interaction.message.id}")

        # Disable buttons immediately to prevent double-clicks
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)


        # Always load from saved to ensure we have data
        # A view of its own for this click: after a restart one persistent view
        # answers every card, so state kept on it would leak between requests.
        req = type(self)()
        if not await req._load_from_saved(interaction.message.id, interaction.client):
            logger.error(f"Could not load book request data for message {interaction.message.id}")
            await interaction.followup.send("❌ Could not find request data. Please re-submit.", ephemeral=True)
            return

        logger.info(f"Book data loaded: {req.book.get('title', '?')}, format: {req.book.get('request_format', '?')}")

        result = await req._approve_core()

        embed = _stamp_decision(interaction.message.embeds[0], approved=True, by=interaction.user.mention, note=result['note'])
        await interaction.message.edit(embed=embed, view=None)
        await _after_decision(interaction.client, req.services, interaction.message.id, approved=True, actor=str(interaction.user),
                              user_id=req.user_id, title=req.book.get('title', 'Unknown'), what="book",
                              user_message=result.get("user_message", ""))

        await interaction.followup.send(result["followup_message"], ephemeral=True)

    async def _submit_to_download(self) -> bool:
        """Search NZBHydra for the book and send NZB to SABnzbd for download"""
        import xml.etree.ElementTree as ET
        import aiohttp as _aiohttp

        hydra_url = self.services.config.nzbhydra_url
        hydra_key = self.services.config.nzbhydra_api_key

        if not hydra_url or not hydra_key:
            logger.warning("NZBHydra not configured")
            return False
        if not self.services.sab.configured:
            logger.warning("SABnzbd not configured")
            return False

        try:
            _timeout = _aiohttp.ClientTimeout(total=30)
            title = self.book.get('title', '')
            author = self.book.get('author', '')
            format_type = self.book.get('request_format', 'ebook')

            # Determine categories
            if format_type == 'audiobook':
                hydra_cat = '3030'
                sab_cat = 'audiobooks'
            else:
                hydra_cat = '7020'
                sab_cat = 'ebooks'

            search_term = f"{title} {author}".strip()
            logger.info(f"Searching NZBHydra for: {search_term} (cat={hydra_cat})")

            async with _aiohttp.ClientSession(timeout=_timeout) as session:
                # 1. Search NZBHydra
                search_url = f"{hydra_url}/api"
                params = {
                    "t": "search",
                    "q": search_term,
                    "cat": hydra_cat,
                    "apikey": hydra_key,
                    "limit": "20"
                }

                async with session.get(search_url, params=params) as resp:
                    if resp.status != 200:
                        logger.error(f"NZBHydra search failed: {resp.status}")
                        return False
                    xml_text = await resp.text()

                # 2. Parse XML results
                root = ET.fromstring(xml_text)

                # Check for API error
                error_elem = root.find('.//{http://www.newznab.com/DTD/2010/feeds/attributes/}error')
                if error_elem is None:
                    error_elem = root.find('.//error')
                if error_elem is not None:
                    logger.error(f"NZBHydra API error: {error_elem.get('description', 'unknown')}")
                    return False

                items = root.findall('.//item')
                if not items:
                    logger.warning(f"No NZBHydra results for: {search_term}")
                    return False

                logger.info(f"NZBHydra returned {len(items)} results")

                # 3. Rank results
                ns = {'newznab': 'http://www.newznab.com/DTD/2010/feeds/attributes/'}

                candidates = []
                for item in items:
                    # A malformed result is skipped, not fatal - see result_title().
                    item_title = result_title(item)
                    if not item_title:
                        logger.debug("Skipping NZBHydra result with no title")
                        continue
                    title_lower = item_title.lower()

                    # Language is a ranking signal, never a filter - see
                    # looks_foreign_language(). Dropping matches here discarded
                    # legitimate English books whose titles contain a nationality.
                    is_foreign = looks_foreign_language(item_title)

                    # Get grabs count
                    grabs = 0
                    for attr in item.findall('.//newznab:attr', ns):
                        if attr.get('name') == 'grabs':
                            try:
                                grabs = int(attr.get('value', '0'))
                            except ValueError:
                                grabs = 0

                    # Get NZB download link
                    link = item.find('link')
                    nzb_url = link.text if link is not None else None

                    if not nzb_url:
                        enclosure = item.find('enclosure')
                        if enclosure is not None:
                            nzb_url = enclosure.get('url')

                    if nzb_url:
                        # Bonus score for EPUB format in ebook searches
                        has_epub = 'epub' in title_lower
                        candidates.append({
                            'title': item_title,
                            'grabs': grabs,
                            'has_epub': has_epub,
                            'is_foreign': is_foreign,
                            'nzb_url': nzb_url
                        })

                if not candidates:
                    logger.warning(f"No usable results for: {search_term}")
                    return False

                # Sort: English-looking first, then EPUB (for ebooks), then by
                # grabs. reverse=True puts True before False, so the key uses
                # "not is_foreign" to rank English-looking releases first.
                if format_type != 'audiobook':
                    candidates.sort(
                        key=lambda x: (not x['is_foreign'], x['has_epub'], x['grabs']),
                        reverse=True,
                    )
                else:
                    candidates.sort(
                        key=lambda x: (not x['is_foreign'], x['grabs']),
                        reverse=True,
                    )

                best = candidates[0]
                logger.info(
                    f"Selected: '{best['title']}' ({best['grabs']} grabs, "
                    f"epub={best['has_epub']}) from {len(candidates)} candidates"
                )
                if best['is_foreign']:
                    logger.warning(
                        f"Best candidate carries a language tag and may not be "
                        f"English: '{best['title']}'"
                    )

                # 4. Send NZB to SABnzbd
                try:
                    result = await self.services.sab.call("addurl", name=best['nzb_url'], cat=sab_cat, timeout=30)
                except ServiceError as e:
                    logger.error(f"SABnzbd: {e}")
                    return False
                if result.get('status'):
                    logger.info(f"Sent to SABnzbd: '{best['title']}' → category '{sab_cat}'")

                    # Write hint file for bookshelf_processor plugin
                    try:
                        watch_dir = self.services.config.bookshelf_ebook_watch if sab_cat == 'ebooks' else self.services.config.bookshelf_audiobook_watch
                        if watch_dir:
                            hint_data = {
                                "title": self.book.get('title', ''),
                                "author": self.book.get('author', ''),
                                "year": self.book.get('year'),
                                "isbn": self.book.get('isbn'),
                                "cover_url": self.book.get('cover_url'),
                                "format": format_type,
                                "nzb_title": best['title'],
                                "requested_by": self.user_id,
                                # Asked on the website without Discord: told by phone alert or email instead.
                                "requested_by_plex_id": getattr(self, 'requester_plex_id', None),
                                "requested_by_plex_name": getattr(self, 'requester_plex_name', None),
                            }
                            # Use the NZB title as the hint filename since SABnzbd
                            # creates a folder with this name
                            safe_name = re.sub(r'[<>:"/\\|?*]', '_', best['title'])
                            hint_path = Path(watch_dir) / f".plexbie_hint_{safe_name}.json"
                            # The watch dir is on the array via shfs: a 400-byte
                            # write measured 49 ms at p95, 137 ms max.
                            await run_blocking(
                                hint_path.write_text, json.dumps(hint_data, indent=2)
                            )
                            logger.info(f"Wrote hint file: {hint_path.name}")
                    except Exception as e:
                        logger.warning(f"Could not write hint file: {e}")

                    return True
                else:
                    logger.error(f"SABnzbd rejected download: {result}")
                    return False

        except Exception as e:
            logger.error(f"Failed to submit download: {e}", exc_info=True)
            return False

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger, custom_id="decline_book_request")
    @single_flight
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Book decline button clicked by {interaction.user} for message {interaction.message.id}")
        # Always load from saved
        # A view of its own for this click: after a restart one persistent view
        # answers every card, so state kept on it would leak between requests.
        req = type(self)()
        if not await req._load_from_saved(interaction.message.id, interaction.client):
            await interaction.response.send_message("❌ Could not find request data.", ephemeral=True)
            return

        embed = _stamp_decision(interaction.message.embeds[0], approved=False, by=interaction.user.mention)
        await interaction.response.edit_message(embed=embed, view=None)
        await _after_decision(interaction.client, req.services, interaction.message.id, approved=False, actor=str(interaction.user),
                              user_id=req.user_id, title=req.book.get('title', 'Unknown'), what="book")


class ConfirmationView(_ConfirmRequestView):
    """Confirmation buttons for a film or TV request."""

    def __init__(self, media: dict, user_id: int, services: BotServices, seasons=None, monitor=False):
        super().__init__(user_id, services)
        self.media = media
        self.seasons = seasons
        self.monitor = monitor

    async def _post(self, interaction: discord.Interaction) -> None:
        await post_media_request(
            interaction.client, self.services, interaction.user,
            self.media, seasons=self.seasons, monitor=self.monitor,
        )


class AdminApprovalView(_RequestApprovalBase):
    """Admin approval buttons.

    Admin-gated: approving submits to Seerr and changes Sonarr/Radarr
    monitoring state.
    """

    def __init__(self, media: dict = None, user_id: int = None, services: BotServices = None, seasons=None, monitor=False):
        super().__init__(timeout=None)  # Persistent
        self.media = media
        self.user_id = user_id
        self.services = services
        self.seasons = seasons
        self.monitor = monitor
        self._seerr_noop_reason = None
        self._seerr_refused = None
        #: Set when the show has no TheTVDB entry: Sonarr can't take it, so it needs a hand download.
        self._no_tvdb = False

    def _restore(self, record: Dict[str, Any], message_id: int, bot) -> None:
        self.media = record.get('media', {})
        self.seasons = record.get('seasons')
        self.monitor = record.get('monitor', False)
        self._message_id = message_id
        self._bot = bot

    def _title(self) -> str:
        return self.media.get('title') or self.media.get('name', 'Unknown')

    def _requested_seasons_list(self):
        if self.seasons == "all" or not self.seasons:
            return None
        if isinstance(self.seasons, list):
            return sorted({int(season) for season in self.seasons})
        return None

    async def _monitor_and_search_seasons(self, series: Dict[str, Any], requested_seasons, episodes: List[Dict[str, Any]]) -> List[int]:
        """Make the requested seasons wanted in Sonarr and search the ones with episodes missing.

        Monitoring alone only catches releases as they appear, so seasons that
        already aired were never fetched when a show was already in Sonarr: the
        Simpsons season 2 request sat approved with nothing downloading. Marks
        each requested season monitored at the season level (episode flags were
        set already), then runs a search for every season still missing aired
        episodes. Returns the seasons searched.
        """
        sonarr = self.services.sonarr
        wanted = ({int(n) for n in requested_seasons} if requested_seasons is not None
                  else {int(s.get("seasonNumber", 0)) for s in series.get("seasons", []) if s.get("seasonNumber")})
        full = await sonarr.get(f"series/{series['id']}")
        if any(int(s.get("seasonNumber", 0)) in wanted and not s.get("monitored") for s in full.get("seasons", [])):
            for season in full.get("seasons", []):
                if int(season.get("seasonNumber", 0)) in wanted:
                    season["monitored"] = True
            await sonarr.put(f"series/{series['id']}", full)

        now = datetime.now(timezone.utc).isoformat()
        missing = sorted({int(e.get("seasonNumber", 0)) for e in episodes
                          if int(e.get("seasonNumber", 0)) in wanted and not e.get("hasFile")
                          and e.get("airDateUtc") and e["airDateUtc"] <= now})
        if not missing:
            return []
        # Season by season in the background, falling back to single episodes when no
        # whole-season release exists; seasons nobody has open a "Can't be found"
        # help request for the admins.
        from core import season_search
        bot, key = getattr(self, "_bot", None), getattr(self, "_message_id", None)

        async def nothing(seasons):
            if bot is not None:
                await season_search.open_not_found_help(bot, key, seasons)
        season_search.start(self.services, series["id"], missing, title=self._title(), on_nothing=nothing)
        logger.info(f"Started Sonarr search for {self._title()} season(s) {missing}")
        return missing

    def _follow_up_movie(self, settle: Optional[float] = None) -> None:
        """core/verified_search.follow_up_new_movie for this request: nothing found by
        ID opens a "Can't be found" help request asking about searching by name."""
        from core import season_search, verified_search
        bot, key = getattr(self, "_bot", None), getattr(self, "_message_id", None)

        async def nothing(report):
            if bot is not None:
                await season_search.open_movie_not_found_help(bot, key, report)
        extra = {} if settle is None else {"settle": settle}
        verified_search.follow_up_new_movie(self.services, tmdb_id=int(self.media["id"]), title=self._title(),
                                            on_nothing=nothing, **extra)

    async def _restore_existing_request_monitoring(self) -> Optional[Dict[str, str]]:
        media_type = "movie" if self.media.get('media_type') == 'movie' else "tv"
        tmdb_id = self.media.get('id')
        title = self._title()

        if media_type == "movie" and self.services.radarr.configured:
            movies = await self.services.radarr.movies()
            match = next((movie for movie in movies if movie.get("tmdbId") == tmdb_id), None)
            if not match:
                return None

            searched = False
            if not match.get("hasFile"):
                self._follow_up_movie(settle=0)       # search by ID, checking it's really the film
                searched = True
            if not match.get("monitored", False):
                await self.services.radarr.set_movie_monitored(match["id"], True)
                return {
                    "mode": "restored",
                    "admin_note": "Monitoring restored in Radarr (existing item)" + (", search started." if searched else "."),
                    "user_message": f"✅ **Good news!** Your request for **{title}** was approved and should be available shortly.",
                    "followup_message": "Existing Radarr item found — monitoring turned back on.",
                }

            return {
                "mode": "already_monitored",
                "admin_note": "Already in Radarr and monitored" + ("; search started." if searched else "."),
                "user_message": f"✅ **Good news!** Your request for **{title}** was approved and should be available shortly.",
                "followup_message": "Existing Radarr item was already monitored.",
            }

        if media_type == "tv" and self.services.sonarr.configured:
            series_list = await self.services.sonarr.series()
            match = next((series for series in series_list if series.get("tmdbId") == tmdb_id), None)
            if not match:
                return None

            requested_seasons = self._requested_seasons_list()
            existing_episodes = await self.services.sonarr.episodes(match["id"])

            if requested_seasons is None:
                target_episodes = existing_episodes
            else:
                season_set = {int(season) for season in requested_seasons}
                target_episodes = [episode for episode in existing_episodes if int(episode.get("seasonNumber", 0)) in season_set]

            had_unmonitored_targets = any(not episode.get("monitored", False) for episode in target_episodes)
            if target_episodes:
                await self.services.sonarr.set_episodes_monitored(match["id"], requested_seasons, True, existing_episodes)
            needs_series_enable = not match.get("monitored", False)
            if needs_series_enable:
                await self.services.sonarr.set_series_monitored(match["id"], True)

            searched = await self._monitor_and_search_seasons(match, requested_seasons, existing_episodes)
            search_note = f" Search started for season {', '.join(str(n) for n in searched)}." if searched else ""

            restored_any = needs_series_enable or had_unmonitored_targets
            if restored_any:
                scope = "requested seasons" if requested_seasons else "series"
                return {
                    "mode": "restored",
                    "admin_note": f"Monitoring restored in Sonarr for existing {scope}.{search_note}",
                    "user_message": f"✅ **Good news!** Your request for **{title}** was approved and should be available shortly.",
                    "followup_message": "Existing Sonarr item found — monitoring turned back on.",
                }

            return {
                "mode": "already_monitored",
                "admin_note": f"Already present and already monitored in Sonarr.{search_note}",
                "user_message": f"✅ **Good news!** Your request for **{title}** was approved and should be available shortly.",
                "followup_message": "Existing Sonarr item was already monitored.",
            }

        return None

    async def _fulfill_request(self) -> Dict[str, Any]:
        try:
            restored = await self._restore_existing_request_monitoring()
            if restored:
                restored["success"] = True
                return restored
        except Exception as e:
            logger.error(f"Failed to restore monitoring for existing media: {e}", exc_info=True)

        submitted = await self._submit_to_seerr()
        title = self._title()
        if submitted and self.media.get("media_type") == "movie" and self.media.get("id"):
            # Seerr adds the film to Radarr and Radarr searches; check what it grabbed
            # really is the film, and search by ID ourselves if it found nothing.
            self._follow_up_movie()
        if submitted and self._no_tvdb:
            return {
                "success": True,
                "mode": "manual",
                "admin_note": NO_TVDB_NOTE,
                "user_message": f"✅ Your request for **{title}** was approved. It can't be fetched automatically, "
                                "so an admin will add it by hand. That can take a little longer.",
                "followup_message": f"Approved, but {NO_TVDB_NOTE[0].lower()}{NO_TVDB_NOTE[1:]}",
            }
        if submitted and self.media.get("media_type") != "movie" and self.media.get("id"):
            # Seerr adds the show to Sonarr; Sonarr's search-on-add skips episodes
            # it thinks haven't aired. Follow up with our own season search.
            from core import season_search
            bot, key = getattr(self, "_bot", None), getattr(self, "_message_id", None)

            async def nothing(seasons):
                if bot is not None:
                    await season_search.open_not_found_help(bot, key, seasons)
            season_search.follow_up_new_show(self.services, tmdb_id=int(self.media["id"]),
                                             seasons=self._requested_seasons_list(), title=title, on_nothing=nothing)
        if submitted:
            if self._seerr_noop_reason:
                return {
                    "success": True,
                    "mode": "seerr_noop",
                    "admin_note": self._seerr_noop_reason,
                    "user_message": f"✅ **Good news!** Your request for **{title}** was approved and should be available shortly.",
                    "followup_message": "Request approved; Seerr already had no requestable seasons left.",
                }
            return {
                "success": True,
                "mode": "seerr",
                "admin_note": "Submitted to Seerr.",
                "user_message": f"✅ **Good news!** Your request for **{title}** was approved and should be available shortly.",
                "followup_message": "Request approved and submitted to Seerr!",
            }

        return {
            "success": False,
            "mode": "failed",
            "followup_message": self._seerr_refused
            or "Failed to submit to Seerr or restore monitoring. Please try manually.",
        }

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success, custom_id="approve_request")
    @single_flight
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Media approve button clicked by {interaction.user} for message {interaction.message.id}")
        try:
            await interaction.response.defer()
        except Exception as e:
            logger.error(f"Failed to defer media approval: {e}")
            return

        # Always load from saved to ensure we have data
        # A view of its own for this click: after a restart one persistent view
        # answers every card, so state kept on it would leak between requests.
        req = type(self)()
        if not await req._load_from_saved(interaction.message.id, interaction.client):
            logger.error(f"Could not load media request data for message {interaction.message.id}")
            await interaction.followup.send("❌ Could not find request data. Please re-submit.", ephemeral=True)
            return

        # Fulfill by restoring monitoring if the item already exists, otherwise submit to Seerr
        result = await req._fulfill_request()
        
        if result.get("success"):
            embed = _stamp_decision(interaction.message.embeds[0], approved=True, by=interaction.user.mention,
                                    note=result.get('admin_note', ''))
            await interaction.message.edit(embed=embed, view=None)
            await _after_decision(interaction.client, req.services, interaction.message.id, approved=True, actor=str(interaction.user),
                                  user_id=req.user_id, title=req._title(), what="media", user_message=result.get("user_message", ""))

            # Register with media tracking system
            await req._register_with_tracking()

            await interaction.followup.send(result["followup_message"], ephemeral=True)
        else:
            await interaction.followup.send(result["followup_message"], ephemeral=True)
    
    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger, custom_id="decline_request")
    @single_flight
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        logger.info(f"Media decline button clicked by {interaction.user} for message {interaction.message.id}")
        # Always load from saved
        # A view of its own for this click: after a restart one persistent view
        # answers every card, so state kept on it would leak between requests.
        req = type(self)()
        if not await req._load_from_saved(interaction.message.id, interaction.client):
            await interaction.response.send_message("❌ Could not find request data.", ephemeral=True)
            return

        embed = _stamp_decision(interaction.message.embeds[0], approved=False, by=interaction.user.mention)
        await interaction.response.edit_message(embed=embed, view=None)
        # Recorded, not removed: the daily Sonarr/Radarr monitoring reconciliation still reads it.
        await _after_decision(interaction.client, req.services, interaction.message.id, approved=False, actor=str(interaction.user),
                              user_id=req.user_id, title=req._title(), what="media")
    
    async def _register_with_tracking(self):
        """Register approved request with media tracking system"""
        try:
            from core.media_tracking import get_media_tracker

            tracker = get_media_tracker()

            title = self.media.get('title') or self.media.get('name', 'Unknown')
            tmdb_id = self.media.get('id')
            media_type = "movie" if self.media.get('media_type') == 'movie' else "tv"

            # Get poster URL
            poster_path = self.media.get('poster_path')
            poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None

            overview = self.media.get('overview', 'No description available.')

            # For TV shows, register each season separately
            if media_type == "tv":
                if isinstance(self.seasons, list):
                    # Specific seasons requested
                    for season_num in self.seasons:
                        tracker.register_request(
                            tmdb_id=tmdb_id,
                            media_type="tv",
                            title=title,
                            requester_user_id=self.user_id,
                            requester_plex_id=getattr(self, 'requester_plex_id', None),
                            requester_plex_name=getattr(self, 'requester_plex_name', None),
                            season_number=season_num,
                            requested_seasons=self.seasons,
                            monitor=self.monitor,
                            poster_url=poster_url,
                            overview=overview,
                        )
                elif self.seasons == "all":
                    # All seasons - register with season_number=None for now
                    # Will be updated when Sonarr grabs episodes
                    tracker.register_request(
                        tmdb_id=tmdb_id,
                        media_type="tv",
                        title=title,
                        requester_user_id=self.user_id,
                        requester_plex_id=getattr(self, 'requester_plex_id', None),
                        requester_plex_name=getattr(self, 'requester_plex_name', None),
                        season_number=None,
                        requested_seasons="all",
                        monitor=self.monitor,
                        poster_url=poster_url,
                        overview=overview,
                    )
            else:
                # Movie
                tracker.register_request(
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    title=title,
                    requester_user_id=self.user_id,
                    requester_plex_id=getattr(self, 'requester_plex_id', None),
                    requester_plex_name=getattr(self, 'requester_plex_name', None),
                    monitor=self.monitor,
                    poster_url=poster_url,
                    overview=overview,
                )

            logger.info(f"Registered tracking for {media_type}: {title} (TMDB: {tmdb_id})")

        except Exception as e:
            logger.error(f"Error registering with tracking system: {e}", exc_info=True)

    async def _submit_to_seerr(self) -> bool:
        """Submit request to Seerr API"""
        if not self.services.seerr.configured:
            logger.warning("Seerr not configured")
            return False

        try:
            media_type = "movie" if self.media.get('media_type') == 'movie' else "tv"

            payload = {
                "mediaType": media_type,
                "mediaId": self.media.get('id')
            }

            # For TV shows, add season selection
            if media_type == "tv":
                if self.seasons == "all":
                    payload["seasons"] = "all"
                elif self.seasons:
                    # Specific seasons selected
                    payload["seasons"] = self.seasons
                else:
                    # Default to all seasons if none specified
                    payload["seasons"] = "all"

                # Add monitoring if requested
                if self.monitor:
                    payload["is4k"] = False  # Assuming non-4K for now
                    # Note: Seerr doesn't have a direct "monitor" flag
                    # Monitoring is typically handled by Sonarr/Radarr after the request
                    # We're just requesting the specific season(s)

            if media_type == "tv" and await no_tvdb_entry(self.services, self.media.get('id')):
                # Sonarr only knows shows by their TheTVDB ID. Seerr would accept this one,
                # fail to pass it to Sonarr and quietly delete its own request (as it did with
                # Faraway Downs), so it isn't sent: it's approved here and flagged for a hand
                # download instead, on the admin card and on Manage → All requests.
                self._no_tvdb = True
                if getattr(self, "_message_id", None):
                    await set_fields(self._message_id, no_tvdb=True)
                logger.warning(f"{self.media.get('title') or self.media.get('name')} has no TheTVDB entry: "
                               "approved, but not sent to Seerr (Sonarr can't take it); it needs a hand download")
                return True

            from webhooks.seerr_handler import remember_submission
            remember_submission(media_type, self.media.get('id'))
            status, text = await self.services.seerr.post("request", payload, raw=True)
            if status in (200, 201):
                # Keep Seerr's number for it, so its webhook events find this request.
                try:
                    rid = (json.loads(text) or {}).get("id")
                    if rid and getattr(self, "_message_id", None):
                        await set_fields(self._message_id, overseerr_request_id=rid)
                except (ValueError, AttributeError):
                    pass
                logger.info(f"Successfully submitted request to Seerr: {self.media.get('title')} - Seasons: {self.seasons}")
                return True
            if status == 202 and "No seasons available to request" in text:
                self._seerr_noop_reason = "Seerr already has no requestable seasons left for this item."
                title = self.media.get('title') or self.media.get('name')
                logger.info(f"Seerr reported no seasons available to request; treating as already handled: {title} - Seasons: {self.seasons}")
                return True
            if status == 403 and "blocklist" in text.lower():
                # Seerr: an admin put this title on its blocklist.
                self._seerr_refused = ("Seerr refused it: this title is on its blocklist. "
                                           "Remove it from the blocklist in Seerr first if it should be allowed.")
                logger.info(f"Seerr's blocklist refused {self.media.get('title') or self.media.get('name')}")
                return False
            logger.error(f"Seerr API error: {status} - {text}")
            return False

        except Exception as e:
            logger.error(f"Failed to submit to Seerr: {e}")
            return False


class MediaRequestsCog(commands.Cog):
    """Media request system with ephemeral in-channel flow and admin approval"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services


    async def cog_load(self):
        """Register the persistent approval views.

        Must be here, not in setup(): core.plugin_manager instantiates the cog
        class and calls bot.add_cog directly, so a module-level setup() is never
        invoked by this bot. These two views had been registered only in setup(),
        which means approve/decline on a pending request stopped working after
        every restart despite the buttons carrying custom_ids.
        """
        self.bot.add_view(AdminApprovalView())
        self.bot.add_view(BookAdminApprovalView())
        logger.info("✅ Registered persistent media/book approval views")

    @app_commands.command(name="requests", description="Media requests still awaiting a decision")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def list_requests(self, interaction: discord.Interaction):
        """Show requests with no recorded outcome, newest first.

        Outcome recording started on 2026-09-30. Everything submitted before that
        has no outcome stored, because nothing ever updated the status field - so
        this cannot tell an untouched request from one approved months ago. Rather
        than guess, those are counted separately and each entry carries a link to
        its approval message, where the presence of buttons is the real answer.
        """
        await interaction.response.defer(ephemeral=True)

        try:
            outstanding = await pending_requests()
            if not outstanding:
                await interaction.followup.send(
                    "✅ Nothing waiting on a decision.", ephemeral=True
                )
                return

            def submitted_at(record):
                stamp = record.get("timestamp")
                try:
                    value = datetime.fromisoformat(stamp)
                except (TypeError, ValueError):
                    return datetime.min.replace(tzinfo=timezone.utc)
                return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

            ordered = sorted(
                outstanding.items(), key=lambda kv: submitted_at(kv[1]), reverse=True
            )

            tracked_from = datetime(2026, 9, 30, tzinfo=timezone.utc)
            before_tracking = sum(
                1 for _, record in ordered if submitted_at(record) < tracked_from
            )

            embed = discord.Embed(
                title="📥 Requests awaiting a decision",
                description=f"{len(ordered)} with no recorded outcome",
                color=discord.Color.blurple(),
            )

            guild_id = interaction.guild.id if interaction.guild else None
            channel_id = self.services.config.admin_channel_id

            for message_id, record in ordered[:MAX_LISTED_REQUESTS]:
                media = record.get("media") or {}
                title = media.get("title") or media.get("name") or "Unknown"
                kind = record.get("media_type") or media.get("media_type") or "?"
                when = submitted_at(record)

                lines = [f"**Type:** {kind}"]
                if record.get("user_id"):
                    lines.append(f"**Requested by:** <@{record['user_id']}>")
                lines.append(f"**Submitted:** <t:{int(when.timestamp())}:R>")
                if guild_id and channel_id:
                    lines.append(
                        f"[Open the approval message]"
                        f"(https://discord.com/channels/{guild_id}/{channel_id}/{message_id})"
                    )

                embed.add_field(
                    name=truncate_field(title, limit=256),
                    value=truncate_field("\n".join(lines)),
                    inline=False,
                )

            notes = []
            if len(ordered) > MAX_LISTED_REQUESTS:
                notes.append(f"Showing newest {MAX_LISTED_REQUESTS} of {len(ordered)}")
            if before_tracking:
                notes.append(
                    f"{before_tracking} predate outcome tracking - open the message "
                    f"to see whether the buttons are still there"
                )
            if notes:
                embed.set_footer(text=" · ".join(notes))

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await reply_failure(interaction, logger, "Error listing requests", e)

    @app_commands.command(name="request", description="Request media (TV, Movie, Audiobook, or Ebook)")
    @app_commands.guild_only()
    async def request_media(self, interaction: discord.Interaction):
        """Start media request flow — choose media type first"""
        view = MediaTypeSelectView(cog=self, user_id=interaction.user.id)
        await interaction.response.send_message(
            "**What would you like to request?**",
            view=view,
            ephemeral=True
        )
    
    async def _search_open_library(self, query: str) -> List[dict]:
        """Search Open Library for books"""
        try:
            params = {
                "q": query,
                "limit": 10,
                "fields": "key,title,author_name,first_publish_year,cover_edition_key,isbn,edition_count,number_of_pages_median,subject"
            }

            data = await self.services.openlibrary.get("search.json", **params)
            results = []
            for doc in data.get('docs', [])[:10]:
                authors = doc.get('author_name', [])
                cover_key = doc.get('cover_edition_key')
                isbns = doc.get('isbn', [])

                result = {
                    'title': doc.get('title', 'Unknown'),
                    'author': ', '.join(authors[:2]) if authors else 'Unknown Author',
                    'year': doc.get('first_publish_year'),
                    'cover_url': f"https://covers.openlibrary.org/b/olid/{cover_key}-M.jpg" if cover_key else None,
                    'isbn': isbns[0] if isbns else None,
                    'editions': doc.get('edition_count'),
                    'pages': doc.get('number_of_pages_median'),
                    'open_library_key': doc.get('key'),
                    'subjects': doc.get('subject', [])[:5],
                }
                results.append(result)
            return results

        except Exception as e:
            logger.error(f"Open Library search error: {e}")

        return []

    async def _search_tmdb(self, query: str) -> List[dict]:
        """Search TMDB for media"""
        if not self.services.config.tmdb_api_key:
            # Mock data for testing
            return [
                {
                    "id": 1,
                    "title": query,
                    "media_type": "movie",
                    "overview": "Mock movie result",
                    "release_date": "2024-01-01",
                    "vote_average": 8.0
                }
            ]
        
        try:
            data = await self.services.tmdb.get("search/multi", query=query, include_adult="false")
            # Only movies and TV shows
            return [r for r in (data or {}).get('results', []) if r.get('media_type') in ['movie', 'tv']]

        except Exception as e:
            logger.error(f"TMDB search error: {e}")
        
        return []


