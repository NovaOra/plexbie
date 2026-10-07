# path: tests/test_cleanup_settings_load.py
"""Cleanup settings: nothing reads, changes or saves them before the saved ones load.

After a restart the cleanup cog held DEFAULT_CONFIG until something loaded the
stored settings, and the daily loop only loads once a day. `/cleanup config`
saved straight away, so changing one field wrote the defaults over all the rest:
the kept-forever titles and the skipped libraries were emptied, and a switched-off
cleanup came back on. The panel's status and Run Scan buttons read the same
defaults, so Run Scan went ahead against a stored "off". A failed database read
was swallowed and left those defaults in place for everything that followed.

The cog is built for real inside the event loop, as it is just after a restart,
and cog_load is deliberately not run: that is the window these tests are about.
"""
import ast
import asyncio
import contextlib
import copy
import logging
import pathlib
import tempfile
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401
from helpers import FakeClient, FakeMember, FakeServices, PermConfig

from plugins.media_cleanup import cog as cleanup
from plugins.media_cleanup.cog import CleanupControlPanel, CleanupSettingsView, MediaCleanupCog

ROOT = pathlib.Path(__file__).resolve().parent.parent
NS = "media_cleanup"
DEFAULTS = copy.deepcopy(cleanup.DEFAULT_CONFIG)

#: What a household has chosen: cleanup off, live, one title kept, one library skipped.
STORED = {
    "enabled": False,
    "dry_run": False,
    "inactivity_days": 120,
    "notify_days_before": 14,
    "exclude_libraries": ["Home Videos"],
    "exempt_items": {"123": {"title": "Kept Film", "type": "movie"}},
    "notification_channel_id": 555,
}

ADMIN = {"user": {"id": "1", "name": "Pat", "via": "discord"}, "member": True, "admin": True}


# --- fakes ---

class _Bot:
    def add_view(self, view):
        pass

    async def wait_until_ready(self):
        await asyncio.Event().wait()     # the hourly loop never gets going in a test


class _Response:
    def __init__(self, sent):
        self.sent, self.done = sent, False

    def is_done(self):
        return self.done

    async def defer(self, ephemeral=False, thinking=False):
        self.done = True

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False):
        self.done = True
        self.sent.append((content, embed, ephemeral))


class _Followup:
    def __init__(self, sent):
        self.sent = sent

    async def send(self, content=None, *, embed=None, ephemeral=False):
        self.sent.append((content, embed, ephemeral))


class _Interaction:
    """An admin (the bot owner) pressing a button or running a command."""

    def __init__(self):
        self.sent = []                   # (content, embed, ephemeral), whichever way it went
        self.response = _Response(self.sent)
        self.followup = _Followup(self.sent)
        self.user = FakeMember(111)
        self.client = FakeClient(PermConfig())
        self.command = None
        self.guild = None


class _Channel:
    def __init__(self, cid):
        self.id, self.mention = cid, f"<#{cid}>"


class _CaptureLog:
    """Collect what the cleanup plugin logs for the duration of a block."""

    def __init__(self):
        self.records = []

    def __enter__(self):
        self.logger = logging.getLogger(cleanup.__name__)
        self.handler = logging.Handler()
        self.handler.emit = self.records.append
        self.logger.addHandler(self.handler)
        self.previous_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.previous_level)

    @property
    def messages(self):
        return [record.getMessage() for record in self.records]


@contextlib.contextmanager
def _patched(**replacements):
    """Swap names in the cleanup module (kv_get, kv_set) for the block."""
    saved = {name: getattr(cleanup, name) for name in replacements}
    for name, value in replacements.items():
        setattr(cleanup, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(cleanup, name, value)


async def _unreadable(namespace, key, default=None):
    raise RuntimeError("database is locked")


def _recorder():
    writes = []

    async def kv_set(namespace, key, value):
        writes.append((key, copy.deepcopy(value)))
    return writes, kv_set


def _fresh_cog():
    """The cog just after a restart: constructed, nothing loaded yet. Needs a running loop."""
    services = FakeServices(PermConfig())
    services.plex_server = None
    return MediaCleanupCog(_Bot(), services)


def _fields(embed):
    return {field.name: field.value for field in embed.fields}


async def _init(path):
    import database.session as session_module

    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


async def _dispose():
    import database.session as session_module

    if session_module.engine is not None:
        await session_module.engine.dispose()


def _run(body, stored=STORED, tracking=None):
    """Seed a throwaway database, run `body(cog)` on a fresh cog; return (body's result, stored config)."""
    from database.kv_store import kv_get, kv_set

    path = pathlib.Path(tempfile.mkdtemp()) / "cleanup.db"

    async def scenario():
        await _init(path)
        try:
            if stored is not None:
                await kv_set(NS, "config", stored)
            if tracking is not None:
                await kv_set(NS, "tracking", tracking)
            cog = _fresh_cog()
            try:
                result = await body(cog)
            finally:
                cog.cog_unload()
            return result, await kv_get(NS, "config")
        finally:
            await _dispose()

    return asyncio.run(scenario())


# --- what an admin can do on a freshly restarted bot ---

async def _config(cog, ix, **kwargs):
    await MediaCleanupCog.cleanup_config.callback(cog, ix, **kwargs)


async def _toggle_dry_run(cog, ix):
    await CleanupControlPanel.toggle_dry_run_button(CleanupControlPanel(cog), ix, None)


async def _toggle_enabled(cog, ix):
    await CleanupSettingsView.toggle_enabled(CleanupSettingsView(cog), ix, None)


async def _status(cog, ix):
    await CleanupControlPanel.status_button(CleanupControlPanel(cog), ix, None)


async def _run_scan(cog, ix):
    await CleanupControlPanel.run_button(CleanupControlPanel(cog), ix, None)


async def _exempt_remove(cog, ix):
    await MediaCleanupCog.cleanup_exempt_remove.callback(cog, ix, "Kept")


async def _panel(cog, ix):
    await MediaCleanupCog.cleanup_panel.callback(cog, ix)


# --- the restart window ---

def test_config_command_on_a_fresh_cog_changes_only_the_given_field():
    async def body(cog):
        await _config(cog, _Interaction(), inactivity_days=60)

    _, stored = _run(body)
    assert stored == {**STORED, "inactivity_days": 60}, (
        "/cleanup config wrote the defaults over the saved settings "
        "(kept titles, skipped libraries, live mode, the off switch)"
    )


def test_config_command_on_a_fresh_cog_changes_only_the_channel():
    async def body(cog):
        await _config(cog, _Interaction(), notification_channel=_Channel(777))

    _, stored = _run(body)
    assert stored == {**STORED, "notification_channel_id": 777}


def test_run_button_on_a_fresh_cog_honours_a_stored_off_switch():
    scans = []

    async def body(cog):
        async def run_cleanup_scan(interaction):
            scans.append(True)
        cog.run_cleanup_scan = run_cleanup_scan
        ix = _Interaction()
        await _run_scan(cog, ix)
        return ix

    ix, stored = _run(body)
    assert scans == [], "Run Scan went ahead on the defaults while the saved settings say cleanup is off"
    assert any("currently disabled" in (content or "") for content, _, _ in ix.sent)
    assert stored == STORED


def test_status_button_on_a_fresh_cog_shows_the_saved_settings():
    async def body(cog):
        ix = _Interaction()
        await _status(cog, ix)
        return ix

    ix, _ = _run(body)
    embeds = [embed for _, embed, _ in ix.sent if embed is not None]
    assert len(embeds) == 1, ix.sent
    fields = _fields(embeds[0])
    assert fields["Status"] == "❌ Disabled"
    assert fields["Mode"] == "🔴 Live Mode", "a restarted panel showed practice mode while the saved mode was live"
    assert fields["Inactivity Threshold"] == "120 days"
    assert fields["Excluded Libraries"] == "• Home Videos"
    assert fields["Exempt Media"] == "1 item(s)"


# --- a database that can't be read ---

def test_a_failed_load_refuses_and_saves_nothing():
    actions = {
        "/cleanup config": lambda cog, ix: _config(cog, ix, inactivity_days=60),
        "toggle dry run": _toggle_dry_run,
        "toggle enabled": _toggle_enabled,
        "/cleanup exempt remove": _exempt_remove,
        "/cleanup panel": _panel,
        "status": _status,
    }
    for name, action in actions.items():
        writes, kv_set = _recorder()

        async def scenario():
            cog = _fresh_cog()
            try:
                ix = _Interaction()
                with _patched(kv_get=_unreadable, kv_set=kv_set):
                    await action(cog, ix)
                return cog, ix
            finally:
                cog.cog_unload()

        cog, ix = asyncio.run(scenario())
        assert writes == [], f"{name}: saved {writes} without the saved settings"
        assert cog.config == DEFAULTS, f"{name}: changed settings it never loaded"
        assert cog._data_loaded is False, name
        assert [(content, ephemeral) for content, embed, ephemeral in ix.sent] == [(cleanup.SETTINGS_UNREADABLE, True)], (
            f"{name}: answered {ix.sent} instead of saying the settings couldn't be read"
        )


def test_save_config_refuses_before_any_load():
    writes, kv_set = _recorder()

    async def scenario():
        cog = _fresh_cog()
        try:
            with _patched(kv_set=kv_set):
                return await cog.save_config()
        finally:
            cog.cog_unload()

    saved = asyncio.run(scenario())
    assert writes == [], "save_config wrote the defaults before anything was loaded"
    assert saved is False


def test_a_corrupt_stored_config_is_a_failed_load_not_defaults():
    async def body(cog):
        ix = _Interaction()
        await _config(cog, ix, inactivity_days=60)
        return ix, await cog.load_data()

    (ix, loaded), stored = _run(body, stored="oops")
    assert stored == "oops", "a corrupt row was replaced with the defaults, which switch cleanup on"
    assert loaded is False
    assert [content for content, _, _ in ix.sent] == [cleanup.SETTINGS_UNREADABLE]

    (ix, loaded), stored = _run(body, tracking="oops")
    assert stored == STORED, "a corrupt tracking row let the defaults through"
    assert loaded is False
    assert [content for content, _, _ in ix.sent] == [cleanup.SETTINGS_UNREADABLE]


def test_daily_check_skips_and_retries_when_settings_cannot_be_read():
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    store, scans, loads = {"last_check": long_ago}, [], [False]

    async def kv_get(namespace, key, default=None):
        return store.get(key, default)

    async def kv_set(namespace, key, value):
        store[key] = value

    async def load_data():
        return loads[-1]

    async def enforce_request_monitor_cleanup():
        scans.append(True)
        raise RuntimeError("the scan reached Plex")

    class Services:
        plex_server = object()

    cog = object.__new__(MediaCleanupCog)
    cog.load_data, cog.enforce_request_monitor_cleanup = load_data, enforce_request_monitor_cleanup
    cog.services = Services()
    cog.config = copy.deepcopy(DEFAULTS)          # what a cog holds before it loads: cleanup on
    check = lambda: asyncio.run(MediaCleanupCog.daily_cleanup_check.coro(cog))

    with _patched(kv_get=kv_get, kv_set=kv_set), _CaptureLog() as log:
        check()
    assert scans == [], "the daily check scanned on the defaults while the saved settings couldn't be read"
    assert store["last_check"] == long_ago, "a failed read used up the day instead of retrying next hour"
    assert any("Skipped the daily cleanup check" in m for m in log.messages), log.messages

    loads.append(True)
    cog.config = {**DEFAULTS, "enabled": False}  # what the load would have brought in
    with _patched(kv_get=kv_get, kv_set=kv_set):
        check()
    assert scans == [] and store["last_check"] != long_ago


# --- the website goes through the same cog ---

def test_portal_refuses_when_cleanup_settings_cannot_be_read():
    from core.config import Config
    from portal.actions import Actions

    class Data:
        class cache:
            @staticmethod
            def drop(key):
                pass

        async def countdown(self):
            return {"5": {"title": "Old Film", "type": "movie"}}

    async def scenario():
        cog = _fresh_cog()
        scans = []

        async def enforce_request_monitor_cleanup():
            scans.append(True)
            return {}
        cog.services.plex_server = object()
        cog.enforce_request_monitor_cleanup = enforce_request_monitor_cleanup
        cog._scan_libraries_for_cleanup = lambda warned=None: ([], [])

        class Bot:
            def get_cog(self, name):
                return cog if name == "MediaCleanupCog" else None

        actions = Actions(bot=Bot(), services=FakeServices(Config()), data=Data(), public_url="https://plex.example.com")
        writes, kv_set = _recorder()
        try:
            with _patched(kv_get=_unreadable, kv_set=kv_set):
                settings = await actions.cleanup_settings(ADMIN, {"enabled": True})
                keep = await actions.exempt(ADMIN, {"ratingKey": "5", "keep": True})
                scan = await actions.cleanup_scan(ADMIN)
        finally:
            cog.cog_unload()
        return writes, scans, cog.config, settings, keep, scan

    writes, scans, config, settings, keep, scan = asyncio.run(scenario())
    assert writes == [], "the website saved cleanup settings it never loaded"
    assert scans == [], "the website ran a scan on the defaults"
    assert config == DEFAULTS
    for result in (settings, keep):
        assert result["ok"] is False and result["message"].startswith("Couldn't read the saved cleanup settings"), result
    assert scan["ok"] is False, scan
    assert scan["message"].startswith("Couldn't read the saved cleanup settings") and "nothing was scanned" in scan["message"]


def _website(cog, countdown=None):
    """The website's Actions, driving `cog`."""
    from core.config import Config
    from portal.actions import Actions

    class Data:
        class cache:
            @staticmethod
            def drop(key):
                pass

        async def countdown(self):
            return await countdown() if countdown else {}

    class Bot:
        def get_cog(self, name):
            return cog if name == "MediaCleanupCog" else None

    return Actions(bot=Bot(), services=FakeServices(Config()), data=Data(), public_url="https://plex.example.com")


def test_a_refused_website_change_leaves_the_settings_alone():
    """A change refused partway through mustn't leave its earlier fields behind:
    the daily scan reads memory, and the next unrelated save would store them."""
    practice = {**STORED, "dry_run": True}

    async def body(cog):
        assert await cog.load_data()
        actions = _website(cog)
        refused = []
        for change in ({"practice": False, "inactivityDays": 5},
                       {"enabled": True, "inactivityDays": "soon"},
                       {"practice": False, "warnDaysBefore": 400}):
            try:
                await actions.cleanup_settings(ADMIN, change)
            except Exception as e:
                refused.append(getattr(e, "status", type(e).__name__))
        in_memory = copy.deepcopy(cog.config)
        ok = await actions.cleanup_settings(ADMIN, {"inactivityDays": 200, "warnDaysBefore": 150})
        return refused, in_memory, ok

    (refused, in_memory, ok), stored = _run(body, stored=practice)
    assert refused == [400, 400, 400], "text for a number of days should be the sender's mistake, not a 502"
    assert in_memory == practice, "a refused change still switched settings in memory (live mode, cleanup on)"
    assert ok["ok"] and stored == {**practice, "inactivity_days": 200, "notify_days_before": 150}


def test_a_failed_save_puts_back_what_a_running_scan_reads():
    """A scan already under way reads cog.config as it goes and doesn't load again,
    so a change that failed to save mustn't stay there, least of all live mode."""
    practice = {**STORED, "dry_run": True}

    async def kv_set_fails(namespace, key, value):
        raise RuntimeError("disk full")

    async def body(cog):
        assert await cog.load_data()
        held = cog.config
        with _patched(kv_set=kv_set_fails):
            await _toggle_dry_run(cog, _Interaction())
        return cog.config["dry_run"], held["dry_run"]

    (now, held), stored = _run(body, stored=practice)
    assert now is True and held is True, "a scan in progress would delete for real while the stored mode is practice"
    assert stored == practice


def test_website_keep_forever_is_stored_even_if_a_save_fails_meanwhile():
    """Working out the countdown takes a while. A failed save during it makes the
    next load swap in a new settings dict; the exemption must land in that one."""
    async def kv_set_fails(namespace, key, value):
        raise RuntimeError("disk full")

    async def body(cog):
        assert await cog.load_data()

        async def countdown():
            with _patched(kv_set=kv_set_fails):
                await _toggle_enabled(cog, _Interaction())
            assert await cog.load_data()
            return {"5": {"title": "Old Film", "type": "movie"}}
        return await _website(cog, countdown).exempt(ADMIN, {"ratingKey": "5", "keep": True})

    result, stored = _run(body)
    assert result == {"ok": True, "message": "Kept permanently."}
    assert set(stored["exempt_items"]) == {"123", "5"}, "the website said 'Kept permanently.' but nothing was stored"
    assert {**stored, "exempt_items": STORED["exempt_items"]} == STORED


# --- keeping any title from the website, as /cleanup exempt add does ---

class _Guid:
    def __init__(self, i):
        self.id = i


class _Title:
    def __init__(self, rk, title, kind, year, guids=()):
        self.ratingKey, self.title, self.type, self.year = rk, title, kind, year
        self.guids = [_Guid(g) for g in guids]
        self.addedAt = datetime(2024, 3, 1, tzinfo=timezone.utc)


class _Section:
    def __init__(self, key, title, kind, items):
        self.key, self.title, self.type, self.items = key, title, kind, items
        self.searched = 0

    def search(self, title=None):
        self.searched += 1
        return [i for i in self.items if title.casefold() in i.title.casefold()]


class _Plex:
    """Films and shows in three libraries; STORED skips "Home Videos" and keeps 123."""

    def __init__(self):
        self.shelves = [
            _Section(1, "Movies", "movie", [_Title(201, "Sintel", "movie", 2010, ["tmdb://45745"]),
                                            _Title(123, "Spring", "movie", 2019)]),
            _Section(2, "TV Shows", "show", [_Title(301, "Caminandes", "show", 2013, ["tvdb://276587", "tmdb://46316"])]),
            _Section(3, "Home Videos", "movie", [_Title(401, "Sintel", "movie", 2021)]),
        ]
        self.library = self
        self.fetched = []

    def sections(self):
        return self.shelves

    def fetchItem(self, key):
        from plexapi.exceptions import NotFound

        self.fetched.append(key)
        for section in self.shelves:
            for item in section.items:
                if item.ratingKey == key:
                    return item
        raise NotFound(f"no item {key}")


def _keep_website(cog, countdown=None):
    """_website, with the cleanup settings read the way the portal reads them."""
    from database.kv_store import kv_get

    actions = _website(cog, countdown)

    async def cleanup_config():
        return {**cleanup.DEFAULT_CONFIG, **(await kv_get(NS, "config", {}) or {})}
    actions.data.cleanup_config = cleanup_config
    return actions


def test_website_keep_forever_stores_what_plex_calls_a_title_the_countdown_does_not_list():
    """The website only had the countdown to name a title, so one found by searching
    (or in a list the page cut short) was kept as "Unknown", with no type or year."""
    plex = _Plex()

    async def body(cog):
        cog.services.plex_server = plex
        result = await _keep_website(cog).exempt(ADMIN, {"ratingKey": "301", "keep": True})
        return result, cog.config

    (result, _), stored = _run(body)
    assert result == {"ok": True, "message": "Kept permanently."}
    kept = stored["exempt_items"]["301"]
    assert (kept["title"], kept["type"], kept["year"]) == ("Caminandes", "show", 2013), kept
    assert kept["added_at"] == "2024-03-01T00:00:00+00:00"
    assert plex.fetched == [301]


def test_website_keep_forever_takes_the_year_from_the_countdown():
    async def countdown():
        return {"5": {"ratingKey": "5", "title": "Old Film", "type": "movie", "year": 1999}}

    async def body(cog):
        return await _keep_website(cog, countdown).exempt(ADMIN, {"ratingKey": "5", "keep": True})

    result, stored = _run(body)
    assert result["ok"] is True
    assert {k: stored["exempt_items"]["5"][k] for k in ("title", "type", "year")} == {"title": "Old Film", "type": "movie", "year": 1999}


def test_website_keep_forever_refuses_a_title_plex_does_not_have():
    async def body(cog):
        cog.services.plex_server = _Plex()
        try:
            await _keep_website(cog).exempt(ADMIN, {"ratingKey": "999", "keep": True})
        except Exception as e:
            return getattr(e, "status", type(e).__name__)
        return "kept"

    status, stored = _run(body)
    assert status == 404, status
    assert stored == STORED, "a title Plex doesn't have was kept anyway"


def test_website_keep_forever_says_plex_is_away_rather_than_that_it_lacks_the_title():
    """With cleanup off the countdown is empty, so Plex has to be asked; with Plex
    disconnected that's a 503, not "Plex has no film or show by that key"."""
    async def body(cog):
        cog.services.plex_server = None
        try:
            await _keep_website(cog).exempt(ADMIN, {"ratingKey": "301", "keep": True})
        except Exception as e:
            return getattr(e, "status", type(e).__name__), getattr(e, "text", "")
        return "kept", ""

    (status, text), stored = _run(body)
    assert status == 503 and "isn't connected" in text, (status, text)
    assert stored == STORED, "a title was kept without a name"


def test_website_title_search_skips_the_libraries_cleanup_skips_and_says_what_is_kept():
    plex = _Plex()

    async def body(cog):
        cog.services.plex_server = plex
        actions = _keep_website(cog)
        return (await actions.cleanup_search(ADMIN, "sin"), await actions.cleanup_search(ADMIN, "SPRING"),
                await actions.cleanup_search(ADMIN, " s "))

    (sintel, spring, short), stored = _run(body)
    assert sintel == [{"ratingKey": "201", "title": "Sintel", "type": "movie", "year": 2010, "kept": False}], sintel
    assert [s.searched for s in plex.shelves] == [2, 2, 0], "a library cleanup skips was still searched"
    assert spring == [{"ratingKey": "123", "title": "Spring", "type": "movie", "year": 2019, "kept": True}], spring
    assert short == [], "one letter would list most of the library"
    assert stored == STORED, "searching changed the cleanup settings"


# --- the defaults themselves ---

def test_default_config_is_never_shared_with_a_cog():
    snapshot = copy.deepcopy(cleanup.DEFAULT_CONFIG)

    async def body(cog):
        cog.config["exempt_items"]["9"] = {}           # before any load
        cog.config["exclude_libraries"].append("Kids")
        await cog.load_data()                           # an empty database: the defaults are the settings
        cog.config["exempt_items"]["10"] = {}
        cog.config["exclude_libraries"].append("Music")

    try:
        _run(body, stored=None)
        assert cleanup.DEFAULT_CONFIG["exempt_items"] == {}, "the cog wrote into DEFAULT_CONFIG"
        assert cleanup.DEFAULT_CONFIG["exclude_libraries"] == []
    finally:
        cleanup.DEFAULT_CONFIG.clear()
        cleanup.DEFAULT_CONFIG.update(snapshot)


def test_every_settings_load_and_save_result_is_checked():
    """A bare `await ...load_data()` carries on with the defaults when the read fails."""
    allowed = {"cog_load"}                              # an early read; every use loads again
    offenders = set()
    for rel in ("plugins/media_cleanup/cog.py", "portal/actions.py"):
        tree = ast.parse((ROOT / rel).read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name in allowed:
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Await)
                        and isinstance(node.value.value, ast.Call)
                        and isinstance(node.value.value.func, ast.Attribute)
                        and node.value.value.func.attr in ("load_data", "save_config")):
                    offenders.add(f"{rel}:{node.lineno} in {fn.name}")
    assert not offenders, "these ignore whether the cleanup settings loaded or saved:\n  " + "\n  ".join(sorted(offenders))


# --- what keeps working ---

def test_fresh_install_with_no_saved_row_loads_defaults_and_can_save():
    async def body(cog):
        await _config(cog, _Interaction(), inactivity_days=60)

    _, stored = _run(body, stored=None)
    assert stored == {**DEFAULTS, "inactivity_days": 60}, "no saved row is a fresh install, not a failed read"


def test_toggle_buttons_on_a_fresh_cog_change_only_their_field():
    from database.kv_store import kv_get

    async def body(cog):
        await _toggle_dry_run(cog, _Interaction())
        after_dry_run = await kv_get(NS, "config")
        await _toggle_enabled(cog, _Interaction())
        return after_dry_run

    after_dry_run, stored = _run(body)
    assert after_dry_run == {**STORED, "dry_run": True}
    assert stored == {**STORED, "dry_run": True, "enabled": True}


def test_cog_load_never_raises_and_a_later_command_retries():
    async def body(cog):
        with _patched(kv_get=_unreadable):
            await cog.cog_load()
        unloaded = cog._data_loaded is False
        await _config(cog, _Interaction(), inactivity_days=60)
        return unloaded

    unloaded, stored = _run(body)
    assert unloaded
    assert stored == {**STORED, "inactivity_days": 60}


def test_a_failed_save_forgets_the_unsaved_change():
    async def kv_set_fails(namespace, key, value):
        raise RuntimeError("disk full")

    async def body(cog):
        await cog.load_data()
        assert cog._data_loaded
        toggle = _Interaction()
        with _patched(kv_set=kv_set_fails):
            await _toggle_dry_run(cog, toggle)
        loaded = cog._data_loaded
        status = _Interaction()
        await _status(cog, status)
        return toggle, loaded, status

    (toggle, loaded, status), stored = _run(body)
    embeds = [embed for _, embed, _ in status.sent if embed is not None]
    assert _fields(embeds[0])["Mode"] == "🔴 Live Mode", "the panel showed a change that was never saved"
    assert stored == STORED
    assert [content for content, _, _ in toggle.sent] == [cleanup.SETTINGS_UNSAVED]
    assert loaded is False


# --- scans: only while cleanup is on, and one at a time ---

MONITOR = {"tv_unmonitored": 0, "tv_reenabled": 0, "movie_unmonitored": 0, "movie_reenabled": 0,
           "media_tracking_pruned": 0}


def _stale_library(cog):
    """Plex with one long-unwatched film. Returns what scans remove, in order."""
    removed = []

    async def monitor():
        return dict(MONITOR)

    async def delete_media_items(items):
        removed.extend(item["title"] for item in items)
        return items

    async def quiet(items, kind):
        pass

    async def nothing():
        return 0
    cog.services.plex_server = object()
    cog.enforce_request_monitor_cleanup, cog.delete_media_items = monitor, delete_media_items
    cog.send_cleanup_notification, cog.reconcile_seerr = quiet, nothing
    cog._scan_libraries_for_cleanup = lambda warned=None: ([], [{"title": "Old Film", "type": "movie", "rating_key": "5",
                                                     "days_inactive": 400}])
    return removed


async def _post_scan(actions):
    """POST /api/admin/cleanup/scan as an admin; (status, answer)."""
    import json
    from aiohttp.test_utils import TestClient, TestServer
    from core.config import Config
    from portal.app import build_app

    async def who(request):
        return ADMIN
    app = build_app(FakeServices(Config()), who=who, readonly=False, dist=None,
                    image_cache=tempfile.mkdtemp(), actions=actions)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/api/admin/cleanup/scan", data=json.dumps({}),
                                 headers={"X-Plexbie": "1", "Content-Type": "application/json"})
        return resp.status, await resp.json()
    finally:
        await client.close()


def test_no_scan_runs_while_cleanup_is_off():
    """Only the daily loop and the Discord button checked the off switch. The
    website's Scan (a stale tab, another admin, a direct call) removed titles from
    a library whose cleanup had been switched off, in live mode."""
    async def body(cog):
        removed = _stale_library(cog)
        website = await _website(cog).cleanup_scan(ADMIN)
        status, answer = await _post_scan(_website(cog))
        ix = _Interaction()
        await cog.run_cleanup_scan(ix)
        return removed, website, status, answer, ix

    (removed, website, status, answer, ix), stored = _run(body)      # off, and live
    assert removed == [], "a scan removed titles while cleanup was switched off"
    assert website["ok"] is False and website["message"].startswith("Cleanup is off"), website
    assert (status, answer) == (409, website)
    assert [(content, ephemeral) for content, _, ephemeral in ix.sent] == [(cleanup.SCAN_DISABLED, True)], ix.sent
    assert stored == STORED


async def _within(call, seconds=1):
    """`call`'s result, or None if it is still going after `seconds`."""
    task = asyncio.ensure_future(call)
    done, _ = await asyncio.wait({task}, timeout=seconds)
    return task.result() if done else None


def test_cleanup_scans_never_overlap():
    """Nothing kept the daily check, the Discord button and the website's Scan
    apart: two at once sent Sonarr/Radarr the same removals and posted every
    warning and "Removed" notice twice."""
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()

    async def body(cog):
        from database.kv_store import kv_set
        await kv_set(NS, "last_check", long_ago)
        removed = _stale_library(cog)
        state, entered, release = {"running": 0, "most": 0, "scans": 0}, asyncio.Event(), asyncio.Event()

        async def monitor():
            state["scans"] += 1
            state["running"] += 1
            state["most"] = max(state["most"], state["running"])
            entered.set()
            await release.wait()
            state["running"] -= 1
            return dict(MONITOR)
        cog.enforce_request_monitor_cleanup = monitor

        first = asyncio.create_task(cog.scan_now())
        await entered.wait()
        # A second scan waits on the first one's Plex, so give each a moment and move on.
        website = await _within(_website(cog).cleanup_scan(ADMIN))
        ix = _Interaction()
        await _within(cog.run_cleanup_scan(ix))
        daily = asyncio.create_task(MediaCleanupCog.daily_cleanup_check.coro(cog))
        await asyncio.sleep(0.05)
        during = state["scans"]
        release.set()
        await first
        await daily
        return state, during, removed, website, ix

    (state, during, removed, website, ix), _ = _run(body, stored={**STORED, "enabled": True})
    assert state["most"] == 1, "two cleanup scans ran at the same time"
    assert during == 1, "the daily check started while another scan was still running"
    assert state["scans"] == 2 and removed == ["Old Film", "Old Film"], "the daily check should run once the other is done"
    assert website and website["ok"] is False and website["message"].startswith("A cleanup scan is already running"), website
    assert [(content, ephemeral) for content, _, ephemeral in ix.sent] == [(cleanup.SCAN_BUSY, True)], ix.sent


def test_run_scan_while_another_runs_says_so_without_announcing_a_scan():
    """The button posted "Running Cleanup Scan..." first and only then heard the
    scan had been refused, so the admin was told both."""
    async def body(cog):
        _stale_library(cog)
        ix = _Interaction()
        async with cog._scan_lock:
            await _run_scan(cog, ix)
        return ix

    ix, _ = _run(body, stored={**STORED, "enabled": True})
    assert [(content, embed, ephemeral) for content, embed, ephemeral in ix.sent] == [(cleanup.SCAN_BUSY, None, True)], ix.sent


def test_a_database_error_before_the_daily_check_does_not_stop_it():
    """A tasks.loop ends for good on an exception its body lets out. Reading or
    stamping the last check sat outside the handler, so one locked database
    stopped cleanup until the next restart."""
    long_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()

    async def remembered(namespace, key, default=None):
        return long_ago

    async def never_checked(namespace, key, default=None):
        return None

    async def unwritable(namespace, key, value):
        raise RuntimeError("database is locked")

    async def loaded():
        return True

    for name, kv_get in (("reading the last check", _unreadable), ("stamping a due check", remembered),
                         ("starting the clock", never_checked)):
        cog = object.__new__(MediaCleanupCog)
        cog.load_data, cog.config = loaded, {**DEFAULTS, "enabled": False}
        with _patched(kv_get=kv_get, kv_set=unwritable), _CaptureLog() as log:
            asyncio.run(MediaCleanupCog.daily_cleanup_check.coro(cog))     # raising here ends the loop
        assert any("daily cleanup check" in m and "next hour" in m for m in log.messages), (name, log.messages)
