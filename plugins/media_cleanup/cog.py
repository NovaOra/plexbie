# path: plugins/media_cleanup/cog.py
"""Media cleanup plugin - removes unwatched content after 3 months"""
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button

from core.blocking import run_blocking
from core.logging import get_logger
from core.permissions import AdminOnlyView, require_admin
from core.services import BotServices
from utils.embeds import create_info_embed, truncate_field
from utils.views import reply_failure
from utils.guids import guid_number
from database.kv_store import kv_get, kv_set
from database.request_store import all_requests

logger = get_logger(__name__)

# Namespace for cleanup data
CLEANUP_NAMESPACE = "media_cleanup"
#: Seerr's media status for something Plex no longer has.
SEERR_DELETED = 7

from portal.cleanup import aware as _aware  # noqa: E402  (plexapi's zone-less local times)


def _ids_of(item) -> Dict[str, str]:
    """A Plex item's TMDB / TheTVDB / IMDb ids, from its guids ("tmdb://1396")."""
    out: Dict[str, str] = {}
    for g in getattr(item, "guids", None) or []:
        gid = str(getattr(g, "id", "") or "")
        if "://" in gid:
            k, v = gid.split("://", 1)
            if k in ("tmdb", "tvdb", "imdb") and v:
                out.setdefault(k, v)
    return out
CHECK_EVERY = timedelta(hours=24)

REQUEST_EXPIRY_DAYS = 90

# Default configuration
DEFAULT_CONFIG = {
    "enabled": True,
    "inactivity_days": 90,  # 3 months
    "dry_run": True,  # Set to False to actually delete
    "exclude_libraries": [],  # Library names to exclude from cleanup
    "exempt_items": {},  # rating_key -> {title, type, added_at, exempted_at}
    "notification_channel_id": None,
    "notify_days_before": 7,  # Warn 7 days before deletion
}


class CleanupControlPanel(AdminOnlyView):
    """Interactive control panel for media cleanup.

    Admin-gated by AdminOnlyView.interaction_check, which runs before any button
    callback below - do not re-check per button.
    """

    def __init__(self, cog):
        super().__init__(timeout=None)  # Persistent view
        self.cog = cog

    @discord.ui.button(label="📊 View Status", style=discord.ButtonStyle.primary, custom_id="cleanup_status")
    async def status_button(self, interaction: discord.Interaction, button: Button):
        """Show current cleanup status"""
        await interaction.response.defer(ephemeral=True)

        try:
            embed = discord.Embed(
                title="🗑️ Media Cleanup Status",
                description="Current configuration and statistics",
                color=discord.Color.blue()
            )

            status = "✅ Enabled" if self.cog.config["enabled"] else "❌ Disabled"
            mode = "🔵 Dry Run" if self.cog.config["dry_run"] else "🔴 Live Mode"

            embed.add_field(name="Status", value=status, inline=True)
            embed.add_field(name="Mode", value=mode, inline=True)
            embed.add_field(
                name="Inactivity Threshold",
                value=f"{self.cog.config['inactivity_days']} days",
                inline=True
            )
            embed.add_field(
                name="Warning Period",
                value=f"{self.cog.config['notify_days_before']} days before deletion",
                inline=True
            )

            if self.cog.config["exclude_libraries"]:
                embed.add_field(
                    name="Excluded Libraries",
                    value=truncate_field("\n".join(f"• {lib}" for lib in self.cog.config["exclude_libraries"])),
                    inline=False
                )

            exempt_count = len(self.cog.config.get("exempt_items", {}))
            embed.add_field(
                name="Exempt Media",
                value=f"{exempt_count} item(s)",
                inline=True
            )

            total_deleted = len(self.cog.tracking_data)
            embed.add_field(
                name="Total Items Cleaned",
                value=f"{total_deleted} items",
                inline=True
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await reply_failure(interaction, logger, "Error showing status", e)

    @discord.ui.button(label="🔄 Run Scan Now", style=discord.ButtonStyle.success, custom_id="cleanup_run")
    async def run_button(self, interaction: discord.Interaction, button: Button):
        """Run cleanup scan immediately"""
        await interaction.response.defer(ephemeral=True)

        try:
            if not self.cog.config["enabled"]:
                await interaction.followup.send(
                    "⚠️ Media cleanup is currently disabled. Enable it first using the ⚙️ Settings button.",
                    ephemeral=True
                )
                return

            mode_text = "🔵 **DRY RUN MODE**" if self.cog.config["dry_run"] else "🔴 **LIVE MODE**"

            embed = discord.Embed(
                title="🔄 Running Cleanup Scan...",
                description=f"{mode_text}\n\nScanning all libraries for inactive media...",
                color=discord.Color.blue() if self.cog.config["dry_run"] else discord.Color.red()
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

            # Run the cleanup check
            await self.cog.run_cleanup_scan(interaction)

        except Exception as e:
            await reply_failure(interaction, logger, "Error running scan", e)

    @discord.ui.button(label="⚙️ Settings", style=discord.ButtonStyle.secondary, custom_id="cleanup_settings")
    async def settings_button(self, interaction: discord.Interaction, button: Button):
        """Show settings menu"""
        view = CleanupSettingsView(self.cog)
        embed = discord.Embed(
            title="⚙️ Cleanup Settings",
            description="Configure media cleanup behavior",
            color=discord.Color.gold()
        )

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="🔴 Toggle Dry Run", style=discord.ButtonStyle.danger, custom_id="cleanup_toggle_dry")
    async def toggle_dry_run_button(self, interaction: discord.Interaction, button: Button):
        """Toggle dry run mode"""
        # Load persisted config before mutating it, otherwise save_config() would
        # write DEFAULT_CONFIG back over the stored settings (losing exempt_items,
        # exclude_libraries, inactivity_days, ...) whenever this fires before the
        # first successful load.
        await self.cog.load_data()

        self.cog.config["dry_run"] = not self.cog.config["dry_run"]
        await self.cog.save_config()

        mode = "🔵 Dry Run (Safe)" if self.cog.config["dry_run"] else "🔴 Live Mode (Deletes Files)"

        embed = discord.Embed(
            title="✅ Dry Run Mode Toggled",
            description=f"Current mode: **{mode}**",
            color=discord.Color.blue() if self.cog.config["dry_run"] else discord.Color.red()
        )

        if not self.cog.config["dry_run"]:
            embed.add_field(
                name="⚠️ Warning",
                value="Live mode is now active. The next scan will **permanently delete files**!",
                inline=False
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"{interaction.user} toggled dry run to: {self.cog.config['dry_run']}")


class CleanupSettingsView(AdminOnlyView):
    """Settings submenu for cleanup configuration.

    Admin-gated by AdminOnlyView. Ephemeral delivery is not a permission
    boundary, so this does not rely on how the view was sent.
    """

    def __init__(self, cog):
        super().__init__(timeout=180)
        self.cog = cog

    @discord.ui.button(label="✅ Enable/Disable", style=discord.ButtonStyle.secondary)
    async def toggle_enabled(self, interaction: discord.Interaction, button: Button):
        """Toggle cleanup enabled/disabled"""
        # See toggle_dry_run_button: load before mutate-and-save.
        await self.cog.load_data()

        self.cog.config["enabled"] = not self.cog.config["enabled"]
        await self.cog.save_config()

        status = "✅ Enabled" if self.cog.config["enabled"] else "❌ Disabled"
        await interaction.response.send_message(
            f"Cleanup is now: **{status}**",
            ephemeral=True
        )

    @discord.ui.button(label="📅 Set Days (90)", style=discord.ButtonStyle.secondary)
    async def set_days(self, interaction: discord.Interaction, button: Button):
        """Show instructions for setting inactivity days"""
        embed = discord.Embed(
            title="📅 Set Inactivity Days",
            description=f"Current: **{self.cog.config['inactivity_days']} days**\n\nTo change, use:\n`/cleanup config inactivity_days:<number>`",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="📢 Set Notification Channel", style=discord.ButtonStyle.secondary)
    async def set_channel(self, interaction: discord.Interaction, button: Button):
        """Show instructions for setting notification channel"""
        current_channel = "Not set"
        if self.cog.config.get("notification_channel_id"):
            channel = interaction.guild.get_channel(self.cog.config["notification_channel_id"])
            if channel:
                current_channel = channel.mention

        embed = discord.Embed(
            title="📢 Set Notification Channel",
            description=f"Current: {current_channel}\n\nTo change, use:\n`/cleanup config notification_channel:#your-channel`",
            color=discord.Color.blue()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class MediaCleanupCog(commands.Cog):
    """Handle automatic cleanup of unwatched media"""

    #: One top-level /cleanup entry instead of five. Discord applies
    #: default_member_permissions at the top level only, and every subcommand here
    #: is admin-only, so the gate belongs on the group.
    cleanup = app_commands.Group(
        name="cleanup",
        description="Manage automatic cleanup of unwatched media",
        default_permissions=discord.Permissions(administrator=True),
        guild_only=True,
    )
    exempt = app_commands.Group(
        name="exempt",
        description="Media exempt from automatic cleanup",
        parent=cleanup,
    )

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        self.config = DEFAULT_CONFIG.copy()
        self.tracking_data = {}
        self._data_loaded = False
        self.daily_cleanup_check.start()

        # Register persistent view
        self.bot.add_view(CleanupControlPanel(self))

    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        self.daily_cleanup_check.cancel()
        # Note: Can't await in cog_unload, data will be saved after each operation

    async def load_data(self):
        """Load config and tracking data from database"""
        if self._data_loaded:
            return
        try:
            # Load config
            saved_config = await kv_get(CLEANUP_NAMESPACE, "config", {})
            self.config = {**DEFAULT_CONFIG, **saved_config}

            # Load tracking data
            self.tracking_data = await kv_get(CLEANUP_NAMESPACE, "tracking", {})

            exempt_items = self.config.get("exempt_items", {})
            if isinstance(exempt_items, list):
                self.config["exempt_items"] = {str(item): {"title": "Unknown", "type": "unknown"} for item in exempt_items}
            elif not isinstance(exempt_items, dict):
                self.config["exempt_items"] = {}

            self._data_loaded = True
            logger.info("Loaded media cleanup data from database")
        except Exception as e:
            logger.error(f"Error loading cleanup data: {e}")

    async def save_config(self):
        """Save cleanup configuration"""
        try:
            await kv_set(CLEANUP_NAMESPACE, "config", self.config)
        except Exception as e:
            logger.error(f"Error saving cleanup config: {e}")

    async def save_tracking_data(self):
        """Save tracking data"""
        try:
            await kv_set(CLEANUP_NAMESPACE, "tracking", self.tracking_data)
        except Exception as e:
            logger.error(f"Error saving tracking data: {e}")


    def _latest_requests_by_media(self, requests_data: Dict[str, Any]) -> Dict[tuple, Dict[str, Any]]:
        latest = {}
        for request in requests_data.values():
            media = request.get("media", {})
            tmdb_id = media.get("id")
            media_type = media.get("media_type") or ("movie" if media.get("title") else "tv")
            timestamp = self._parse_request_timestamp(request.get("timestamp"))
            if not tmdb_id or not timestamp:
                continue
            key = (media_type, int(tmdb_id))
            if key not in latest or timestamp > latest[key]["timestamp"]:
                latest[key] = {
                    "timestamp": timestamp,
                    "media": media,
                    "seasons": request.get("seasons"),
                    "monitor": request.get("monitor", False),
                }
        return latest

    def _prune_media_tracking_cache(self, latest_requests: Dict[tuple, Dict[str, Any]], cutoff: datetime) -> int:
        from core.media_tracking import get_media_tracker

        tracker = get_media_tracker()
        to_remove = []
        for key, tracked in tracker.tracked_media.items():
            request_ts = self._parse_request_timestamp(getattr(tracked, "request_timestamp", None))
            if not request_ts or request_ts >= cutoff:
                continue

            latest_for_media = latest_requests.get((tracked.media_type, tracked.tmdb_id))
            if latest_for_media and latest_for_media["timestamp"] >= cutoff:
                continue
            to_remove.append(key)

        for key in to_remove:
            del tracker.tracked_media[key]

        if to_remove:
            tracker.save_tracking_data()
            logger.info(f"Pruned {len(to_remove)} stale media tracking entries")
        return len(to_remove)

    # _prune_new_media_tracking_cache was removed here.
    #
    # It read config/new_media_tracking.json, which has contained "{}" since that
    # store moved into the database - so it returned 0 every time and its figure in
    # the daily summary was always 0 regardless of reality.
    #
    # It is not reinstated against the database because new_media_added owns that
    # namespace and keeps an in-memory copy of it. A second writer deleting rows
    # behind its back would be resurrected by its next save. Its cleanup_old_batches
    # loop now deletes properly, so the store has exactly one owner.
    #
    # One gap is left, deliberately: batches with is_monitored set are never pruned,
    # because that flag means a requester is still waiting and dropping the batch
    # would lose their notification. Nothing re-checks whether the request behind
    # one is still outstanding, so they accumulate (8 of 56 at the time of writing).

    async def enforce_request_monitor_cleanup(self) -> Dict[str, int]:
        # One query, not a 661 KB parse. Same shape as the file it replaces, so
        # _latest_requests_by_media is unchanged - and so is every decision it
        # drives about Sonarr/Radarr monitoring.
        requests_data = await all_requests()
        latest_requests = self._latest_requests_by_media(requests_data)
        cutoff = datetime.now(timezone.utc) - timedelta(days=REQUEST_EXPIRY_DAYS)
        summary = {
            "tv_unmonitored": 0,
            "tv_reenabled": 0,
            "movie_unmonitored": 0,
            "movie_reenabled": 0,
            "media_tracking_pruned": 0,
        }

        try:
            sonarr_by_tmdb = {}
            if self.services.config.sonarr_url and self.services.config.sonarr_token:
                sonarr_by_tmdb = {series.get("tmdbId"): series for series in await self.services.sonarr.series()}

            for (media_type, tmdb_id), request in latest_requests.items():
                if media_type != "tv":
                    continue
                series = sonarr_by_tmdb.get(tmdb_id)
                if not series:
                    continue

                requested_seasons = request.get("seasons") if isinstance(request.get("seasons"), list) else None
                is_active = request["timestamp"] >= cutoff
                if is_active:
                    if not series.get("monitored", False):
                        await self.services.sonarr.set_series_monitored(series["id"], True)
                        summary["tv_reenabled"] += 1
                    await self.services.sonarr.set_episodes_monitored(series["id"], requested_seasons, True)
                else:
                    if series.get("monitored", False):
                        await self.services.sonarr.set_series_monitored(series["id"], False)
                        summary["tv_unmonitored"] += 1
                    await self.services.sonarr.set_episodes_monitored(series["id"], None, False)
        except Exception as e:
            logger.error(f"Error enforcing Sonarr request monitoring cleanup: {e}", exc_info=True)

        try:
            radarr_by_tmdb = {}
            if self.services.config.radarr_url and self.services.config.radarr_token:
                radarr_by_tmdb = {movie.get("tmdbId"): movie for movie in await self.services.radarr.movies()}

            for (media_type, tmdb_id), request in latest_requests.items():
                if media_type != "movie":
                    continue
                movie = radarr_by_tmdb.get(tmdb_id)
                if not movie:
                    continue

                is_active = request["timestamp"] >= cutoff
                if is_active and not movie.get("monitored", False):
                    await self.services.radarr.set_movie_monitored(movie["id"], True)
                    summary["movie_reenabled"] += 1
                elif not is_active and movie.get("monitored", False):
                    await self.services.radarr.set_movie_monitored(movie["id"], False)
                    summary["movie_unmonitored"] += 1
        except Exception as e:
            logger.error(f"Error enforcing Radarr request monitoring cleanup: {e}", exc_info=True)

        summary["media_tracking_pruned"] = self._prune_media_tracking_cache(latest_requests, cutoff)
        return summary

    @tasks.loop(hours=1)
    async def daily_cleanup_check(self):
        """Check for media to clean up once a day.

        The loop wakes hourly but only runs a day after the last check, which is
        remembered: restarting Plexbie used to run it (and send its warnings)
        every time. A new install has its first check a day after it starts.
        """
        last = await kv_get(CLEANUP_NAMESPACE, "last_check")
        now = datetime.now(timezone.utc)
        if last is None:
            await kv_set(CLEANUP_NAMESPACE, "last_check", now.isoformat())
            return
        try:
            if now - datetime.fromisoformat(last) < CHECK_EVERY:
                return
        except (TypeError, ValueError):
            pass
        await kv_set(CLEANUP_NAMESPACE, "last_check", now.isoformat())

        # Ensure data is loaded
        await self.load_data()

        if not self.config["enabled"]:
            logger.info("Media cleanup is disabled")
            return

        if not self.services.plex_server:
            logger.warning("Plex server not available for cleanup check")
            return

        try:
            logger.info("Starting daily media cleanup check...")
            monitor_summary = await self.enforce_request_monitor_cleanup()
            logger.info(f"Request monitor cleanup summary: {monitor_summary}")
            try:
                await self.reconcile_seerr()
            except Exception as e:
                logger.warning(f"Couldn't tidy Seerr's removed titles: {e}")

            # The whole traversal runs in one worker thread. It is one request to
            # list sections, one per section to list items, and one more per show
            # to list its episodes - hundreds of blocking round-trips on a real
            # library. Inline, that froze the event loop (and the Discord
            # heartbeat) for minutes and triggered gateway reconnects.
            items_to_notify, items_to_delete = await run_blocking(
                self._scan_libraries_for_cleanup
            )

            # Send notifications
            if items_to_notify:
                await self.send_cleanup_notification(items_to_notify, "warning")

            # Delete items
            if items_to_delete:
                deleted = await self.delete_media_items(items_to_delete)
                if deleted:
                    await self.send_cleanup_notification(deleted, "deleted")

            logger.info(f"Cleanup check complete. Notified: {len(items_to_notify)}, Deleted: {len(items_to_delete)}")

        except Exception as e:
            logger.error(f"Error in daily cleanup check: {e}", exc_info=True)

    @daily_cleanup_check.before_loop
    async def before_daily_cleanup_check(self):
        """Wait for bot to be ready before starting cleanup checks"""
        await self.bot.wait_until_ready()


    def _get_item_tmdb_id(self, item) -> Optional[int]:
        """Best-effort TMDB extraction from Plex item guid metadata."""
        guid_candidates = []

        primary_guid = getattr(item, "guid", None)
        if primary_guid:
            guid_candidates.append(str(primary_guid))

        for guid_obj in getattr(item, "guids", []) or []:
            guid_value = getattr(guid_obj, "id", None) or getattr(guid_obj, "tag", None)
            if guid_value:
                guid_candidates.append(str(guid_value))
            else:
                guid_candidates.append(str(guid_obj))

        for guid in guid_candidates:
            tmdb_id = guid_number(guid, "tmdb")
            if tmdb_id:
                return tmdb_id

        return None

    def _parse_request_timestamp(self, value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None

        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            logger.debug(f"Could not parse media request timestamp: {value}")
            return None

    def _get_recent_request_timestamp(self, item) -> Optional[datetime]:
        """Return the newest request timestamp matching this Plex item, if any."""
        try:
            from core.media_tracking import get_media_tracker

            tracker = get_media_tracker()
            if not tracker or not getattr(tracker, "tracked_media", None):
                return None

            item_tmdb_id = self._get_item_tmdb_id(item)
            item_title = getattr(item, "title", None)
            item_media_type = "tv" if getattr(item, "type", None) == "show" else getattr(item, "type", None)
            candidates = []

            for tracked in tracker.tracked_media.values():
                matches_tmdb = item_tmdb_id is not None and tracked.tmdb_id == item_tmdb_id
                matches_title = item_tmdb_id is None and tracked.title == item_title and tracked.media_type == item_media_type
                if not (matches_tmdb or matches_title):
                    continue

                request_ts = self._parse_request_timestamp(getattr(tracked, "request_timestamp", None))
                if request_ts:
                    candidates.append(request_ts)

            if not candidates:
                return None

            return max(candidates)
        except Exception as e:
            logger.debug(f"Could not look up recent media request timestamp for {getattr(item, 'title', '<unknown>')}: {e}")
            return None

    def _scan_libraries_for_cleanup(self):
        """Blocking: walk every eligible library and classify each item.

        Runs in a worker thread. Returns (items_to_notify, items_to_delete).

        Kept synchronous end to end so that no plexapi call - including the lazy
        per-show season/episode requests inside check_item_for_cleanup - can end
        up back on the event loop.
        """
        items_to_notify = []
        items_to_delete = []
        # What anyone on the server watched lately. Without it nothing is deleted: Plex's
        # own "last viewed" on an item is only the owner's, so a show the household is
        # watching would look untouched.
        try:
            views = self._everyones_views()
        except Exception as e:
            logger.warning(f"Media cleanup: couldn't read Plex's watch history ({e}); deleting nothing today")
            return [], []

        for library in self.services.plex_server.library.sections():
            # Skip excluded libraries
            if library.title in self.config["exclude_libraries"]:
                logger.info(f"Skipping excluded library: {library.title}")
                continue

            # Only process movie and show libraries
            if library.type not in ["movie", "show"]:
                continue

            logger.info(f"Checking library: {library.title}")

            for item in library.all():
                result = self.check_item_for_cleanup(item, views)
                if result:
                    if result["action"] == "notify":
                        items_to_notify.append(result)
                    elif result["action"] == "delete":
                        items_to_delete.append(result)

        return items_to_notify, items_to_delete

    def _everyones_views(self) -> Dict[str, datetime]:
        """Blocking: when anyone last watched each film / any episode of each show
        (portal/cleanup.everyones_views: the server's history has every account)."""
        from portal.cleanup import everyones_views
        return everyones_views(self.services.plex_server, int(self.config["inactivity_days"]))

    def check_item_for_cleanup(self, item, views: Optional[Dict[str, datetime]] = None) -> Optional[Dict]:
        """Check if an item should be cleaned up.

        Synchronous on purpose: it performs a blocking plexapi call per show
        (item.episodes()) and awaits nothing. Callers must invoke it
        from inside a worker thread - see _scan_libraries_for_cleanup.
        """
        try:
            # Get the item's rating key for tracking
            rating_key = str(item.ratingKey)

            exempt_items = self.config.get("exempt_items", {})
            if rating_key in exempt_items:
                logger.info(f"Skipping exempt media item: {item.title} ({item.type})")
                return None

            # Check last viewed time: by anyone (views), and the owner's own as before
            last_viewed = (views or {}).get(rating_key)
            newest = None

            # For TV shows, check all episodes: the whole show is one thing. Anyone
            # watching any episode keeps all of it, and a season that just arrived
            # counts as fresh, so it isn't deleted before anyone could watch it.
            if item.type == "show":
                # One request per show, not one per season. Show.episodes() fetches
                # /library/metadata/<key>/allLeaves in a single call; seasons()
                # followed by episodes() on each season cost 1 + N requests. Across
                # a few hundred shows that is the difference between a couple of
                # hundred round-trips and a couple of thousand, and this runs inside
                # the daily library walk.
                for episode in item.episodes():
                    if episode.lastViewedAt:
                        seen = _aware(episode.lastViewedAt)
                        if not last_viewed or seen > last_viewed:
                            last_viewed = seen
                    came = getattr(episode, "addedAt", None)
                    if came and (not newest or _aware(came) > newest):
                        newest = _aware(came)

            # For movies, check directly
            elif item.type == "movie" and item.lastViewedAt:
                seen = _aware(item.lastViewedAt)
                if not last_viewed or seen > last_viewed:
                    last_viewed = seen

            # Never watched, or new episodes since: the newest addition counts
            added = newest or (_aware(item.addedAt) if getattr(item, "addedAt", None) else None)
            if added and (not last_viewed or added > last_viewed):
                last_viewed = added

            recent_request_timestamp = self._get_recent_request_timestamp(item)
            if recent_request_timestamp and (not last_viewed or recent_request_timestamp > last_viewed):
                logger.info(
                    f"Using recent request timestamp for cleanup grace on {item.title}: "
                    f"{recent_request_timestamp.isoformat()}"
                )
                last_viewed = recent_request_timestamp

            # Calculate days since last activity
            now = datetime.now(timezone.utc)
            days_inactive = (now - last_viewed).days

            # Determine action
            inactivity_threshold = self.config["inactivity_days"]
            notify_threshold = inactivity_threshold - self.config["notify_days_before"]

            if days_inactive >= inactivity_threshold:
                # Mark for deletion
                return {
                    "action": "delete",
                    "item": item,
                    "rating_key": rating_key,
                    "title": item.title,
                    "type": item.type,
                    "last_viewed": last_viewed.isoformat(),
                    "days_inactive": days_inactive,
                }
            elif days_inactive >= notify_threshold:
                # Notify that it will be deleted soon
                days_until_deletion = inactivity_threshold - days_inactive
                return {
                    "action": "notify",
                    "item": item,
                    "rating_key": rating_key,
                    "title": item.title,
                    "type": item.type,
                    "last_viewed": last_viewed.isoformat(),
                    "days_inactive": days_inactive,
                    "days_until_deletion": days_until_deletion,
                }

            return None

        except Exception as e:
            logger.error(f"Error checking item {item.title}: {e}")
            return None

    async def delete_media_items(self, items: List[Dict]) -> List[Dict]:
        """Delete media items from Sonarr/Radarr and Plex"""
        deleted = []

        for item_data in items:
            try:
                item = item_data["item"]
                media_type = item_data["type"]

                if self.config["dry_run"]:
                    logger.info(f"[DRY RUN] Would delete: {item.title} ({media_type})")
                    deleted.append(item_data)
                else:
                    logger.info(f"Deleting: {item.title} ({media_type})")

                    # Delete from Sonarr/Radarr first (this deletes the actual files)
                    ids = _ids_of(item)
                    removed = None
                    if media_type == "movie":
                        removed = await self._delete_from_radarr(item, ids)
                        if not removed:
                            logger.warning(f"Could not delete {item.title} from Radarr, trying Plex...")
                    elif media_type == "show":
                        removed = await self._delete_from_sonarr(item, ids)
                        if not removed:
                            logger.warning(f"Could not delete {item.title} from Sonarr, trying Plex...")
                    # ...and from Seerr, or it keeps the old request and refuses the next one
                    # ("no seasons available to request": The Boys, after three removals).
                    tmdb = ids.get("tmdb") or (removed or {}).get("tmdbId")
                    if tmdb:
                        await self._clear_from_seerr("movie" if media_type == "movie" else "tv", int(tmdb), item.title)

                    # Also remove from Plex library (Sonarr/Radarr deletion should trigger this, but be safe)
                    try:
                        await run_blocking(item.delete)
                    except Exception as e:
                        logger.debug(f"Could not delete from Plex (may already be gone): {e}")

                    deleted.append(item_data)

                    # Update tracking
                    self.tracking_data[item_data["rating_key"]] = {
                        "deleted_at": datetime.now(timezone.utc).isoformat(),
                        "title": item.title,
                        "type": item.type,
                        "tmdb": tmdb,
                    }

            except Exception as e:
                logger.error(f"Error deleting {item_data['title']}: {e}")

        await self.save_tracking_data()
        return deleted

    async def _delete_from_radarr(self, movie, ids: Optional[Dict[str, str]] = None) -> Optional[dict]:
        """Delete a movie from Radarr and from disk. Returns Radarr's record of it."""
        return await self._delete_from_arr(self.services.radarr, "movie", movie,
                                           {"deleteFiles": "true", "addImportExclusion": "false"}, ids)

    async def _delete_from_sonarr(self, show, ids: Optional[Dict[str, str]] = None) -> Optional[dict]:
        """Delete a TV show from Sonarr and from disk. Returns Sonarr's record of it."""
        return await self._delete_from_arr(self.services.sonarr, "series", show,
                                           {"deleteFiles": "true", "addImportListExclusion": "false"}, ids)

    async def _delete_from_arr(self, arr, path: str, item, params: Dict[str, str],
                               ids: Optional[Dict[str, str]] = None) -> Optional[dict]:
        """Find a Plex item in Sonarr/Radarr, by its TMDB/TheTVDB/IMDb id (Plex's guids),
        else by title and year, and delete it with its files. Returns what was deleted."""
        if not arr.configured:
            logger.warning(f"{arr.name} not configured")
            return None
        ids = ids if ids is not None else _ids_of(item)
        try:
            rows = await arr.get(path) or []

            def same(m: dict) -> bool:
                return any(str(m.get(field) or "") == ids[k] for k, field in
                           (("tmdb", "tmdbId"), ("tvdb", "tvdbId"), ("imdb", "imdbId")) if ids.get(k))
            year = getattr(item, "year", None)
            match = next((m for m in rows if ids and same(m)), None) or next(
                (m for m in rows if (m.get("title") or "").lower() == item.title.lower()
                 and (not year or not m.get("year") or m["year"] == year)), None)
            if not match:
                logger.warning(f"Could not find {item.title} in {arr.name}")
                return None
            await arr.delete(f"{path}/{match['id']}", **params)
            logger.info(f"Deleted {item.title} from {arr.name} and disk")
            return match
        except Exception as e:
            logger.error(f"Error deleting {item.title} from {arr.name}: {e}")
            return None

    async def _clear_from_seerr(self, kind: str, tmdb_id: int, title: str) -> bool:
        """Seerr's "Clear data" for it: its media record and requests go, so it can be
        asked for again. True when Seerr no longer has it."""
        seerr = self.services.seerr
        if not seerr.configured:
            return False
        try:
            info = (await seerr.get(f"{kind}/{int(tmdb_id)}") or {}).get("mediaInfo") or {}
            if not info.get("id"):
                return True
            await seerr.delete(f"media/{int(info['id'])}")
            logger.info(f"Cleared {title} from Seerr")
            return True
        except Exception as e:
            logger.warning(f"Could not clear {title} from Seerr: {e}")
            return False

    async def reconcile_seerr(self) -> int:
        """Seerr records of things that are gone: Seerr marks media "deleted" (status 7)
        once Plex no longer has it, but keeps the old request, and then refuses new ones.
        Each of those Sonarr/Radarr no longer has is cleared, as a removal now does
        itself. Returns how many were cleared."""
        seerr = self.services.seerr
        if not seerr.configured or self.config.get("dry_run"):
            return 0
        gone, skip = [], 0
        while True:
            page = await seerr.get("media", take=100, skip=skip) or {}
            results = page.get("results") or []
            gone += [m for m in results if m.get("status") == SEERR_DELETED]
            skip += len(results)
            if not results or skip >= ((page.get("pageInfo") or {}).get("results") or 0):
                break
        if not gone:
            return 0
        have = {"movie": set(), "tv": set()}
        if self.services.radarr.configured:
            have["movie"] = {str(m.get("tmdbId")) for m in await self.services.radarr.movies()}
        if self.services.sonarr.configured:
            for s in await self.services.sonarr.series():
                have["tv"] |= {f"tvdb:{s.get('tvdbId')}", f"tmdb:{s.get('tmdbId')}"}
        cleared = 0
        for m in gone:
            kind = m.get("mediaType")
            still = (str(m.get("tmdbId")) in have["movie"]) if kind == "movie" else bool(
                {f"tvdb:{m.get('tvdbId')}", f"tmdb:{m.get('tmdbId')}"} & have["tv"])
            if still or kind not in ("movie", "tv"):
                continue
            try:
                await seerr.delete(f"media/{int(m['id'])}")
                cleared += 1
            except Exception as e:
                logger.info(f"Could not clear Seerr media {m.get('id')}: {e}")
        if cleared:
            logger.info(f"Cleared {cleared} removed title(s) from Seerr, so they can be requested again")
        return cleared

    async def send_cleanup_notification(self, items: List[Dict], notification_type: str):
        """Send notification about cleanup actions"""
        # Its own channel if one was picked in the cleanup settings, else the admin channel.
        channel_id = self.config.get("notification_channel_id") or self.services.config.admin_channel_id
        if not channel_id:
            logger.info("Cleanup report skipped: no admin channel set")
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.error(f"Could not find notification channel {channel_id}")
            return

        try:
            if notification_type == "warning":
                embed = discord.Embed(
                    title="⚠️ Media Cleanup Warning",
                    description=f"The following {len(items)} item(s) will be removed soon due to inactivity:",
                    color=discord.Color.orange(),
                )

                for item in items[:10]:  # Show max 10
                    days = item["days_until_deletion"]
                    embed.add_field(
                        name=f"{'🎬' if item['type'] == 'movie' else '📺'} {item['title']}",
                        value=f"Will be removed in **{days} day(s)** if not watched",
                        inline=False
                    )

                if len(items) > 10:
                    embed.add_field(
                        name="And more...",
                        value=f"+ {len(items) - 10} more items",
                        inline=False
                    )

            elif notification_type == "deleted":
                dry_run_text = " (DRY RUN)" if self.config["dry_run"] else ""
                embed = discord.Embed(
                    title=f"🗑️ Media Cleanup Complete{dry_run_text}",
                    description=f"Removed {len(items)} item(s) due to {self.config['inactivity_days']} days of inactivity:",
                    color=discord.Color.red() if not self.config["dry_run"] else discord.Color.blue(),
                )

                for item in items[:10]:  # Show max 10
                    embed.add_field(
                        name=f"{'🎬' if item['type'] == 'movie' else '📺'} {item['title']}",
                        value=f"Inactive for {item['days_inactive']} days",
                        inline=False
                    )

                if len(items) > 10:
                    embed.add_field(
                        name="And more...",
                        value=f"+ {len(items) - 10} more items",
                        inline=False
                    )

            embed.set_footer(text="Use /cleanup config to adjust settings")
            await channel.send(embed=embed)

        except Exception as e:
            logger.error(f"Error sending cleanup notification: {e}")

    def _format_media_label(self, item_type: str, title: str, year: Optional[int] = None) -> str:
        """Format a human-friendly media label"""
        prefix = "[Movie]" if item_type == "movie" else "[Show]"
        if year:
            return f"{prefix} {title} ({year})"
        return f"{prefix} {title}"

    async def _find_media_matches(self, title_query: str, media_type: Optional[str] = None) -> List[Dict]:
        """Find candidate Plex media items matching a title query."""
        return await run_blocking(self._find_media_matches_blocking, title_query, media_type)

    def _find_media_matches_blocking(self, title_query: str, media_type: Optional[str] = None) -> List[Dict]:
        """Blocking: search every eligible library for a title.

        Runs in a worker thread. One request per library section to search, and it
        already reduces everything to plain dicts, so no lazy plexapi object
        escapes back to the event loop.
        """
        matches = []
        normalized_query = title_query.casefold().strip()

        for library in self.services.plex_server.library.sections():
            if library.type not in ["movie", "show"]:
                continue
            if media_type and library.type != media_type:
                continue

            try:
                candidates = library.search(title=title_query)
            except Exception as e:
                logger.error(f"Error searching {library.title} for '{title_query}': {e}")
                continue

            for item in candidates:
                if item.type not in ["movie", "show"]:
                    continue

                item_title = getattr(item, "title", "")
                item_year = getattr(item, "year", None)
                exact = item_title.casefold() == normalized_query
                contains = normalized_query in item_title.casefold()
                score = 0 if exact else 1 if contains else 2

                matches.append({
                    "rating_key": str(item.ratingKey),
                    "title": item_title,
                    "type": item.type,
                    "year": item_year,
                    "added_at": item.addedAt.isoformat() if getattr(item, "addedAt", None) else None,
                    "score": score,
                })

        deduped = {}
        for match in matches:
            deduped[match["rating_key"]] = match

        return sorted(
            deduped.values(),
            key=lambda item: (item["score"], item["title"].casefold(), item.get("year") or 0, item["rating_key"]),
        )

    async def _resolve_single_media_match(self, title_query: str, media_type: Optional[str] = None) -> tuple[Optional[Dict], List[Dict]]:
        """Resolve a query to one media item, preferring exact title matches"""
        matches = await self._find_media_matches(title_query, media_type)
        if not matches:
            return None, []

        exact_matches = [m for m in matches if m["title"].casefold() == title_query.casefold().strip()]
        if len(exact_matches) == 1:
            return exact_matches[0], matches
        if len(matches) == 1:
            return matches[0], matches
        return None, matches

    async def scan_now(self) -> Optional[Dict[str, Any]]:
        """One scan on demand, as the daily loop does it. None if Plex is unavailable.

        Shared by the Discord panel's "Run Scan Now" and the website's Manage page.
        """
        await self.load_data()
        if not self.services.plex_server:
            return None

        monitor_summary = await self.enforce_request_monitor_cleanup()

        # Same traversal as the daily loop, off the event loop. This path is
        # reached from a click, so blocking here would freeze the bot for
        # every other user while one admin's scan ran.
        items_to_notify, items_to_delete = await run_blocking(
            self._scan_libraries_for_cleanup
        )

        if items_to_notify:
            await self.send_cleanup_notification(items_to_notify, "warning")

        deleted_items = []
        if items_to_delete:
            deleted_items = await self.delete_media_items(items_to_delete)
            if deleted_items:
                await self.send_cleanup_notification(deleted_items, "deleted")

        logger.info(f"Cleanup scan complete. Notified: {len(items_to_notify)}, Deleted: {len(deleted_items)}")
        return {"notify": items_to_notify, "to_delete": items_to_delete, "deleted": deleted_items,
                "monitor": monitor_summary, "dry_run": bool(self.config["dry_run"])}

    async def run_cleanup_scan(self, interaction: discord.Interaction):
        """Run the actual cleanup scan logic"""
        result = await self.scan_now()
        if result is None:
            await interaction.followup.send(
                "❌ Plex server not available",
                ephemeral=True
            )
            return
        items_to_notify, items_to_delete = result["notify"], result["to_delete"]
        deleted_items, monitor_summary = result["deleted"], result["monitor"]

        # Send summary to user
        summary_embed = discord.Embed(
            title="✅ Cleanup Scan Complete",
            color=discord.Color.green()
        )

        summary_embed.add_field(
            name="⚠️ Items Needing Attention",
            value=f"{len(items_to_notify)} item(s) will be deleted soon",
            inline=True
        )

        action_text = "Would be deleted" if self.config["dry_run"] else "Deleted"
        summary_embed.add_field(
            name=f"🗑️ Items {action_text}",
            value=f"{len(deleted_items)} item(s)",
            inline=True
        )

        summary_embed.add_field(
            name="🛠️ Request Monitor Sync",
            value=(
                f"TV off: {monitor_summary['tv_unmonitored']} | TV on: {monitor_summary['tv_reenabled']}\n"
                f"Movies off: {monitor_summary['movie_unmonitored']} | Movies on: {monitor_summary['movie_reenabled']}\n"
                f"Tracking pruned: {monitor_summary['media_tracking_pruned']}"
            ),
            inline=False
        )

        if items_to_delete and self.config["dry_run"]:
            summary_embed.set_footer(text="Dry run mode - no files were actually deleted.")

        await interaction.followup.send(embed=summary_embed, ephemeral=True)

    @exempt.command(name="add", description="Exempt a movie or show from automatic cleanup")
    @app_commands.describe(
        title="Movie or show title to exempt",
        media_type="Limit the search to movies or shows"
    )
    @app_commands.choices(media_type=[
        app_commands.Choice(name="Movie", value="movie"),
        app_commands.Choice(name="Show", value="show"),
    ])
    async def cleanup_exempt_add(
        self,
        interaction: discord.Interaction,
        title: str,
        media_type: Optional[app_commands.Choice[str]] = None,
    ):
        """Add a specific media item to the cleanup exemption list"""
        if not await require_admin(interaction):
            return

        await self.load_data()

        if not self.services.plex_server:
            await interaction.response.send_message(
                "Plex server not available",
                ephemeral=True
            )
            return

        selected_type = media_type.value if media_type else None
        match, matches = await self._resolve_single_media_match(title, selected_type)
        if not match:
            if not matches:
                await interaction.response.send_message(
                    f"No matching media found for `{title}`.",
                    ephemeral=True
                )
                return

            preview = "\n".join(
                f"- {self._format_media_label(item['type'], item['title'], item.get('year'))}"
                for item in matches[:10]
            )
            await interaction.response.send_message(
                f"Found multiple matches for `{title}`. Please refine the title or specify the media type.\n\n{preview}",
                ephemeral=True
            )
            return

        exempt_items = self.config.setdefault("exempt_items", {})
        exempt_items[match["rating_key"]] = {
            "title": match["title"],
            "type": match["type"],
            "year": match.get("year"),
            "added_at": match.get("added_at"),
            "exempted_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.save_config()

        embed = create_info_embed(
            "Configuration Updated",
            f"Exempted: {self._format_media_label(match['type'], match['title'], match.get('year'))}\n\nThis item will be skipped by automatic cleanup until you remove the exemption."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"{interaction.user} exempted media from cleanup: {match['title']} ({match['type']}) [{match['rating_key']}]")

    @exempt.command(name="remove", description="Remove a cleanup exemption from a movie or show")
    @app_commands.describe(title="Movie or show title to remove from the exemption list")
    async def cleanup_exempt_remove(self, interaction: discord.Interaction, title: str):
        """Remove a specific media item from the cleanup exemption list"""
        if not await require_admin(interaction):
            return

        await self.load_data()
        exempt_items = self.config.setdefault("exempt_items", {})
        query = title.casefold().strip()
        matches = [
            (rating_key, data)
            for rating_key, data in exempt_items.items()
            if query in data.get("title", "").casefold()
        ]

        if not matches:
            await interaction.response.send_message(
                f"No exempt media found matching `{title}`.",
                ephemeral=True
            )
            return

        exact_matches = [
            (rating_key, data)
            for rating_key, data in matches
            if data.get("title", "").casefold() == query
        ]
        if len(exact_matches) == 1:
            matches = exact_matches

        if len(matches) > 1:
            preview = "\n".join(
                f"- {self._format_media_label(data.get('type', 'show'), data.get('title', 'Unknown'), data.get('year'))}"
                for _, data in matches[:10]
            )
            await interaction.response.send_message(
                f"Multiple exempt items matched `{title}`. Please use a more specific title.\n\n{preview}",
                ephemeral=True
            )
            return

        rating_key, data = matches[0]
        exempt_items.pop(rating_key, None)
        await self.save_config()

        embed = create_info_embed(
            "Configuration Updated",
            f"Removed exemption: {self._format_media_label(data.get('type', 'show'), data.get('title', 'Unknown'), data.get('year'))}\n\nThis item will be evaluated by automatic cleanup again."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"{interaction.user} removed cleanup exemption: {data.get('title', 'Unknown')} ({data.get('type', 'unknown')}) [{rating_key}]")

    @exempt.command(name="list", description="List media currently exempt from automatic cleanup")
    async def cleanup_exempt_list(self, interaction: discord.Interaction):
        """List the current cleanup exemption entries"""
        if not await require_admin(interaction):
            return

        await self.load_data()
        exempt_items = self.config.get("exempt_items", {})

        if not exempt_items:
            embed = create_info_embed(
                "No Exempt Media",
                "No specific movies or shows are currently exempt from automatic cleanup."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        entries = sorted(
            exempt_items.values(),
            key=lambda item: (item.get("type", ""), item.get("title", "").casefold(), item.get("year") or 0),
        )

        embed = discord.Embed(
            title="Cleanup Exemptions",
            description=f"{len(entries)} item(s) are exempt from automatic cleanup.",
            color=discord.Color.green(),
        )

        lines = [
            f"- {self._format_media_label(item.get('type', 'show'), item.get('title', 'Unknown'), item.get('year'))}"
            for item in entries[:20]
        ]
        embed.add_field(name="Exempt Media", value=truncate_field("\n".join(lines)), inline=False)
        if len(entries) > 20:
            embed.set_footer(text=f"Showing first 20 of {len(entries)} exempt items")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @cleanup.command(name="panel", description="Media cleanup control panel")
    async def cleanup_panel(self, interaction: discord.Interaction):
        """Show the interactive cleanup control panel"""
        if not await require_admin(interaction):
            return

        # Ensure data is loaded
        await self.load_data()

        # Create control panel
        view = CleanupControlPanel(self)

        status = "✅ Enabled" if self.config["enabled"] else "❌ Disabled"
        mode = "🔵 Dry Run" if self.config["dry_run"] else "🔴 Live Mode"

        embed = discord.Embed(
            title="🗑️ Media Cleanup Control Panel",
            description="Manage automatic cleanup of unwatched media",
            color=discord.Color.blue() if self.config["dry_run"] else discord.Color.red()
        )

        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Mode", value=mode, inline=True)
        embed.add_field(
            name="Inactivity Threshold",
            value=f"{self.config['inactivity_days']} days",
            inline=True
        )

        embed.set_footer(text="Use the buttons below to control cleanup")

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @cleanup.command(name="config", description="Configure cleanup settings")
    @app_commands.describe(
        inactivity_days="Number of days before media is considered inactive",
        notification_channel="Channel to send cleanup notifications"
    )
    async def cleanup_config(
        self,
        interaction: discord.Interaction,
        inactivity_days: Optional[int] = None,
        notification_channel: Optional[discord.TextChannel] = None
    ):
        """Configure cleanup settings (for advanced options)"""
        if not await require_admin(interaction):
            return

        changes = []

        if inactivity_days is not None:
            if inactivity_days < 30:
                await interaction.response.send_message(
                    "Inactivity days must be at least 30 days for safety.",
                    ephemeral=True
                )
                return
            self.config["inactivity_days"] = inactivity_days
            changes.append(f"Inactivity Days: {inactivity_days}")

        if notification_channel is not None:
            self.config["notification_channel_id"] = notification_channel.id
            changes.append(f"Notification Channel: {notification_channel.mention}")

        if changes:
            await self.save_config()
            embed = create_info_embed(
                "✅ Configuration Updated",
                "\n".join(f"• {change}" for change in changes)
            )
        else:
            embed = create_info_embed(
                "ℹ️ No Changes Made",
                "Use the command parameters to update settings, or use `/cleanup` for the interactive panel."
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info(f"{interaction.user} updated cleanup config: {changes}")

