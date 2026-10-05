# path: bot.py
"""Plexbie - Modular Discord Bot for Plex Management"""
import asyncio
import os
import shutil
import sys
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
from core.discord_lookup import home_guild
from core.logging import setup_logging, get_logger
from core.plugin_manager import PluginManager
from core.services import BotServices
from core.webhooks import WebhookServer
from database.session import init_database

logger = get_logger(__name__)


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
        )
        
        self.config = config
        self.services = services
        self.plugin_manager = PluginManager(self, services)
        self.webhook_server = WebhookServer(services)
    
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

        # Sync slash commands. Guild-scoped only: a guild sync is immediate,
        # whereas global commands take up to an hour to propagate.
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                f"Commands: {len(synced)} synced to guild {self.config.guild_id}"
            )

            # Then make sure the global scope is empty. Discord merges global and
            # guild commands in the picker, so anything left there from an earlier
            # global sync appears a second time - this deployment had accumulated
            # 15 such duplicates, which nothing in the code ever removed. Cheap and
            # idempotent: after the first cleanup fetch_commands() returns nothing.
            try:
                stale = await self.tree.fetch_commands()
                if stale:
                    logger.warning(
                        f"Commands: removing {len(stale)} stale global command(s) "
                        f"that duplicate the guild-scoped ones"
                    )
                    self.tree.clear_commands(guild=None)
                    await self.tree.sync()
            except discord.HTTPException as e:
                # Not worth failing startup over; the duplicates are cosmetic.
                logger.warning(f"Could not check for stale global commands: {e}")
        else:
            synced = await self.tree.sync()
            logger.info(f"Commands: {len(synced)} synced globally")
    
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
        self._check_role_order()

        # List loaded plugins
        if self.plugin_manager.loaded_cogs:
            logger.info("   📦 Active Plugins:")
            for plugin_name in self.plugin_manager.loaded_cogs:
                plugin_meta = self.plugin_manager.plugins.get(plugin_name, {})
                version = plugin_meta.get('version', '1.0.0')
                logger.info(f"      • {plugin_name} v{version}")

        logger.info(f"✅ Webhook Server: Port {self.config.webhook_port}")
        logger.info("✅ Commands: Synced and ready to use")
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
    print("   DISCORD_BOT_TOKEN, PLEX_URL and PLEX_TOKEN, and save it.", flush=True)
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
