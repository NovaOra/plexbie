# path: bot.py
"""Plexbie - Modular Discord Bot for Plex Management"""
import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# Plexbie never joins voice (Watch Party only reads who is streaming), so the
# "PyNaCl/davey is not installed, voice will NOT be supported" warnings are noise.
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False

from core.config import Config
from core.discord_lookup import (
    PUBLIC_BOT_FIX, admin_channel, clear_global_commands, home_guild, not_in_text, pick_home_guild, problem_text,
)
from core.logging import setup_logging, get_logger
from core.permissions import HomeGuildTree
from core.plugin_manager import PluginManager
from core.services import BotServices
from core.webhooks import WebhookServer
from database.session import init_database

logger = get_logger(__name__)

#: Posts and phone alerts about other servers Plexbie was added to, per hour. Anyone
#: who owns many servers could otherwise fill the admin channel; the rest go to the
#: log, and Manage → Health lists every server.
FOREIGN_NOTICES_PER_HOUR = 3


class Plexbie(commands.Bot):
    """Main bot class with modular plugin support"""
    
    def __init__(self, config: Config, services: BotServices):
        intents = discord.Intents.default()
        # No Message Content intent: Plexbie only reads message text in DMs (the
        # /join-plex email reply), and Discord sends DM text without it.
        intents.message_content = False
        intents.members = True
        
        super().__init__(
            # Only slash commands; a mention prefix keeps discord.py from warning
            # that text commands need the Message Content intent.
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            # Nothing Plexbie posts pings anyone unless that send says so: text from members
            # (a ticket answer in the admin channel) or from Plex, Seerr and TMDB can't
            # @everyone or ping a role. /say, Manage's "say" and the arrivals role ping opt in.
            allowed_mentions=discord.AllowedMentions.none(),
            # Commands, autocomplete and context menus answer only in the household's server.
            tree_cls=HomeGuildTree,
        )
        
        self.config = config
        self.services = services
        self.plugin_manager = PluginManager(self, services)
        self.webhook_server = WebhookServer(services)
        # Which server is home is settled once, on the first on_ready (_settle_home_guild).
        self._home_settled = False
        self._home_problem = None        # why commands are off, when GUILD_ID is blank
        self._told_foreign = set()       # servers the admins were already told about
        self._foreign_notices = []       # when they were last told (FOREIGN_NOTICES_PER_HOUR)
        self._synced = None              # commands in the household's server, once synced
    
    async def setup_hook(self):
        """Initialize bot components on startup"""
        logger.info("Starting Plexbie bot setup")

        # What people DM Plexbie: on Manage → Messages, an alert to the admins, and their
        # thread under the admin channel (portal/inbox.py).
        from portal import inbox

        async def on_dm(message):
            await inbox.on_dm(self, message)
        self.add_listener(on_dm, "on_message")

        # Set up global interaction logging
        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: Exception):
            """Global error handler for app commands"""
            command_name = interaction.command.name if interaction.command else "unknown"

            # A refused command is not a crash. These used to be logged at ERROR
            # with a full traceback and answered with "An error occurred... try
            # again later", which told the user nothing and made ordinary denials
            # look like failures in the log.
            if isinstance(error, app_commands.MissingPermissions):
                logger.info(
                    f"/{command_name} denied for {interaction.user} "
                    f"({interaction.user.id}): missing permissions"
                )
                message = "You don't have permission to use this command."
            elif isinstance(error, app_commands.NoPrivateMessage):
                logger.info(f"/{command_name} attempted in a DM by {interaction.user}")
                message = "This command only works inside a server."
            elif isinstance(error, app_commands.CommandOnCooldown):
                logger.info(f"/{command_name} on cooldown for {interaction.user}")
                message = f"That command is on cooldown - try again in {error.retry_after:.0f}s."
            else:
                logger.error(
                    f"App command error: /{command_name} by {interaction.user} "
                    f"({interaction.user.id}) - {error}",
                    exc_info=error,
                )
                message = "An error occurred while processing your command. Please try again later."

            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(message, ephemeral=True)
                else:
                    await interaction.followup.send(message, ephemeral=True)
            except discord.HTTPException as e:
                # The interaction may have expired; nothing more to do than note it.
                logger.debug(f"Could not deliver error message for /{command_name}: {e}")

        # Load all plugins (they can now register webhook routes)
        await self.plugin_manager.load_all_plugins()

        # Register plugin webhook routes BEFORE starting server
        await self.plugin_manager.register_webhook_routes(self.webhook_server)

        # Webhooks: Sonarr/Radarr for media tracking, Seerr and Tautulli for
        # requests and playback (webhooks/*_handler.py)
        from webhooks.sonarr_handler import register_sonarr_webhook
        from webhooks.radarr_handler import register_radarr_webhook
        from webhooks.seerr_handler import register_seerr_webhook
        from webhooks.tautulli_handler import register_tautulli_webhook

        register_sonarr_webhook(self.webhook_server, self)
        register_radarr_webhook(self.webhook_server, self)
        register_seerr_webhook(self.webhook_server, self)
        register_tautulli_webhook(self.webhook_server, self)

        # Start webhook server (this freezes the router). A port clash is loud but
        # not fatal: Discord commands and the website keep working without webhooks.
        try:
            await self.webhook_server.start()
            # Keep the Seerr/Tautulli webhooks Plexbie set up in step with this version.
            from core.webhook_connect import refresh_connections
            from portal.setup import _lan_address
            base = f"http://{_lan_address()}:{self.config.webhook_port}"
            self._refresh_task = asyncio.create_task(refresh_connections(self.services, base))
        except RuntimeError as e:
            logger.error(f"❌ Webhooks are OFF: {e}. Pick a free WEBHOOK_PORT in config/.env and restart.")

        # plexbie.com, when WEB_PORT is set. Never allowed to stop the bot starting.
        try:
            from portal.server import start_portal
            self.portal_runner = await start_portal(self, self.services)
        except Exception as e:
            logger.error(f"Web portal failed to start: {e}", exc_info=True)
            self.portal_runner = None

        # DEBUG: Log what commands are in the tree before syncing
        logger.debug(f"Commands in tree before sync: {len(self.tree.get_commands())}")
        for cmd in self.tree.get_commands():
            logger.debug(f"  - {cmd.name}: {cmd.description}")

        await self._sync_commands()

    async def _sync_commands(self) -> None:
        """Slash commands go to the household's server (GUILD_ID) only, never the
        global scope: a guild sync is immediate, and global commands would appear
        in every server Plexbie is ever added to. With GUILD_ID blank nothing is
        registered until on_ready has worked out which server is home."""
        if self.config.guild_id:
            await self._sync_to_home(self.config.guild_id)
        else:
            logger.info("Commands: GUILD_ID is blank. Waiting to see which Discord server Plexbie is in; "
                        "nothing is registered globally.")

    async def _sync_to_home(self, guild_id: int) -> bool:
        """Put every command in the household's server. Never fatal: Plexbie may not
        be in that server yet (moving to a new one), and joining it syncs again."""
        guild = discord.Object(id=guild_id)
        ok = False
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            self._synced = len(synced)
            logger.info(f"Commands: {len(synced)} synced to guild {guild_id}")
            ok = True
        except discord.Forbidden:
            logger.error(f"❌ Commands: Discord won't let Plexbie add its commands to server {guild_id} (GUILD_ID). "
                         "Plexbie isn't in it, or was added without the applications.commands scope. Add it with "
                         "the setup page's link; the commands appear as soon as it joins.")
        except discord.HTTPException as e:
            logger.error(f"❌ Commands: couldn't add Plexbie's commands to server {guild_id}: {e}")
        await self._clear_stale_globals()
        return ok

    async def _clear_stale_globals(self) -> int:
        """Make sure the global scope is empty. Discord merges global and guild
        commands in the picker, so anything left there from an older version's
        global sync appears a second time (one deployment had 15 such duplicates),
        and in every other server Plexbie is in. Cheap and idempotent: after the
        first cleanup fetch_commands() returns nothing."""
        try:
            removed = await clear_global_commands(self)
        except discord.HTTPException as e:
            # Not worth failing startup over.
            logger.warning(f"Could not check for stale global commands: {e}")
            return 0
        if removed:
            logger.warning(f"Commands: removing {removed} stale global command(s); "
                           "Plexbie's commands live only in its household's server")
        return removed

    async def _settle_home_guild(self) -> None:
        """Once per start, when connected: say which server is home, or with GUILD_ID
        blank pick it when that can be proved (core.discord_lookup.pick_home_guild)."""
        if self._home_settled:
            return
        try:
            gid = self.config.guild_id
            if gid:
                guild = self.get_guild(gid)
                if guild is None:
                    logger.error(f"❌ {not_in_text(gid, self.guilds)}")
                else:
                    logger.info(f"🏠 Home server: {guild.name} ({gid})")
            else:
                guild, reason = await pick_home_guild(self, self.config)
                if guild is not None:
                    await self._adopt(guild, reason)
                else:
                    self._home_problem = problem_text(reason, self.guilds)
                    await self._clear_stale_globals()
                    logger.error(f"❌ Commands are OFF: {self._home_problem}")
            app = self.application      # fetched at login
            if app is not None and app.bot_public:
                logger.warning(f"⚠️  Public Bot is on: anyone who has Plexbie's ID can add it to their own server. "
                               f"{PUBLIC_BOT_FIX}")
        finally:
            self._home_settled = True

    async def _adopt(self, guild, reason: str) -> None:
        """Make `guild` home for good: this run (the shared config every check reads)
        and the next (config/.env)."""
        from portal.setup import write_env
        self.config.guild_id = guild.id
        os.environ["GUILD_ID"] = str(guild.id)
        self.config.forget_everyone_roles()
        self._home_problem = None
        try:
            await asyncio.to_thread(write_env, ENV_FILE, {"GUILD_ID": str(guild.id)})
            saved = f"saved GUILD_ID={guild.id} in config/.env"
        except OSError as e:
            saved = "is using it for this run"
            logger.warning(f"Couldn't save GUILD_ID to config/.env ({e}). Add GUILD_ID={guild.id} yourself, "
                           "or Plexbie decides again at the next start.")
        await self._sync_to_home(guild.id)
        self.dispatch("home_server", guild)     # e.g. the invite tracker loads its invites
        why = ("your channel and role settings are there" if reason == "settings"
               else "it is the only server Plexbie is in and the bot's owner owns it")
        logger.warning(f"🏠 GUILD_ID was blank, so Plexbie made '{guild.name}' ({guild.id}) its household's server "
                       f"({why}) and {saved}. Commands and admin buttons now work only there. Wrong server? "
                       "Change GUILD_ID and restart. If your container sets GUILD_ID itself, set it there too.")

    async def on_guild_join(self, guild: discord.Guild):
        """Plexbie was added to a server: home, a candidate for home, or someone else's.

        It never leaves a server by itself: the household may be moving there.
        """
        gid = self.config.guild_id
        if gid and guild.id == gid:
            logger.info(f"Plexbie joined its household's server '{guild.name}' ({guild.id})")
            await self._sync_to_home(guild.id)
            self.dispatch("home_server", guild)
            self._check_role_order()
            return
        if not gid:
            if not self._home_settled:
                return      # on_ready is about to decide, with this server included
            picked, reason = await pick_home_guild(self, self.config)
            if picked is not None:
                await self._adopt(picked, reason)
            else:
                self._home_problem = problem_text(reason, self.guilds)
                logger.warning(f"Plexbie was added to '{guild.name}' ({guild.id}). {self._home_problem}")
            return
        if guild.id in self._told_foreign:
            return
        self._told_foreign.add(guild.id)
        home = self.get_guild(gid)
        home_name = f"'{home.name}' ({gid})" if home else str(gid)

        def told(name: str) -> str:
            return (f"Plexbie was added to another Discord server: {name} ({guild.id}, owner {guild.owner_id}). "
                    f"It ignores commands and admin buttons there; your household's server is "
                    f"{home_name}. Moving your household there? Set GUILD_ID={guild.id} "
                    "on the setup page or in config/.env and restart. Didn't add it? Remove Plexbie from that "
                    "server and turn off Public Bot in the Discord Developer Portal.")
        logger.warning(f"⚠️  {told(repr(guild.name))}")
        now = time.monotonic()
        self._foreign_notices = [t for t in self._foreign_notices if now - t < 3600]
        if len(self._foreign_notices) >= FOREIGN_NOTICES_PER_HOUR:
            logger.info(f"Not posting about server {guild.id}: the admins were already told about "
                        f"{FOREIGN_NOTICES_PER_HOUR} other servers this hour (Manage → Health lists them all)")
            return
        self._foreign_notices.append(now)
        # The server's name is whatever its owner typed: shown as code, so no links,
        # markdown or mentions from it, and left out of the phone alert altogether.
        name = discord.utils.escape_mentions((guild.name or "").replace("`", "'")[:80])
        channel = admin_channel(self, self.config)
        if channel is not None:
            try:
                await channel.send(f"⚠️ {told(f'`{name}`')}")
            except discord.HTTPException as e:
                logger.debug(f"Couldn't tell the admin channel about server {guild.id}: {e}")
        from core.notify import alert_admins_soon
        alert_admins_soon(self, self.config, title="Plexbie was added to another server",
                          body=f"Server {guild.id}: it ignores commands there.",
                          url="/manage?tab=health", tag=f"guild-join-{guild.id}")

    async def on_guild_remove(self, guild: discord.Guild):
        if self.config.guild_id and guild.id == self.config.guild_id:
            logger.error(f"❌ Plexbie was removed from its household's server '{guild.name}' ({guild.id}). Commands "
                         "and admin buttons won't work until it's added back, or GUILD_ID names the new server and "
                         "Plexbie is restarted.")

    def _check_role_order(self) -> None:
        """Warn when Plexbie can't hand out its own roles (its role isn't above them)."""
        from core.role_order import HANDED_OUT, fix_text
        guild = home_guild(self, self.config)
        if not guild or not guild.me:
            return
        stuck = []
        for key in HANDED_OUT:
            rid = getattr(self.config, key.lower(), None)
            role = guild.get_role(int(rid)) if rid else None
            if role and role >= guild.me.top_role:
                stuck.append(role.name)
        if stuck:
            logger.warning(f"⚠️  Plexbie can't give out {', '.join(stuck)}. " + fix_text(stuck, guild.me.display_name))

    async def on_ready(self):
        """Bot is ready and connected"""
        logger.info("=" * 60)
        logger.info("🎉 PLEXBIE DISCORD BOT - READY")
        logger.info("=" * 60)
        logger.info(f"✅ Bot User: {self.user} (ID: {self.user.id})")
        logger.info(f"✅ Connected Guilds: {len(self.guilds)}")
        logger.info(f"✅ Loaded Plugins: {len(self.plugin_manager.loaded_cogs)}")
        try:
            await self._settle_home_guild()
        except Exception as e:
            logger.error(f"Couldn't work out the household's Discord server: {e}", exc_info=True)
        self._check_role_order()

        # List loaded plugins
        if self.plugin_manager.loaded_cogs:
            logger.info("   📦 Active Plugins:")
            for plugin_name in self.plugin_manager.loaded_cogs:
                plugin_meta = self.plugin_manager.plugins.get(plugin_name, {})
                version = plugin_meta.get('version', '1.0.0')
                logger.info(f"      • {plugin_name} v{version}")

        logger.info(f"✅ Webhook Server: Port {self.config.webhook_port}")
        home = home_guild(self, self.config)
        if home is not None and self._synced is not None:
            logger.info(f"✅ Commands: {self._synced} in {home.name}")
        else:
            logger.info("❌ Commands: OFF (see the error above, or Manage → Health)")
        logger.info("=" * 60)
        logger.info("🚀 All systems operational!")
        logger.info("=" * 60)

        # Set presence
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Plex Media Server"
            )
        )
    
    async def close(self):
        """Free the website and webhook ports too, so setup can reopen on them."""
        for stop in (getattr(getattr(self, "portal_runner", None), "cleanup", None),
                     getattr(getattr(self, "webhook_server", None), "stop", None)):
            if stop:
                try:
                    await stop()
                except Exception as e:
                    logger.warning(f"Shutdown: {e}")
        await super().close()

    async def on_message(self, message):
        """Only slash commands, so messages aren't parsed as text commands. Otherwise every
        "@Plexbie ..." in any server or DM logs a CommandNotFound at ERROR. DMs still reach
        portal/inbox.on_dm, a listener of its own, and bot.wait_for still sees them."""

    async def on_command_error(self, ctx, error):
        """Global error handler"""
        logger.error(f"Command error: {error}", exc_info=error)


ENV_FILE = Path("config/.env")
ENV_TEMPLATE = Path("/app/defaults/.env.example")


async def wait_for_settings(problem: str = None) -> None:
    """First start: put config/.env in place and get the settings that are needed.

    A fresh install (an empty config folder) gets the documented template copied
    in. While the Discord bot token or the Plex address and token are missing,
    the website's port serves a setup page (portal/setup.py) that explains each
    key, tests it and saves it; finishing it carries straight on with start-up.
    With the website off (WEB_PORT=0) the file is edited by hand instead and
    checked every 15 seconds. Settings passed as container environment variables
    count too. `problem` reopens setup with a reason, e.g. a token Discord refused.
    """
    from portal import setup

    if not ENV_FILE.exists() and ENV_TEMPLATE.exists():
        try:
            ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copyfile, ENV_TEMPLATE, ENV_FILE)
            print("📝 First start: created config/.env from the template.", flush=True)
        except OSError as e:
            print(f"Could not create config/.env ({e}); create it yourself from config/.env.example.", flush=True)
    if not (problem or setup.needs_setup()):
        return
    try:
        port = int(os.getenv("WEB_PORT") or 7979)
    except ValueError:
        port = 7979
    if port > 0:
        try:
            await setup.run(ENV_FILE, port, os.getenv("WEB_BIND") or "0.0.0.0", problem)
            return
        except OSError as e:
            print(f"Could not open the setup page on port {port} ({e}); edit config/.env instead.", flush=True)
    print("=" * 60, flush=True)
    print("👋 Plexbie needs its settings before it can start.", flush=True)
    if problem:
        print(f"   {problem}", flush=True)
    print("   Open config/.env (in the container's config folder), fill in at least", flush=True)
    print("   DISCORD_BOT_TOKEN, PLEX_URL and PLEX_TOKEN, and GUILD_ID (your server's ID; Plexbie", flush=True)
    print("   picks it itself only when that's unambiguous and you own that server), and save it.", flush=True)
    print("   Plexbie checks every 15 seconds and starts by itself.", flush=True)
    print("=" * 60, flush=True)
    before = os.getenv("DISCORD_BOT_TOKEN")
    while setup.needs_setup() or (problem and os.getenv("DISCORD_BOT_TOKEN") == before):
        await asyncio.sleep(15)
        load_dotenv(ENV_FILE, override=True)


#: Waits between tries while Discord can't be reached (no network yet, DNS down).
OFFLINE_RETRY = (10, 20, 30, 60, 120, 300)


async def ensure_webhook_secrets() -> None:
    """Every webhook route gets a secret, made and saved on start if it has none.

    A route without one can't tell who's calling, and "only this machine can reach
    it" isn't a safe stand-in: a tunnel or proxy on this machine forwards strangers
    from 127.0.0.1 too. Set in the container's environment, a secret is kept as is.
    """
    from core.webhook_connect import SECRET_KEYS, new_secrets
    from portal.setup import write_env
    made = new_secrets({k: os.getenv(k, "") for k in SECRET_KEYS})
    if not made:
        return
    try:
        if ENV_FILE.exists():
            await asyncio.to_thread(write_env, ENV_FILE, made)
    except OSError as e:
        print(f"Could not save new webhook secrets to config/.env ({e}); they last until a restart.", flush=True)
    os.environ.update(made)
    print(f"🔑 Made webhook secrets for {', '.join(k.split('_')[0].title() for k in made)} "
          f"(config/.env). Apps that send webhooks to Plexbie need them.", flush=True)


async def main():
    """Main entry point"""
    # Load environment variables from .env file (renamed settings get their new names first)
    from core.config import rename_env_keys
    try:
        for old in rename_env_keys(ENV_FILE):
            print(f"config/.env: {old} is now called {old.replace('OVERSEERR_', 'SEERR_')}", flush=True)
    except OSError as e:
        print(f"Could not update setting names in config/.env ({e}); the old names still work.", flush=True)
    load_dotenv(ENV_FILE)
    await wait_for_settings()
    await ensure_webhook_secrets()

    # Setup logging
    setup_logging()

    services = None
    offline = 0
    while True:
        try:
            logger.info("=" * 60)
            logger.info("🤖 PLEXBIE - Starting Up...")
            logger.info("=" * 60)

            # Load configuration
            config = Config()
            logger.info("✅ Configuration loaded")

            # Initialize database
            await init_database(config.db_url)
            logger.info("✅ Database initialized (SQLite)")

            # Create services container
            services = BotServices(config)
            await services.initialize()
            logger.info("✅ Services initialized")

            logger.info("=" * 60)

            # Create and run bot
            bot = Plexbie(config, services)

            async with bot:
                await bot.start(config.discord_bot_token)
            break

        except discord.LoginFailure:
            # A wrong or reset token: ask again in the browser rather than crash-looping.
            logger.error("❌ Discord turned down the bot token.")
            await _close_quietly(services)
            await wait_for_settings("Discord turned down the bot token. Paste a fresh one.")
        except discord.PrivilegedIntentsRequired:
            logger.error("❌ The bot's Server Members intent is off.")
            await _close_quietly(services)
            await wait_for_settings("Turn on Server Members Intent for the bot "
                                    "(Discord developer portal, Bot page), then press Finish.")
        except (OSError, asyncio.TimeoutError, discord.GatewayNotFound) as e:
            # No network yet (Unraid starts containers before it's up) or Discord is
            # down: keep trying. Exiting left Plexbie, website included, stopped for
            # good, since Unraid's autostart doesn't restart a container that quits.
            wait = OFFLINE_RETRY[min(offline, len(OFFLINE_RETRY) - 1)]
            offline += 1
            logger.warning(f"Can't reach Discord ({e}); trying again in {wait}s")
            await _close_quietly(services)
            services = None
            await asyncio.sleep(wait)
        except KeyboardInterrupt:
            logger.info("Received shutdown signal")
            break
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=e)
            sys.exit(1)
    logger.info("Shutdown complete")


async def _close_quietly(services) -> None:
    close = getattr(services, "close", None) or getattr(services, "cleanup", None)
    if close:
        try:
            await close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
