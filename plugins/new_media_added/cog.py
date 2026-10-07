# path: plugins/new_media_added/cog.py
"""New media added notifications from Plex webhooks"""
import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from collections import defaultdict

import discord
from discord.ext import commands, tasks

from utils.formatting import episode_label
from utils.embeds import truncate_field
from utils.guids import guid_number
from core.admin_mirror import send_user_dm
from core.blocking import run_blocking
from core.logging import get_logger
from core.services import BotServices
from database.kv_store import kv_delete_many, kv_get, kv_get_all, kv_set, kv_set_many
from portal.progress import WAIT_NAMESPACE as WAIT_NS

logger = get_logger(__name__)

# Namespace for tracking episode batches and messages
NEW_MEDIA_NAMESPACE = "new_media_tracking"
#: Where the Recently Added sweep keeps its place (not among the batches above).
SWEEP_NAMESPACE = "new_media_sweep"
#: When each movie was last announced, so a second report of it isn't posted again.
#: 1.0 kept this as an "announced" row among the batches, where loading the batches
#: choked on it (no show_title) and every batch saved after it was lost on restart.
ANNOUNCED_NAMESPACE = "new_media_announced"
ANNOUNCED_KEY = "announced"

#: One announcement per show and season while episodes keep arriving: each new
#: episode edits it (edits notify nobody) as long as it comes within this long of
#: the previous one. A season dump that downloads over hours stays one message;
#: a weekly episode a week later gets its own post, because people wait for it.
GROUP_WINDOW = timedelta(hours=24)


#: How often Plex's Recently Added list is checked for anything no webhook announced.
SWEEP_MINUTES = 10
FIRST_SWEEP_LOOKBACK = timedelta(hours=2)
#: On disk (Sonarr/Radarr) this long and still not listed by Plex: tell the admins.
STUCK_AFTER = timedelta(hours=2)
#: Requests older than this aren't watched for it any more.
WATCH_DAYS = 60


def _recent_items(server) -> List[tuple]:
    """Blocking: (rating key, added at) of Plex's recently added movies, seasons and episodes."""
    out = []
    for item in server.library.recentlyAdded():
        added = getattr(item, "addedAt", None)
        if added and getattr(item, "type", "") in ("movie", "season", "episode", "show"):
            out.append((str(item.ratingKey), _utc(added)))
    return out


def _utc(when: datetime) -> datetime:
    """plexapi gives local, zone-less times; the sweep's mark is kept in UTC so a
    container that gains a TZ setting later doesn't shift it by hours."""
    return when.astimezone(timezone.utc)


def _plex_web_url(payload: Dict) -> Optional[str]:
    """The item's page in Plex's web app, from a Plex webhook payload."""
    machine_id = (payload.get("Server") or {}).get("uuid")
    item_key = (payload.get("Metadata") or {}).get("key")
    return f"https://app.plex.tv/desktop/#!/server/{machine_id}/details?key={item_key}" if machine_id and item_key else None


def _overview_and_poster(tmdb_data: Optional[Dict], fallback: Optional[str] = None) -> tuple:
    """(overview, poster URL) for an arrival card, TMDB's when it has them."""
    overview, poster_url = fallback or "No description available.", None
    if tmdb_data:
        overview = tmdb_data.get("overview") or overview
        if tmdb_data.get("poster_path"):
            poster_url = f"https://image.tmdb.org/t/p/w500{tmdb_data['poster_path']}"
    return overview, poster_url


#: How far back "recently added" reaches when Plex reports a whole show or season.
RECENT_EPISODES = timedelta(hours=6)


def _recently_added_episodes(server, rating_key, within: timedelta) -> List[tuple]:
    """Blocking: (season, episode, title) of the item's episodes added in the last `within`."""
    item = server.fetchItem(int(rating_key))
    cutoff = datetime.now() - within          # plexapi's addedAt is local, naive
    return [(int(e.parentIndex or 0), int(e.index or 0), e.title or "")
            for e in item.episodes() if e.addedAt and e.addedAt >= cutoff and e.index is not None]


def _show_key_by_title(server, title: str) -> Optional[str]:
    """Blocking: the rating key of the one Plex show with exactly this title, if there is one."""
    shows = [s for s in server.library.search(title=title, libtype="show") if (s.title or "").lower() == title.lower()]
    return str(shows[0].ratingKey) if len(shows) == 1 else None


class EpisodeBatch:
    """Track a batch of episodes being added for a show/season"""

    def __init__(self, show_title: str, season: int, tmdb_id: Optional[int] = None):
        self.show_title = show_title
        self.season = season
        self.tmdb_id = tmdb_id
        self.episodes: List[int] = []
        self.message_id: Optional[int] = None
        self.channel_id: Optional[int] = None
        self.last_update = datetime.now(timezone.utc)
        self.is_monitored = False
        self.expected_episode_count: Optional[int] = None    # aired episodes in the season
        self.titles: Dict[str, str] = {}                     # episode number -> title
        self.plex_url: Optional[str] = None
        self.tvdb_id: Optional[int] = None
        self.show_key: Optional[str] = None                  # the show's Plex rating key
        self.started = self.last_update                      # when this run of episodes began

    def add_episode(self, episode_num: int):
        """Add an episode to the batch"""
        if episode_num not in self.episodes:
            self.episodes.append(episode_num)
            self.episodes.sort()
            self.last_update = datetime.now(timezone.utc)

    def should_create_new_message(self, now: Optional[datetime] = None) -> bool:
        """Post afresh (and notify the channel) only when there's no message yet or
        the last episode came longer than GROUP_WINDOW ago; otherwise edit."""
        if not self.message_id:
            return True
        return (now or datetime.now(timezone.utc)) - self.last_update > GROUP_WINDOW

    def is_complete(self) -> bool:
        """Every aired episode of the season is on Plex."""
        return bool(self.expected_episode_count) and len(self.episodes) >= self.expected_episode_count

    def to_dict(self) -> Dict:
        """Serialize to dict"""
        return {
            "show_title": self.show_title,
            "season": self.season,
            "tmdb_id": self.tmdb_id,
            "episodes": self.episodes,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "last_update": self.last_update.isoformat(),
            "is_monitored": self.is_monitored,
            "expected_episode_count": self.expected_episode_count,
            "titles": self.titles,
            "plex_url": self.plex_url,
            "tvdb_id": self.tvdb_id,
            "show_key": self.show_key,
            "started": self.started.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "EpisodeBatch":
        """Deserialize from dict"""
        batch = cls(data["show_title"], data["season"], data.get("tmdb_id"))
        batch.episodes = data["episodes"]
        batch.message_id = data.get("message_id")
        batch.channel_id = data.get("channel_id")
        batch.last_update = datetime.fromisoformat(data["last_update"])
        batch.is_monitored = data.get("is_monitored", False)
        batch.expected_episode_count = data.get("expected_episode_count")
        batch.titles = data.get("titles") or {}
        batch.plex_url = data.get("plex_url")
        batch.tvdb_id = data.get("tvdb_id")
        batch.show_key = data.get("show_key")
        batch.started = datetime.fromisoformat(data["started"]) if data.get("started") else batch.last_update
        return batch


ARRIVALS_BUTTON_ID = "plexbie:arrivals_role"
#: A movie announced within this long isn't announced again (Plex and Tautulli can both report it).
MOVIE_REPEAT = timedelta(days=7)


class ArrivalsRoleView(discord.ui.View):
    """"Ping me for new arrivals": adds or removes ARRIVALS_ROLE_ID for whoever presses it.

    Persistent (registered on load, fixed custom_id), so the button keeps working
    after restarts. The setup page posts it in the announcements channel.
    """

    def __init__(self, services=None):
        super().__init__(timeout=None)
        self.services = services

    @discord.ui.button(label="Ping me for new arrivals", emoji="🔔", style=discord.ButtonStyle.secondary,
                       custom_id=ARRIVALS_BUTTON_ID)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_id = self.services.config.arrivals_role_id if self.services else None
        role = interaction.guild.get_role(int(role_id)) if (interaction.guild and role_id) else None
        if role is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("New-arrival pings aren't set up on this server.", ephemeral=True)
            return
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role, reason="Opted out of new-arrival pings")
                text = "🔕 You won't be pinged for new arrivals any more. Press again to turn it back on."
            else:
                await interaction.user.add_roles(role, reason="Opted in to new-arrival pings")
                text = "🔔 You'll be pinged once when something new lands (not for every episode)."
        except discord.Forbidden:
            text = "Plexbie isn't allowed to hand out that role. An admin needs to move Plexbie's role above it."
        await interaction.response.send_message(text, ephemeral=True)


class NewMediaAddedCog(commands.Cog):
    """Handle new media webhooks from Plex"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        self.active_batches: Dict[str, EpisodeBatch] = {}
        self._data_loaded = False
        # One season's episodes are handled one report at a time: Plex sends several
        # in seconds, and each must see the message the first one posted.
        self._batch_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._announce_lock = asyncio.Lock()
        self._sweep_lock = asyncio.Lock()
        self._progress = None
        self._show_ids_cache: Dict[str, tuple] = {}
        self._grabs_checked: set = set()       # Radarr downloads already checked (check_movie_grabs)

    async def cog_load(self):
        self.bot.add_view(ArrivalsRoleView(self.services))
        # Started last: a load that fails before this point leaves nothing running.
        self.cleanup_old_batches.start()
        self.sweep_recently_added.start()
        self.live_progress.start()

    @tasks.loop(minutes=SWEEP_MINUTES)
    async def sweep_recently_added(self):
        """Announce what Plex added that no webhook told us about.

        Plex's webhook needs Plex Pass and Tautulli's "recently added" is delayed
        and sometimes never comes, and an announcement (and the requester's "it's
        ready") hung on one of them arriving. So Plex's own Recently Added list is
        checked every few minutes; anything newer than the last check goes through
        the same announcement path, which skips what was announced already. The
        very first check looks back FIRST_SWEEP_LOOKBACK only, so a library isn't
        announced all over again.
        """
        await self.check_recently_added()
        try:
            await self.check_waiting_for_plex()
        except Exception as e:
            logger.warning(f"Waiting-for-Plex check failed: {e}")
        try:
            from core import blocked_imports
            await blocked_imports.check(self.bot, self.services)
        except Exception as e:
            logger.warning(f"Blocked-imports check failed: {e}")
        try:
            await self.check_movie_grabs()
        except Exception as e:
            logger.warning(f"Checking Radarr's downloads failed: {e}")

    async def _fix_plex_match(self, media: dict, rating_key) -> Optional[str]:
        """core/plex_match for a requested film; the admin channel hears what was fixed."""
        from core.discord_lookup import admin_channel
        from core.plex_match import fix_film_match
        from core.verified_search import find_movie
        if media.get("media_type") != "movie" or not rating_key or not self.services.radarr.configured:
            return None
        try:
            fixed = await fix_film_match(self.services, await find_movie(self.services.radarr, media.get("id")), rating_key)
        except Exception as e:
            logger.warning(f"Couldn't check Plex's match for {media.get('title')}: {e}")
            return None
        if fixed:
            if self._progress is not None:
                self._progress.cache.drop("plex:index:movie")
            channel = admin_channel(self.bot, self.services.config)
            if channel:
                try:
                    await channel.send(f"🎯 **{media.get('title')}**: {fixed}")
                except Exception as e:
                    logger.warning(f"Could not tell the admin channel about {media.get('title')}: {e}")
        return fixed

    async def _flag_wrong_film(self, key, rec: dict, wk: str, plex_ids: list, wanted_ids: list, changed: dict) -> None:
        """Plex has the downloaded file but identifies it as another film: most likely a
        release of a namesake (same title, a year either side) grabbed for this one.
        One help request for the admins, kept until Plex's ids and the request agree."""
        from core import season_search
        media = rec.get("media") or {}
        title = media.get("title") or media.get("name") or "It"
        def words(ids):
            return ", ".join(i.replace("tmdb://", "TMDB ").replace("imdb://", "IMDb ") for i in ids if not i.endswith("://None"))
        asked, theirs = words(wanted_ids), words(plex_ids)
        h = await season_search.open_help(
            self.bot, key, seasons=rec.get("seasons"), reason="wrongfilm", status_now="On Plex, as another film",
            note=f"Plex identifies the downloaded {title} as {theirs}" + (f", not the requested {asked}" if asked else "")
                 + ". It may be a release of a different film with the same name. Check it in Plex; if it's wrong, "
                 "blocklist the release in Radarr (History), delete the file and search again.")
        if h:
            changed[wk] = {"wrong": plex_ids, "help": h.get("id"), "since": datetime.now(timezone.utc).isoformat(),
                           "fixTried": True}
            logger.info(f"{title}: Plex identifies the download as {theirs}; admins told")

    async def check_movie_grabs(self) -> None:
        """Radarr's downloads for requested films, each checked once: one whose title
        is another film's (an indexer labelled a namesake with this film's ID) is
        removed and blocklisted and, unless the film is on disk or downloading
        anyway, searched for again by ID, keeping only releases that are the film
        (core/verified_search.verify_movie). The admin channel hears what was
        stopped; nothing found asks the admins, on a help request, whether to
        search by name."""
        from core import season_search, verified_search
        from core.discord_lookup import admin_channel
        from database.request_store import all_requests
        radarr = self.services.radarr
        if not radarr.configured:
            return
        queue = [q for q in await radarr.queue() if q.get("movieId")
                 and (q.get("downloadId") or q.get("id")) not in self._grabs_checked]
        if not queue:
            return
        now, requested = datetime.now(timezone.utc), {}
        for key, rec in (await all_requests()).items():
            media = (rec or {}).get("media") or {}
            if rec.get("status") != "approved" or media.get("media_type") != "movie" or not media.get("id"):
                continue
            try:
                asked = datetime.fromisoformat(rec.get("timestamp") or "")
            except ValueError:
                continue
            if now - (asked if asked.tzinfo else asked.replace(tzinfo=timezone.utc)) <= timedelta(days=WATCH_DAYS):
                requested[int(media["id"])] = key
        if len(self._grabs_checked) > 5000:
            self._grabs_checked.clear()
        movies: Dict[int, dict] = {}
        for item in queue:
            self._grabs_checked.add(item.get("downloadId") or item.get("id"))
            if item["movieId"] not in movies:
                movies[item["movieId"]] = await radarr.get(f"movie/{item['movieId']}") or {}
            movie = movies[item["movieId"]]
            key = requested.get(int(movie.get("tmdbId") or 0))
            if key is None:
                continue
            result = await verified_search.verify_movie(radarr, movie)
            if not result.get("removed"):
                continue
            title = movie.get("title") or "It"
            text = verified_search.describe_by_id(title, result)
            if result["state"] != "searched" or result.get("grabbed"):
                channel = admin_channel(self.bot, self.services.config)
                if channel:
                    await channel.send(f"🛡️ **{title}**: {text}")
            else:
                await season_search.open_movie_not_found_help(self.bot, key, text)

    async def check_waiting_for_plex(self) -> None:
        """Requests whose files Sonarr/Radarr have but Plex doesn't list ("Adding to
        Plex" on the website). Timed here, not on the website, so it works with
        nobody looking and survives restarts. After STUCK_AFTER the admins get a
        help request ("Downloaded, but not on Plex"), once; when Plex lists it,
        that help request is closed again."""
        from core import season_search
        from database.request_store import all_requests
        from portal import help as helpdesk
        from portal.cache import TTLCache
        from portal.progress import Progress
        if self._progress is None:
            self._progress = Progress(self.services, TTLCache())
        now = datetime.now(timezone.utc)
        waits = await kv_get_all(WAIT_NS)
        seen, changed, done = set(), {}, []
        for key, rec in (await all_requests()).items():
            media = (rec or {}).get("media") or {}
            if rec.get("status") != "approved" or rec.get("media_type") in ("ebook", "audiobook", "both") \
                    or "open_library_key" in media:
                continue
            try:
                asked = datetime.fromisoformat(rec.get("timestamp") or "")
                if asked.tzinfo is None:
                    asked = asked.replace(tzinfo=timezone.utc)
                if now - asked > timedelta(days=WATCH_DAYS):
                    continue
            except ValueError:
                continue
            wk = Progress.wait_key(media, rec.get("seasons"))
            if wk in seen:
                continue
            seen.add(wk)
            try:
                live = await self._progress.video(media, rec.get("seasons"))
            except Exception as e:
                logger.info(f"Couldn't check {media.get('title') or media.get('name')} on Plex: {e}")
                continue
            wait = waits.get(wk)
            if not live.get("waitingForPlex"):
                wrong = isinstance(wait, dict) and wait.get("wrong")
                if isinstance(wait, dict) and wait.get("help") and not wrong:
                    await helpdesk.resolve(wait["help"], "Plexbie", "Plex has it now.")
                if live.get("plexIds") and not (wrong and wait.get("fixTried")):
                    # Plex filed it as another film: matched right by Plexbie when the file is
                    # plainly the requested one, otherwise one help request for the admins.
                    fixed = await self._fix_plex_match(media, live.get("plexKey"))
                    if fixed:
                        if wrong and wait.get("help"):
                            await helpdesk.resolve(wait["help"], "Plexbie", fixed)
                        if wait is not None:
                            done.append(wk)
                        continue
                    if wrong:
                        changed[wk] = {**wait, "fixTried": True}
                    else:
                        await self._flag_wrong_film(key, rec, wk, live["plexIds"], live.get("wantedIds") or [], changed)
                # A wrong film is put right once Plex has the requested one (rematched, or the
                # right file); while the replacement downloads, the record and help stay.
                put_right = live.get("stage") == "available" and not live.get("plexIds")
                if wait is not None and wk not in changed and (not wrong or put_right):
                    done.append(wk)
                    if wrong and wait.get("help"):
                        await helpdesk.resolve(wait["help"], "Plexbie", "Plex now has the requested film.")
                continue
            if not isinstance(wait, dict):
                changed[wk] = {"since": now.isoformat(), "request": key}
                continue
            if wait.get("alerted") or now - datetime.fromisoformat(wait["since"]) < STUCK_AFTER:
                continue
            title = media.get("title") or media.get("name") or "It"
            h = await season_search.open_help(
                self.bot, key, seasons=rec.get("seasons"), reason="notonplex", status_now="Adding to Plex",
                note=f"{title} has been downloaded for over {int(STUCK_AFTER.total_seconds() // 3600)} hours, "
                     "but Plex still doesn't list it. Scan the library in Plex, and check the folder "
                     "Sonarr/Radarr put it in is one of Plex's library folders.")
            changed[wk] = {**wait, "alerted": now.isoformat(), "help": (h or {}).get("id")}
            logger.info(f"{title}: on disk but not on Plex after {STUCK_AFTER}; admins told")
        done += list(set(waits) - seen)       # also: declined, removed or too old
        if changed:
            await kv_set_many(WAIT_NS, changed)
        if done:
            await kv_delete_many(WAIT_NS, done)

    async def check_recently_added(self) -> None:
        """One Recently Added check (see sweep_recently_added). Also run straight
        away when Seerr says something became available, so Plexbie confirms
        it on Plex itself rather than taking Seerr's word for it."""
        async with self._sweep_lock:
            await self._check_recently_added()

    async def _check_recently_added(self) -> None:
        server = self.services.plex_server
        if server is None:
            return
        try:
            items = await run_blocking(_recent_items, server)
        except Exception as e:
            logger.info(f"Recently added sweep skipped: {e}")
            return
        mark = await kv_get(SWEEP_NAMESPACE, "mark")
        # First check: only what arrived in the last couple of hours (a fresh install
        # still announces what came in during setup), not the whole library.
        since = _utc(datetime.fromisoformat(mark)) if mark else datetime.now(timezone.utc) - FIRST_SWEEP_LOOKBACK
        fresh = sorted(((k, t) for k, t in items if t > since), key=lambda kt: kt[1])
        for key, _ in fresh:
            try:
                await self.announce_rating_key(key)
            except Exception as e:
                logger.warning(f"Couldn't announce Plex item {key}: {e}")
        if fresh:
            logger.info(f"Recently added sweep: {len(fresh)} new item(s) on Plex")
        if fresh or mark is None:
            await kv_set(SWEEP_NAMESPACE, "mark", (fresh[-1][1] if fresh else since).isoformat())
        await self._follow_open_batches(server)

    async def _follow_open_batches(self, server) -> None:
        """Episodes that joined a season already announced. Plex's Recently Added
        lists a season once, with the time its first episode came, so the rest of a
        season arriving over the next hours doesn't show up there as anything new
        (and no webhook may say so either). For each announcement still open (the
        season not complete, an episode within GROUP_WINDOW), ask Plex which of the
        season's episodes came since the run began, and add any it hasn't got."""
        await self.load_tracking_data()
        now = datetime.now(timezone.utc)
        for batch in list(self.active_batches.values()):
            if not batch.message_id or batch.is_complete() or now - batch.last_update > GROUP_WINDOW:
                continue
            if not batch.show_key:
                # Announced before batches kept the show's key.
                try:
                    batch.show_key = await run_blocking(_show_key_by_title, server, batch.show_title)
                except Exception as e:
                    logger.info(f"Couldn't find {batch.show_title} on Plex: {e}")
                if not batch.show_key:
                    continue
            within = now - batch.started + RECENT_EPISODES
            try:
                found = await run_blocking(_recently_added_episodes, server, batch.show_key, within)
            except Exception as e:
                logger.info(f"Couldn't check {batch.show_title} S{batch.season} for more episodes: {e}")
                continue
            more = [(n, t) for season, n, t in found if season == batch.season and n not in batch.episodes]
            if more:
                logger.info(f"{len(more)} more episode(s) of {batch.show_title} S{batch.season} on Plex since the announcement")
                await self._arrivals(batch.show_title, batch.season, more, batch.tmdb_id, batch.tvdb_id, {},
                                     show_key=batch.show_key)

    @tasks.loop(minutes=1)
    async def live_progress(self):
        """Downloads kept live on requesters' phones (core/live_progress)."""
        from core import live_progress
        from portal.cache import TTLCache
        from portal.progress import Progress
        if self._progress is None:
            self._progress = Progress(self.services, TTLCache())
        try:
            await live_progress.tick(self.services, self._progress)
        except Exception as e:
            logger.warning(f"Live progress pass failed: {type(e).__name__}: {e}")

    @live_progress.before_loop
    async def _before_live(self):
        await self.bot.wait_until_ready()

    @sweep_recently_added.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_ready()

    def _ping(self) -> dict:
        """send() kwargs: the opt-in role's mention, for new posts only."""
        role_id = self.services.config.arrivals_role_id
        if not role_id:
            return {}
        return {"content": f"<@&{int(role_id)}>",
                "allowed_mentions": discord.AllowedMentions(everyone=False, users=False, roles=True)}

    async def announce_rating_key(self, rating_key) -> None:
        """Announce a Plex item by its key: Tautulli's "recently added" event, which works
        without Plex Pass. Goes through the same paths as Plex's own webhook, so an
        arrival both report is announced once."""
        server = self.services.plex_server
        if server is None or not rating_key:
            return
        try:
            item = await run_blocking(server.fetchItem, int(rating_key))
        except Exception as e:
            logger.warning(f"Couldn't look up Plex item {rating_key}: {e}")
            return
        guids = [{"id": g.id} for g in (getattr(item, "guids", None) or [])]
        payload = {"Server": {"uuid": getattr(server, "machineIdentifier", None)}}
        kind = getattr(item, "type", "")
        meta = {"type": kind, "title": item.title, "key": item.key, "ratingKey": item.ratingKey,
                "summary": getattr(item, "summary", "") or "", "Guid": guids}
        if kind == "episode":
            meta.update(grandparentTitle=item.grandparentTitle, parentIndex=item.parentIndex, index=item.index,
                        grandparentRatingKey=getattr(item, "grandparentRatingKey", None),
                        grandparentGuid=getattr(item, "grandparentGuid", None))
            if not meta["grandparentGuid"]:
                meta.pop("grandparentGuid")
        elif kind == "season":
            meta.update(parentTitle=item.parentTitle, parentRatingKey=getattr(item, "parentRatingKey", None),
                        parentGuid=getattr(item, "parentGuid", None) or "")
        await self.handle_plex_webhook({"event": "library.new", "Metadata": meta, **payload})

    async def register_webhook_routes(self, webhook_server):
        """Register Plex webhook route before server starts"""
        from aiohttp import web

        async def handle_plex_webhook(request):
            try:
                # Plex sends multipart/form-data
                data = await request.post()
                payload_json = data.get("payload")

                if payload_json:
                    payload = json.loads(payload_json)
                    await self.handle_plex_webhook(payload)

                return web.json_response({"status": "ok"})
            except Exception as e:
                logger.error(f"Plex webhook error: {e}")
                return web.json_response({"error": str(e)}, status=400)

        # Register through the server so the route is signature-validated.
        # Plex sends no auth header of its own, so protect it by appending
        # ?token=<PLEX_WEBHOOK_SECRET> to the webhook URL in Plex settings.
        webhook_server.add_validated_post("/webhook/plex", handle_plex_webhook, "plex")
        logger.info("✅ Registered Plex webhook handler at /webhook/plex")

    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        self.cleanup_old_batches.cancel()
        self.sweep_recently_added.cancel()
        self.live_progress.cancel()
        # Note: Can't await in cog_unload, data will be saved on next cleanup cycle

    async def load_tracking_data(self):
        """Load tracking data from database"""
        if self._data_loaded:
            return
        try:
            data = await kv_get_all(NEW_MEDIA_NAMESPACE)
            if isinstance(data.get(ANNOUNCED_KEY), dict):
                await self._move_announced(data.pop(ANNOUNCED_KEY))
            stray = [k for k, v in data.items() if not isinstance(v, dict)]
            if stray:                   # not batches: an older version kept its sweep mark here
                await kv_delete_many(NEW_MEDIA_NAMESPACE, stray)
            for key, batch_data in data.items():
                if not isinstance(batch_data, dict):
                    continue
                # One bad row skips that batch, not every batch after it. It stays
                # in the database for someone to look at; it is never saved over.
                try:
                    self.active_batches[key] = EpisodeBatch.from_dict(batch_data)
                except (KeyError, TypeError, ValueError) as e:
                    logger.warning(f"Skipping unreadable episode batch {key!r}: {e!r}")
            self._data_loaded = True
            logger.info(f"Loaded {len(self.active_batches)} active batches")
        except Exception as e:
            logger.error(f"Error loading tracking data: {e}")

    async def _move_announced(self, legacy: Dict[str, str]):
        """Carry 1.0's "announced" row over to its own namespace, then drop it from
        the batches. Anything already in the new place was written later, so it wins."""
        merged = {**legacy, **(await kv_get(ANNOUNCED_NAMESPACE, ANNOUNCED_KEY) or {})}
        await kv_set(ANNOUNCED_NAMESPACE, ANNOUNCED_KEY, merged)
        await kv_delete_many(NEW_MEDIA_NAMESPACE, [ANNOUNCED_KEY])
        logger.info(f"Moved {len(merged)} movie announcement time(s) out of the episode batches")

    async def save_tracking_data(self):
        """Save tracking data to database, in one transaction.

        This writes the whole namespace, so a per-key kv_set meant one transaction
        and one fsync per tracked batch every time any single batch changed.
        """
        try:
            await kv_set_many(
                NEW_MEDIA_NAMESPACE,
                {key: batch.to_dict() for key, batch in self.active_batches.items()},
            )
        except Exception as e:
            logger.error(f"Error saving tracking data: {e}")

    @tasks.loop(hours=1)
    async def cleanup_old_batches(self):
        """Forget batches that have been idle for a day and are not being watched for.

        Hourly: the cutoff is a day, so checking every five minutes only re-read
        the tracking data 287 extra times a day.

        Removing them from active_batches is not enough on its own:
        save_tracking_data upserts the survivors, so every removed row stayed in the
        database and came back on the next restart. The log said "Cleaned up 47 old
        batches" twenty times over - the same 47 each time - and 48 of 56 rows were
        stale by the time anyone counted.

        Monitored batches are deliberately never removed here. is_monitored means a
        requester is still waiting on that show, and dropping the batch would lose
        the "your request is available" notification. They do accumulate as a
        result; nothing currently re-checks whether the request behind one is still
        outstanding.
        """
        await self.load_tracking_data()

        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        to_remove = [
            key for key, batch in self.active_batches.items()
            if batch.last_update < cutoff and not batch.is_monitored
        ]
        if not to_remove:
            return

        for key in to_remove:
            del self.active_batches[key]

        try:
            removed = await kv_delete_many(NEW_MEDIA_NAMESPACE, to_remove)
        except Exception as e:
            # The keys are already out of active_batches, so leaving it there would
            # let a later save write a set that disagrees with the database. Reload
            # from the database instead and treat it as the authority.
            logger.error(f"Could not delete old batches, reloading from database: {e}")
            self.active_batches.clear()
            self._data_loaded = False
            await self.load_tracking_data()
            return

        logger.info(
            f"Cleaned up {len(to_remove)} old batch(es); {removed} row(s) deleted"
        )

    def get_batch_key(self, show_title: str, season: int) -> str:
        """Generate a unique key for a show/season batch"""
        return f"{show_title.lower()}:s{season}"

    async def _fetch_tmdb_data(
        self, tmdb_id: int, media_type: str = "tv"
    ) -> Optional[Dict]:
        """Fetch TMDB data for media"""
        if not self.services.config.tmdb_api_key:
            logger.warning("TMDB API key not configured")
            return None

        try:
            return await self.services.tmdb.get(f"{media_type}/{tmdb_id}")

        except Exception as e:
            logger.error(f"Error fetching TMDB data: {e}")
            return None


    async def _create_media_embed(
        self,
        title: str,
        description: str,
        overview: str,
        poster_url: Optional[str],
        tmdb_id: Optional[int],
        tvdb_id: Optional[int],
        plex_web_url: Optional[str],
        media_type: str = "tv",
    ) -> discord.Embed:
        """Create a rich embed for new media"""

        embed = discord.Embed(
            title=title,
            description=f"**{description}**\n\n{overview}",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        # Add poster image
        if poster_url:
            embed.set_image(url=poster_url)

        # Add links
        links = []
        if tvdb_id and media_type == "tv":
            links.append(
                f"[View Details on TheTVDB](https://thetvdb.com/?tab=series&id={tvdb_id})"
            )
        if plex_web_url:
            links.append(f"[View Details on Plex Web]({plex_web_url})")

        if links:
            embed.add_field(name="Links", value=truncate_field("\n".join(links)), inline=False)

        return embed

    def _format_episode_list(self, episodes: List[int]) -> str:
        """Format a list of episodes nicely (e.g., '1-3, 5, 7-9')"""
        if not episodes:
            return ""

        episodes = sorted(episodes)
        ranges = []
        start = episodes[0]
        end = episodes[0]

        for i in range(1, len(episodes)):
            if episodes[i] == end + 1:
                end = episodes[i]
            else:
                if start == end:
                    ranges.append(f"{start}")
                else:
                    ranges.append(f"{start}-{end}")
                start = end = episodes[i]

        # Add last range
        if start == end:
            ranges.append(f"{start}")
        else:
            ranges.append(f"{start}-{end}")

        return ", ".join(ranges)

    async def handle_plex_webhook(self, payload: Dict[str, Any]):
        """Process Plex webhook payload"""
        try:
            event = payload.get("event")

            # We only care about library.new events
            if event != "library.new":
                return

            metadata = payload.get("Metadata", {})
            if not metadata:
                return

            media_type = metadata.get("type")

            # TV goes through the season announcements (one post, then edits).
            if media_type == "episode":
                await self._handle_new_episode(metadata, payload)
            elif media_type in ("show", "season"):
                if not await self._handle_new_show_or_season(metadata, payload, media_type):
                    await self._handle_new_media(metadata, payload, "show")
            elif media_type == "movie":
                await self._handle_new_media(metadata, payload, media_type)

        except Exception as e:
            logger.error(f"Error processing Plex webhook: {e}", exc_info=True)

    def _ids(self, *guid_lists) -> tuple:
        """(tmdb_id, tvdb_id) from Plex GUID lists, first found wins."""
        tmdb_id = tvdb_id = None
        for guids in guid_lists:
            for guid in guids or []:
                guid_id = guid.get("id", "")
                tmdb_id = tmdb_id or guid_number(guid_id, "tmdb")
                tvdb_id = tvdb_id or guid_number(guid_id, "tvdb")
        return tmdb_id, tvdb_id

    async def _show_ids(self, show_key) -> tuple:
        """(tmdb_id, tvdb_id) of the show itself. An episode's or season's own GUIDs
        name that episode or season, which finds the wrong poster on TMDB and no
        match in Sonarr or the request tracker."""
        if not show_key or self.services.plex_server is None:
            return None, None
        key = str(show_key)
        if key not in self._show_ids_cache:
            try:
                show = await run_blocking(self.services.plex_server.fetchItem, int(key))
                self._show_ids_cache[key] = self._ids([{"id": g.id} for g in (getattr(show, "guids", None) or [])])
            except Exception as e:
                logger.info(f"Couldn't read the show's ids for {key}: {e}")
                return None, None
        return self._show_ids_cache[key]

    async def _handle_new_episode(self, metadata: Dict, payload: Dict):
        """One new episode (Plex sends these when a single episode is added)."""
        tmdb_id, tvdb_id = await self._show_ids(metadata.get("grandparentRatingKey"))
        if not (tmdb_id or tvdb_id):
            # No Plex connection: the show's GUID, if it carries a TMDB id.
            tmdb_id, tvdb_id = self._ids([{"id": metadata["grandparentGuid"]}] if "grandparentGuid" in metadata else [])
        await self._arrivals(metadata.get("grandparentTitle", "Unknown Show"), metadata.get("parentIndex", 0),
                             [(metadata.get("index", 0), metadata.get("title", "Unknown Episode"))],
                             tmdb_id, tvdb_id, payload, show_key=metadata.get("grandparentRatingKey"))

    async def _handle_new_show_or_season(self, metadata: Dict, payload: Dict, kind: str) -> bool:
        """Several episodes at once. Plex then sends one show- (or season-) level event
        with no episode numbers, so ask Plex which episodes arrived lately and announce
        those like any others. False if that can't be worked out."""
        server = self.services.plex_server
        if server is None or not metadata.get("ratingKey"):
            return False
        try:
            found = await run_blocking(_recently_added_episodes, server, metadata["ratingKey"], RECENT_EPISODES)
        except Exception as e:
            logger.warning(f"Couldn't list the new episodes of {metadata.get('title')}: {e}")
            return False
        if not found:
            return False
        show_title = metadata.get("title") if kind == "show" else metadata.get("parentTitle") or metadata.get("title")
        show_key = metadata.get("ratingKey") if kind == "show" else metadata.get("parentRatingKey")
        tmdb_id, tvdb_id = await self._show_ids(show_key)
        if not (tmdb_id or tvdb_id) and kind == "show":
            tmdb_id, tvdb_id = self._ids(metadata.get("Guid", []))
        by_season: Dict[int, List[tuple]] = defaultdict(list)
        for season, number, title in found:
            by_season[season].append((number, title))
        for season in sorted(by_season):
            await self._arrivals(show_title or "Unknown Show", season, by_season[season], tmdb_id, tvdb_id, payload,
                                 show_key=show_key)
        return True

    async def _arrivals(self, show_title: str, season: int, episodes: List[tuple],
                        tmdb_id: Optional[int], tvdb_id: Optional[int], payload: Dict, show_key=None) -> None:
        """Episodes of one season reached Plex: add them to the season's announcement
        (one post, then edits), mark tracked requests, and DM requesters."""
        async with self._batch_locks[self.get_batch_key(show_title, season)]:
            await self._arrivals_locked(show_title, season, episodes, tmdb_id, tvdb_id, payload, show_key)

    async def _arrivals_locked(self, show_title: str, season: int, episodes: List[tuple],
                               tmdb_id: Optional[int], tvdb_id: Optional[int], payload: Dict, show_key=None) -> None:
        await self.load_tracking_data()
        try:
            batch_key = self.get_batch_key(show_title, season)
            batch = self.active_batches.get(batch_key)
            if not batch:
                batch = EpisodeBatch(show_title, season, tmdb_id)
                self.active_batches[batch_key] = batch
                # Integration point with media_requests: is someone waiting for it?
                batch.is_monitored = await self._check_if_monitored(show_title, season, tmdb_id)

            batch.tvdb_id = tvdb_id or batch.tvdb_id
            batch.show_key = str(show_key) if show_key else batch.show_key
            # Post or edit, decided before these episodes refresh last_update.
            post_new = batch.should_create_new_message()
            if post_new and batch.message_id:
                # A new run of episodes (a week later, say): start a fresh message.
                batch.episodes, batch.titles, batch.message_id = [], {}, None
                batch.started = datetime.now(timezone.utc)
            before = set(batch.episodes)
            for number, title in episodes:
                batch.add_episode(number)
                batch.titles[str(number)] = title
            if not post_new and set(batch.episodes) == before:
                # Already announced (Plex and Tautulli both reported it): nothing to change.
                return

            notify = []
            if tmdb_id and batch.is_monitored:
                try:
                    from core.media_tracking import get_media_tracker
                    tracker = get_media_tracker()
                    for number, title in episodes:
                        tracked = tracker.mark_episode_available(tmdb_id, season, number, title)
                        if tracked and tracked.should_notify_for_episode_arrival(season, number):
                            notify.append(tracked)
                except Exception as e:
                    logger.error(f"Error updating tracked media: {e}")

            await self._publish_batch(batch, tmdb_id, tvdb_id, payload, post_new)

            for tracked in notify[:1]:
                await self._send_requester_availability_dm(tracked, media_kind="tv", detail=f"Season {season}, Episode 1")

            await self.save_tracking_data()
        except Exception as e:
            logger.error(f"Error handling new episodes of {show_title}: {e}", exc_info=True)

    async def _season_size(self, tmdb_id: Optional[int], tvdb_id: Optional[int], season: int) -> Optional[int]:
        """How many episodes of the season have aired: Sonarr's count, else TMDB's."""
        now = datetime.now(timezone.utc)
        sonarr = self.services.sonarr
        try:
            if sonarr.configured and (tmdb_id or tvdb_id):
                series = next((x for x in await sonarr.series()
                               if (tmdb_id and x.get("tmdbId") == tmdb_id) or (tvdb_id and x.get("tvdbId") == tvdb_id)), None)
                if series:
                    aired = [e for e in await sonarr.episodes(series["id"])
                             if int(e.get("seasonNumber", -1)) == int(season)
                             and e.get("airDateUtc") and e["airDateUtc"] <= now.isoformat()]
                    if aired:
                        return len(aired)
            if tmdb_id and self.services.tmdb.configured:
                data = await self.services.tmdb.get(f"tv/{tmdb_id}/season/{int(season)}") or {}
                today = now.date().isoformat()
                aired = [e for e in data.get("episodes") or [] if e.get("air_date") and e["air_date"] <= today]
                return len(aired) or None
        except Exception as e:
            logger.info(f"Couldn't tell how many episodes season {season} has: {e}")
        return None

    def _batch_text(self, batch: EpisodeBatch) -> tuple:
        """(title, headline) for the announcement as it stands."""
        show, season, eps = batch.show_title, batch.season, sorted(batch.episodes)
        size = batch.expected_episode_count
        if batch.is_complete():
            return (f"✅ Season {season} of {show} has been added!",
                    f"All {size} episodes of season {season} are ready to watch.")
        if len(eps) == 1:
            title = f"📺 A new episode of {show} has been added!"
            return title, episode_label(show, season, eps[0], batch.titles.get(str(eps[0])))
        title = f"📺 New episodes of {show} have been added!"
        line = f"{show} - Season {season}, Episodes {self._format_episode_list(eps)}"
        if size and size > len(eps):
            filled = round(10 * len(eps) / size)
            line += f"\n`{'▰' * filled}{'▱' * (10 - filled)}` {len(eps)} of {size} · more on the way"
        return title, line

    async def _publish_batch(self, batch: EpisodeBatch, tmdb_id: Optional[int], tvdb_id: Optional[int],
                             payload: Dict, post_new: bool) -> None:
        """Post the season's announcement, or edit it with the episodes since.

        Only a new post notifies the channel. Every later episode, and the season
        completing, is an edit, so members hear once per batch of episodes.
        """
        channel_id = batch.channel_id if (batch.message_id and not post_new) else self.services.config.updates_channel_id
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if not channel:
            logger.warning("Updates channel not configured" if not channel_id else f"Could not find channel {channel_id}")
            return

        if post_new or not batch.expected_episode_count or len(batch.episodes) >= batch.expected_episode_count:
            # Fresh count on a new post, and again whenever the season looks done
            # (more may have aired since the post).
            batch.expected_episode_count = await self._season_size(tmdb_id, tvdb_id, batch.season) or batch.expected_episode_count

        batch.plex_url = batch.plex_url or _plex_web_url(payload)

        tmdb_data = await self._fetch_tmdb_data(tmdb_id, "tv") if tmdb_id else None
        overview, poster_url = _overview_and_poster(tmdb_data)

        title, headline = self._batch_text(batch)
        embed = await self._create_media_embed(title, headline, overview, poster_url, tmdb_id, tvdb_id, batch.plex_url)
        if batch.is_complete():
            embed.color = discord.Color.green()

        episodes = self._format_episode_list(batch.episodes)
        if batch.message_id and not post_new:
            try:
                # Edits notify nobody, which is the point. A partial message is enough:
                # everything the embed needs is kept on the batch.
                await channel.get_partial_message(batch.message_id).edit(embed=embed)
                logger.info(f"Updated episode message for {batch.show_title} S{batch.season} - Episodes {episodes}")
                return
            except discord.NotFound:
                logger.warning(f"Message {batch.message_id} is gone; posting a new one")
            except Exception as e:
                logger.error(f"Error editing episode message: {e}")
                return
        message = await channel.send(embed=embed, **self._ping())
        batch.message_id, batch.channel_id = message.id, channel.id
        logger.info(f"Posted new episode message for {batch.show_title} S{batch.season} - Episodes {episodes}")

    async def _handle_new_media(
        self, metadata: Dict, payload: Dict, media_type: str
    ):
        """Handle new movie or show"""
        try:
            title = metadata.get("title", "Unknown")

            # Get GUIDs
            guids = metadata.get("Guid", [])
            tmdb_id = None
            tvdb_id = None

            for guid in guids:
                guid_id = guid.get("id", "")
                if not tmdb_id:
                    tmdb_id = guid_number(guid_id, "tmdb")
                if not tvdb_id:
                    tvdb_id = guid_number(guid_id, "tvdb")

            # Fetch TMDB data
            tmdb_data = None
            if tmdb_id:
                tmdb_data = await self._fetch_tmdb_data(tmdb_id, "tv" if media_type == "show" else media_type)

            # Build embed
            icon = "🎬" if media_type == "movie" else "📺"
            embed_title = f"{icon} {title} has been added!"

            overview, poster_url = _overview_and_poster(tmdb_data, metadata.get("summary"))
            plex_web_url = _plex_web_url(payload)

            embed = await self._create_media_embed(
                embed_title,
                title,
                overview,
                poster_url,
                tmdb_id,
                tvdb_id,
                plex_web_url,
                media_type,
            )

            # Send to updates channel
            channel_id = self.services.config.updates_channel_id
            if not channel_id:
                logger.warning("Updates channel not configured")
                return

            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.error(f"Could not find channel {channel_id}")
                return

            async with self._announce_lock:
                seen_key = f"{media_type}:{tmdb_id or title.lower()}"
                seen = await kv_get(ANNOUNCED_NAMESPACE, ANNOUNCED_KEY) or {}
                last = seen.get(seen_key)
                if last and datetime.now(timezone.utc) - datetime.fromisoformat(last) < MOVIE_REPEAT:
                    logger.info(f"{title} was announced already; not again")
                    return
                await channel.send(embed=embed, **self._ping())
                cutoff = datetime.now(timezone.utc) - MOVIE_REPEAT
                seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v) > cutoff}
                seen[seen_key] = datetime.now(timezone.utc).isoformat()
                await kv_set(ANNOUNCED_NAMESPACE, ANNOUNCED_KEY, seen)

            logger.info(f"Posted new {media_type} notification for {title}")

            if media_type == "movie" and tmdb_id:
                from core.media_tracking import get_media_tracker

                tracker = get_media_tracker()
                tracked_media = tracker.get_tracked_media(tmdb_id, media_type="movie")
                if tracked_media and tracked_media.should_notify_for_movie_arrival():
                    await self._send_requester_availability_dm(
                        tracked_media,
                        media_kind="movie",
                    )

        except Exception as e:
            logger.error(f"Error handling new media: {e}", exc_info=True)

    async def _check_if_monitored(
        self, show_title: str, season: int, tmdb_id: Optional[int]
    ) -> bool:
        """Check if this show/season is being monitored (from media_requests)"""
        try:
            from core.media_tracking import get_media_tracker

            tracker = get_media_tracker()

            # Try to find by TMDB ID first (most reliable)
            if tmdb_id:
                tracked = tracker.get_tracked_media(tmdb_id, season, media_type="tv")
                if tracked:
                    return True

            # Fallback: try to match by title and season
            tracker.load_tracking_data()
            for tracked_media in tracker.tracked_media.values():
                requested_seasons = tracked_media.requested_season_numbers() if hasattr(tracked_media, "requested_season_numbers") else None
                season_matches = (
                    tracked_media.season_number == season
                    or tracked_media.season_number is None
                    or requested_seasons is None
                    or season in requested_seasons
                )
                if (
                    tracked_media.media_type == "tv"
                    and tracked_media.title.lower() == show_title.lower()
                    and season_matches
                ):
                    return True

            return False

        except Exception as e:
            logger.error(f"Error checking if monitored: {e}", exc_info=True)
            return False

    async def _notify_website_requester(self, tracked_media, message: str, reason: str):
        """The requester asked on the website without Discord: phone alert from plexbie.com, else email."""
        from core.media_tracking import get_media_tracker
        from core.notify import notify_member, plain

        how = await notify_member(
            self.services, title=f"Ready to watch: {tracked_media.title}", body=plain(message),
            url="/app/schedule", plex_account_id=tracked_media.requester_plex_id,
            plex_name=tracked_media.requester_plex_name, context=f"arrival of {tracked_media.title}")
        # Marked either way: the website already shows it as arrived, and retrying
        # every webhook for someone with no alerts and no email would only spam the log.
        tracked_media.mark_requester_notified(reason if how != "none" else f"{reason} (no alert route)")
        await run_blocking(get_media_tracker().save_tracking_data)

    async def _send_requester_availability_dm(self, tracked_media, media_kind: str, detail: Optional[str] = None):
        """Send a friendly requester DM when media first becomes watchable on Plex."""
        try:
            if tracked_media.requester_notification_sent:
                return

            if media_kind == "movie":
                dm_message = f"✅ **Good news!** The movie you requested, **{tracked_media.title}**, is now available on Plex."
                reason = "movie_available"
            else:
                dm_message = (
                    f"✅ **Good news!** **{tracked_media.title}** is now ready to start on Plex — "
                    f"**{detail or 'Episode 1'}** is available."
                )
                reason = "requested_season_episode_1_available"

            from portal.ticket_view import mark_arrived
            if not tracked_media.requester_user_id:
                await self._notify_website_requester(tracked_media, dm_message, reason)
                await mark_arrived(self.bot, tracked_media.tmdb_id, plex_id=tracked_media.requester_plex_id,
                                   media_type=tracked_media.media_type)
                return

            user = await self.bot.fetch_user(tracked_media.requester_user_id)
            # On Manage → Messages like every DM (no receipt in the admin channel).
            await send_user_dm(self.bot, self.services, user, context=f"arrival of {tracked_media.title}",
                               content=dm_message)
            tracked_media.mark_requester_notified(reason)
            # The approval DM's "Open a ticket" comes off now it's here.
            await mark_arrived(self.bot, tracked_media.tmdb_id, user_id=tracked_media.requester_user_id,
                               media_type=tracked_media.media_type)

            from core.media_tracking import get_media_tracker
            get_media_tracker().save_tracking_data()

            logger.info(f"Sent requester availability DM to user {tracked_media.requester_user_id} for {tracked_media.title}")

        except discord.Forbidden:
            # Logged as not delivered on Manage → Messages.
            logger.warning(f"Cannot DM user {tracked_media.requester_user_id} - DMs are disabled")
        except Exception as e:
            logger.error(f"Error sending requester availability DM: {e}", exc_info=True)
