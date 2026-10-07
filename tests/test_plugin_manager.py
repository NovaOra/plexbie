# path: tests/test_plugin_manager.py
"""Plugin cog-name derivation, and what a plugin that fails to load leaves behind.

Regression coverage for: the loader derived the cog class name with a PascalCase
join while the (since removed) unloader used str.title(), producing
"Watch_TrackingCog" for any multi-word plugin, so the two never agreed.

And: six cogs started their background loops in __init__, before add_cog. When
add_cog then failed (a database error in cog_load, or a command name another
plugin already registered), discord.py did not call cog_unload, so the loops ran
on a cog the bot never registered, with nothing left to stop them. Among them
were the daily inactivity check that removes Plex users and the daily cleanup.
"""
import asyncio
import contextlib
import importlib
import pathlib
import tempfile

import conftest  # noqa: F401
import discord
from discord import app_commands
from discord.ext import commands, tasks
from helpers import FakeServices

from core.config import Config
from core.plugin_manager import PluginManager, cog_class_name

PLUGINS_DIR = pathlib.Path(conftest.PROJECT_ROOT) / "plugins"


# --- the name derivation ---

def test_multi_word_plugin_name():
    assert cog_class_name("watch_tracking") == "WatchTrackingCog"


def test_three_word_plugin_name():
    assert cog_class_name("bookshelf_processor_x") == "BookshelfProcessorXCog"


def test_single_word_plugin_name():
    assert cog_class_name("status") == "StatusCog"


def test_title_case_bug_is_not_reintroduced():
    """str.title() produced Watch_TrackingCog - the underscore is the tell."""
    assert "_" not in cog_class_name("watch_tracking")


def test_derivation_matches_every_real_plugin():
    """The real cog classes must be findable by the derived name."""
    import importlib
    import pathlib

    plugins_dir = pathlib.Path(conftest.PROJECT_ROOT) / "plugins"
    checked = 0
    for plugin_dir in sorted(plugins_dir.iterdir()):
        if not plugin_dir.is_dir() or not (plugin_dir / "cog.py").exists():
            continue
        module = importlib.import_module(f"plugins.{plugin_dir.name}.cog")
        expected = cog_class_name(plugin_dir.name)
        assert hasattr(module, expected), (
            f"plugins/{plugin_dir.name}/cog.py has no class {expected}"
        )
        checked += 1
    assert checked >= 11, f"expected to check the full plugin set, got {checked}"


# --- background loops: started by cog_load, and stopped when the load fails ---

class _Bot(commands.Bot):
    """A real Bot, so add_cog runs discord.py's own load steps, that never gets
    ready: a loop with a before_loop waits there instead of reaching Plex or the
    database (the scenarios stub what the others would touch). Keeps every cog it
    was offered."""

    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.none())
        self.offered = []

    async def add_cog(self, cog, **kwargs):
        self.offered.append(cog)
        await super().add_cog(cog, **kwargs)

    async def wait_until_ready(self):
        await asyncio.Event().wait()


def _loops(cog):
    """The cog's own tasks.Loop instances, by name."""
    names = {name for klass in type(cog).__mro__ for name, value in vars(klass).items()
             if isinstance(value, tasks.Loop)}
    return {name: getattr(cog, name) for name in sorted(names)}


def _looping_plugins():
    found = []
    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        if not (plugin_dir / "cog.py").exists():
            continue
        module = importlib.import_module(f"plugins.{plugin_dir.name}.cog")
        cog_class = getattr(module, cog_class_name(plugin_dir.name))
        if any(isinstance(value, tasks.Loop) for klass in cog_class.__mro__ for value in vars(klass).values()):
            found.append(plugin_dir.name)
    return found


async def _settle():
    """Let cancelled loop tasks finish."""
    for _ in range(5):
        await asyncio.sleep(0)


async def _running(cog):
    await _settle()
    running = [name for name, loop in _loops(cog).items() if loop.is_running()]
    for name in running:                         # tidy up, so a failure doesn't leak loops into later tests
        _loops(cog)[name].cancel()
    await _settle()
    return running


@contextlib.contextmanager
def _patched(module_name, **replacements):
    module = importlib.import_module(module_name)
    saved = {name: getattr(module, name) for name in replacements}
    for name, value in replacements.items():
        setattr(module, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(module, name, value)


async def _not_live():
    return False


async def _database_locked():
    raise RuntimeError("database is locked")


def test_every_looping_plugin_is_checked():
    assert {"user_mgmt", "new_media_added", "watch_tracking", "watch_party",
            "media_cleanup", "service_health", "bookshelf_processor"} <= set(_looping_plugins())


def test_building_a_cog_starts_no_loop():
    """Only cog_load may start them: it runs inside add_cog, after anything that can fail."""
    async def scenario():
        started = {}
        for plugin in _looping_plugins():
            module = importlib.import_module(f"plugins.{plugin}.cog")
            cog = getattr(module, cog_class_name(plugin))(_Bot(), FakeServices(Config()))
            started[plugin] = await _running(cog)
        return {plugin: names for plugin, names in started.items() if names}

    with tempfile.TemporaryDirectory() as tmp, \
            _patched("plugins.watch_tracking.cog", STREAKS_FILE=pathlib.Path(tmp) / "watch_streaks.json"):
        started = asyncio.run(scenario())
    assert started == {}, f"loops started before add_cog: {started}"


def test_cog_load_starts_every_loop_and_cog_unload_stops_them():
    async def scenario(plugin, tmp):
        module = importlib.import_module(f"plugins.{plugin}.cog")
        cog = getattr(module, cog_class_name(plugin))(_Bot(), FakeServices(Config()))
        if plugin == "new_media_added":
            async def loaded():
                return None
            cog.load_tracking_data = loaded          # cleanup_old_batches has no before_loop
        if plugin == "media_cleanup":
            async def loaded():
                return True
            cog.load_data = loaded
        if plugin == "bookshelf_processor":
            async def nothing_waiting():
                return None
            cog._seed_existing_items = nothing_waiting
            cog.cache_dir = pathlib.Path(tmp) / "bookshelf"
        await cog.cog_load()
        never_started = [name for name, loop in _loops(cog).items() if loop.get_task() is None]
        await discord.utils.maybe_coroutine(cog.cog_unload)
        return never_started, await _running(cog)

    with tempfile.TemporaryDirectory() as tmp, \
            _patched("plugins.watch_tracking.cog", STREAKS_FILE=pathlib.Path(tmp) / "watch_streaks.json"), \
            _patched("webhooks.tautulli_handler", recently_live=_not_live):
        for plugin in _looping_plugins():
            never_started, still_running = asyncio.run(scenario(plugin, tmp))
            assert never_started == [], f"{plugin}: cog_load didn't start {never_started}"
            assert still_running == [], f"{plugin}: cog_unload left {still_running} running"


def _load(plugin, make_bot=_Bot):
    """load_plugin on a real plugin; returns (the loops it left running, whether it loaded, the bot)."""
    async def scenario():
        bot = make_bot()
        manager = PluginManager(bot, FakeServices(Config()))
        await manager.load_plugin(PLUGINS_DIR / plugin)
        (cog,) = bot.offered
        return await _running(cog), plugin in manager.loaded_cogs, bot
    return asyncio.run(scenario())


def _taken(command_name):
    """A bot on which another plugin already registered /<command_name>."""
    def make():
        bot = _Bot()

        @app_commands.command(name=command_name, description="Another plugin's command")
        async def taken(interaction: discord.Interaction):
            pass
        bot.tree.add_command(taken)
        bot.taken = taken
        return bot
    return make


def _registered(bot, cog):
    """The cog's own listeners and slash commands that are on the bot."""
    listeners = [name for name, listener in cog.get_listeners()
                 if listener in bot.extra_events.get(name, [])]
    commands_left = [command.name for command in cog.__cog_app_commands__
                     if bot.tree.get_command(command.name) is command]
    return listeners, commands_left


def _clash(plugin, command_name):
    with _patched("webhooks.tautulli_handler", recently_live=_not_live):
        running, loaded, bot = _load(plugin, _taken(command_name))
    assert not loaded
    assert running == [], f"left running: {running}"
    left = _registered(bot, bot.offered[0])
    assert left == ([], []), f"left registered: {left}"
    assert bot.tree.get_command(command_name) is bot.taken, "the other plugin's command was removed"


def test_a_failed_cog_load_leaves_no_loop_running():
    """The inactivity check that removes Plex users must not run for a plugin that didn't load."""
    with _patched("webhooks.tautulli_handler", recently_live=_database_locked):
        running, loaded, _ = _load("user_mgmt")
    assert not loaded
    assert running == [], f"left running: {running}"


def test_a_command_name_clash_leaves_no_loop_running():
    """discord.py registers the commands after cog_load and doesn't unload on a clash."""
    _clash("user_mgmt", "remove-user")


def test_a_clash_on_a_later_command_takes_back_the_earlier_ones():
    """/remove-user and /list-tracked-users are registered before /manage-links clashes."""
    _clash("user_mgmt", "manage-links")


def test_a_clash_takes_back_the_cogs_listeners():
    """watch_party's voice listener would keep recording parties for a plugin that didn't load."""
    _clash("watch_party", "watchparty-active")


def test_a_failed_second_copy_leaves_the_loaded_one_alone():
    """Taking back only this cog's own listeners and commands, never a namesake's."""
    async def scenario():
        bot = _Bot()
        manager = PluginManager(bot, FakeServices(Config()))
        await manager.load_plugin(PLUGINS_DIR / "watch_party")
        await manager.load_plugin(PLUGINS_DIR / "watch_party")   # "already loaded"
        first, second = bot.offered
        still_loaded = bot.get_cog(first.qualified_name) is first
        registered = _registered(bot, first)
        return still_loaded, registered, await _running(first), await _running(second)

    still_loaded, (listeners, commands_left), first_running, second_running = asyncio.run(scenario())
    assert still_loaded
    assert listeners == ["on_voice_state_update"]
    assert sorted(commands_left) == ["watchparty-active", "watchparty-stats"]
    assert first_running == ["accumulate_credits", "refresh_linked_users_cache"]
    assert second_running == []


def test_a_loaded_plugin_keeps_its_loops():
    with _patched("webhooks.tautulli_handler", recently_live=_not_live):
        running, loaded, _ = _load("user_mgmt")
    assert loaded
    assert running == ["auto_link_users", "check_inactive_users"]
