# path: core/plugin_manager.py
"""Plugin discovery and management system"""
import importlib
import json
from pathlib import Path
from typing import Dict, List

import discord
from discord.ext import commands

from core.logging import get_logger
from core.services import BotServices

logger = get_logger(__name__)


def cog_class_name(plugin_name: str) -> str:
    """Map a plugin directory name to its cog class name.

    watch_tracking -> WatchTrackingCog

    One place on purpose: a str.title() copy once yielded "Watch_TrackingCog"
    for multi-word plugins, which matched no cog.
    """
    return "".join(word.capitalize() for word in plugin_name.split("_")) + "Cog"


class PluginManager:
    """Manages plugin discovery, loading, and lifecycle"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        self.plugins: Dict[str, dict] = {}
        self.loaded_cogs: List[str] = []
    
    async def load_all_plugins(self):
        """Discover and load all plugins from plugins directory"""
        plugins_dir = Path("plugins")

        if not plugins_dir.exists():
            logger.warning(f"⚠️  Plugins directory not found at: {plugins_dir.absolute()}")
            return

        plugin_count = 0
        for plugin_dir in plugins_dir.iterdir():
            if not plugin_dir.is_dir():
                continue

            plugin_count += 1
            await self.load_plugin(plugin_dir)

        logger.info(f"✅ Plugins: Loaded {len(self.loaded_cogs)}/{plugin_count} available plugins")
    
    async def load_plugin(self, plugin_dir: Path):
        """Load a single plugin"""
        plugin_name = plugin_dir.name
        
        # Check for required files
        plugin_json = plugin_dir / "plugin.json"
        cog_py = plugin_dir / "cog.py"
        
        if not plugin_json.exists():
            logger.warning(f"Plugin {plugin_name} missing plugin.json")
            return
        
        if not cog_py.exists():
            logger.warning(f"Plugin {plugin_name} missing cog.py")
            return
        
        try:
            # Load plugin metadata
            with open(plugin_json) as f:
                metadata = json.load(f)
            
            self.plugins[plugin_name] = metadata
            
            # Check if enabled
            if not metadata.get("enabled", True):
                # Disabled plugins are silently skipped
                return

            # Import and load cog
            import_path = f"plugins.{plugin_name}.cog"

            # Dynamic import
            module = importlib.import_module(import_path)

            # Get cog class (e.g., watch_tracking -> WatchTrackingCog)
            cog_class = getattr(module, cog_class_name(plugin_name), None)

            if cog_class:
                # Initialize cog with services
                cog_instance = cog_class(self.bot, self.services)

                # Add to bot
                try:
                    await self.bot.add_cog(cog_instance)
                except Exception:
                    # add_cog runs cog_load, then adds the cog's listeners, then
                    # its slash commands one at a time. It doesn't undo any of
                    # that or call cog_unload when cog_load fails or a slash
                    # command name is already taken. Take back the listeners and
                    # the commands this cog added (only where the tree holds this
                    # cog's own command, never the other plugin's of the same
                    # name), then stop its loops.
                    for name, listener in cog_instance.get_listeners():
                        self.bot.remove_listener(listener, name)
                    for command in cog_instance.__cog_app_commands__:
                        if self.bot.tree.get_command(command.name) is command:
                            self.bot.tree.remove_command(command.name)
                    try:
                        await discord.utils.maybe_coroutine(cog_instance.cog_unload)
                    except Exception as e:
                        logger.error(f"Plugin {plugin_name}: stopping it after the failed load failed: {e}")
                    raise
                self.loaded_cogs.append(plugin_name)
                # Suppress individual plugin load messages
            else:
                logger.warning(f"⚠️  Plugin {plugin_name}: No cog class found")
                
        except discord.app_commands.CommandAlreadyRegistered as e:
            # Two plugins claiming the same top-level command name. The generic
            # message below says only "Failed to load plugin X", which sends you
            # looking for a bug in X rather than at the name it collides with.
            logger.error(
                f"Plugin {plugin_name} was not loaded: it defines the command "
                f"/{e.name}, which another loaded plugin already registered. "
                f"Rename one of them, or enable only one of the two."
            )
        except Exception as e:
            logger.error(f"Failed to load plugin {plugin_name}: {e}", exc_info=e)
    

    async def register_webhook_routes(self, webhook_server):
        """Allow plugins to register webhook routes before server starts"""
        for cog in self.bot.cogs.values():
            if hasattr(cog, "register_webhook_routes"):
                try:
                    await cog.register_webhook_routes(webhook_server)
                except Exception as e:
                    logger.error(f"Error registering webhook routes for {cog.__class__.__name__}: {e}")
