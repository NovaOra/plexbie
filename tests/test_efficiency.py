# path: tests/test_efficiency.py
"""Wasted work, not wrong answers.

None of the defects covered here produced an incorrect result - which is why they
survived five correctness passes. They each did the right thing far more often, or
far more expensively, than necessary:

  * every stats-message update issued a GET whose body was never read, so the
    bot's own rate-limit monitor showed identical counts on the two routes
    (98 GET / 98 PATCH in a live sample) - half of its Discord traffic;
  * the auto-link loop fetched the whole Tautulli user table every five minutes
    even when every invite it held was already linked;
  * the alias file was opened, read and JSON-parsed once per streaming user on
    every tick of a ten-second loop, synchronously, on the event loop;
  * hint files that expire after fourteen days were swept every ten seconds;
  * the cleanup scan asked Plex for episodes one season at a time.

Each test below pins the cheap shape in place.
"""
import ast
import asyncio
import inspect
import pathlib
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import conftest  # noqa: F401

ROOT = pathlib.Path(conftest.PROJECT_ROOT)


def _source_files():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if any(skip in rel for skip in ("sync-conflict", ".bak", "tests/", "backups/")):
            continue
        yield rel, path.read_text()


# ===================================================================
# 1. A message is edited, never fetched first
# ===================================================================

#: Places a real fetch is justified, because the fetched embed is read. Everything
#: else only ever calls .edit(), for which a partial message is enough - and which
#: still raises NotFound, so the create-a-new-one fallbacks keep working.
FETCH_MESSAGE_ALLOWED = {
    # _close_admin_card adds a "decided on the website" field to the card's
    # existing embed, so it has to read that embed first.
    "plugins/media_requests/cog.py",
    # apply_decision adds the outcome to the Seerr request's announcement embed.
    "webhooks/seerr_handler.py",
}


def _fetch_message_sites():
    for rel, text in _source_files():
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "fetch_message"
            ):
                yield rel, node.lineno


def test_fetch_message_is_only_used_where_the_body_is_read():
    """A fetch whose result is only edited is a wasted round-trip.

    Adding a file here must be a conscious decision: it means the code genuinely
    reads the fetched message, not merely overwrites it.
    """
    offenders = sorted(
        f"{rel}:{line}"
        for rel, line in _fetch_message_sites()
        if rel not in FETCH_MESSAGE_ALLOWED
    )
    assert offenders == [], (
        "these fetch a message only to edit it; use "
        "channel.get_partial_message(id).edit(...) instead:\n  "
        + "\n  ".join(offenders)
    )


def test_the_three_stats_displays_use_partial_messages():
    """The hot path: one 10-second loop and two 5-minute loops."""
    text = (ROOT / "plugins/watch_tracking/cog.py").read_text()
    partials = [
        node
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_partial_message"
    ]
    # One place edits a board (_edit_board), and test_board_clock checks all three use it.
    assert len(partials) == 1, f"expected the boards' one edit path to use a partial message; found {len(partials)}"
    assert "fetch_message" not in text, "no board fetches its message before editing it"


def test_now_watching_tick_issues_no_message_fetch():
    """Behavioural: drive one tick and count the fetches."""
    from plugins.watch_tracking.cog import WatchTrackingCog

    calls = {"fetch": 0, "partial": 0, "edit": 0}

    class FakePartial:
        async def edit(self, **kwargs):
            calls["edit"] += 1

    class FakeChannel:
        def get_partial_message(self, message_id):
            calls["partial"] += 1
            return FakePartial()

        async def fetch_message(self, message_id):
            calls["fetch"] += 1
            return FakePartial()

    class FakeServices:
        plex_server = object()  # only needs to be non-None

        async def plex_sessions(self, max_age=None, force=False):
            return []

    cog = object.__new__(WatchTrackingCog)
    cog.services = FakeServices()
    cog.stats_channel = FakeChannel()
    cog.now_watching_message_id = 123
    cog._username_cache = {}
    cog._aliases_cache = {}
    cog._aliases_stamp = None
    from utils.board_clock import BoardClock
    cog.clock, cog.live = BoardClock(), False

    async def _true():
        return True

    async def _noop():
        return None

    cog._ensure_channel = _true
    cog._refresh_username_cache = _noop

    asyncio.run(WatchTrackingCog.update_now_watching.coro(cog))

    assert calls["edit"] == 1, "the tick did not complete - the display would go stale"
    assert calls["partial"] == 1
    assert calls["fetch"] == 0, (
        "the loop still fetches the message it is about to overwrite; at one tick "
        "every 10 seconds that is ~8,600 pointless REST calls a day"
    )


# ===================================================================
# 2. No Tautulli round-trip when nothing is waiting to link
# ===================================================================

def _user_mgmt_cog(invites):
    from plugins.user_mgmt import cog as module

    calls = {"tautulli": 0}

    from core.clients import ServiceError

    class FakeTautulli:
        configured = True

        async def users(self):
            calls["tautulli"] += 1
            raise ServiceError("Tautulli answered HTTP 500", 500)

    class FakeConfig:
        tautulli_url = "http://tautulli.local"
        tautulli_token = "token"

    class FakeServices:
        config = FakeConfig()
        tautulli = FakeTautulli()

    cog = object.__new__(module.UserMgmtCog)
    cog.services = FakeServices()
    cog.bot = None

    async def _kv_get_all(namespace):
        return invites

    original = module.kv_get_all
    module.kv_get_all = _kv_get_all
    return module, cog, calls, original


def test_fully_linked_invites_do_not_touch_tautulli():
    """The steady state: invites are never removed once they link."""
    invites = {
        "1": {"status": "linked", "email": "a@example.com"},
        "2": {"status": "linked", "email": "b@example.com"},
    }
    module, cog, calls, original = _user_mgmt_cog(invites)
    try:
        asyncio.run(module.UserMgmtCog.auto_link_users.coro(cog))
    finally:
        module.kv_get_all = original

    assert calls["tautulli"] == 0, (
        "the whole Tautulli user table was fetched to skip every row; at one pass "
        "every 5 minutes that is 288 no-op round-trips a day"
    )


def test_an_approved_invite_still_queries_tautulli():
    """The guard must not turn the feature off."""
    invites = {
        "1": {"status": "linked", "email": "a@example.com"},
        "2": {"status": "approved", "email": "b@example.com"},
    }
    module, cog, calls, original = _user_mgmt_cog(invites)
    try:
        asyncio.run(module.UserMgmtCog.auto_link_users.coro(cog))
    finally:
        module.kv_get_all = original

    assert calls["tautulli"] == 1, "an approved invite must still be looked up"


def test_a_request_waiting_for_approval_is_never_linked():
    """Someone removed from Plex asked to join again; Tautulli still knew their
    email, so the new, unapproved request was marked "linked" at once."""
    invites = {"1": {"status": "pending", "email": "back@example.com"}}
    module, cog, calls, original = _user_mgmt_cog(invites)
    try:
        asyncio.run(module.UserMgmtCog.auto_link_users.coro(cog))
    finally:
        module.kv_get_all = original
    assert calls["tautulli"] == 0, "nothing to link until an admin approves"


def test_invites_with_no_email_are_not_worth_a_request():
    """An invite with no email can never match, pending or not."""
    invites = {"1": {"status": "pending"}, "2": {"status": "pending", "email": ""}}
    module, cog, calls, original = _user_mgmt_cog(invites)
    try:
        asyncio.run(module.UserMgmtCog.auto_link_users.coro(cog))
    finally:
        module.kv_get_all = original

    assert calls["tautulli"] == 0


# ===================================================================
# 3. The alias file is read only when it changes
# ===================================================================

def _watch_tracking_cog():
    from plugins.watch_tracking.cog import WatchTrackingCog

    cog = object.__new__(WatchTrackingCog)
    cog._aliases_cache = {}
    cog._aliases_stamp = None
    return cog


def test_alias_lookups_reuse_one_parse():
    """_get_display_name runs once per streaming user on a 10-second loop."""
    from plugins.watch_tracking import cog as module

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "user_aliases.json"
        path.write_text('{"aliases": {"alt": "primary"}}')

        original = module.USER_ALIASES_FILE
        module.USER_ALIASES_FILE = path
        try:
            cog = _watch_tracking_cog()
            first = cog._load_aliases()
            second = cog._load_aliases()
            assert first == {"alt": "primary"}
            assert second is first, (
                "the file was re-read and re-parsed; this is a synchronous read "
                "on the event loop, once per user, every 10 seconds"
            )
        finally:
            module.USER_ALIASES_FILE = original


def test_editing_the_alias_file_still_takes_effect():
    """Caching must not require a restart to pick up a change."""
    from plugins.watch_tracking import cog as module

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "user_aliases.json"
        path.write_text('{"aliases": {"alt": "primary"}}')

        original = module.USER_ALIASES_FILE
        module.USER_ALIASES_FILE = path
        try:
            cog = _watch_tracking_cog()
            assert cog._load_aliases() == {"alt": "primary"}

            # A different size guarantees a new stamp regardless of clock
            # granularity, which is the point of keying on size as well as mtime.
            path.write_text('{"aliases": {"alt": "primary", "other": "second"}}')
            assert cog._load_aliases() == {"alt": "primary", "other": "second"}
        finally:
            module.USER_ALIASES_FILE = original


def test_a_missing_alias_file_is_not_an_error():
    from plugins.watch_tracking import cog as module

    with tempfile.TemporaryDirectory() as tmp:
        original = module.USER_ALIASES_FILE
        module.USER_ALIASES_FILE = Path(tmp) / "absent.json"
        try:
            cog = _watch_tracking_cog()
            assert cog._load_aliases() == {}
            assert cog._resolve_alias("someone") == "someone"
        finally:
            module.USER_ALIASES_FILE = original


def test_a_corrupt_alias_file_is_retried_not_cached():
    """Caching a parse failure would hide a later repair until restart."""
    from plugins.watch_tracking import cog as module

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "user_aliases.json"
        path.write_text("{ not json")

        original = module.USER_ALIASES_FILE
        module.USER_ALIASES_FILE = path
        try:
            cog = _watch_tracking_cog()
            assert cog._load_aliases() == {}
            assert cog._aliases_stamp is None, "a bad parse must not be stamped"

            path.write_text('{"aliases": {"alt": "primary"}}')
            assert cog._load_aliases() == {"alt": "primary"}
        finally:
            module.USER_ALIASES_FILE = original


# ===================================================================
# 5. Hint expiry is swept on the hint's timescale, not the loop's
# ===================================================================

def test_sweep_interval_is_far_longer_than_the_scan_interval():
    from plugins.bookshelf_processor.cog import (
        HINT_MAX_AGE_DAYS,
        HINT_SWEEP_INTERVAL_SECONDS,
    )

    assert HINT_SWEEP_INTERVAL_SECONDS >= 3600
    assert HINT_SWEEP_INTERVAL_SECONDS < HINT_MAX_AGE_DAYS * 86400, (
        "sweeping less often than the expiry would leave hints past their deadline"
    )


def _bookshelf_cog(tmp):
    from plugins.bookshelf_processor.cog import BookshelfProcessorCog

    audiobooks = Path(tmp) / "audiobooks"
    ebooks = Path(tmp) / "ebooks"
    audiobooks.mkdir()
    ebooks.mkdir()

    cog = object.__new__(BookshelfProcessorCog)
    cog.bot = None
    cog.services = None
    cog.settle_seconds = 120
    cog.pending = {}
    cog.failed = {}
    cog.audiobook_watch = audiobooks
    cog.ebook_watch = ebooks
    cog.audiobook_lib = Path(tmp) / "lib_a"
    cog.ebook_lib = Path(tmp) / "lib_e"
    cog.cache_dir = Path(tmp) / "cache"
    cog._last_hint_sweep = None
    return cog


def test_hints_are_not_swept_on_every_tick():
    from plugins.bookshelf_processor import cog as module
    from plugins.bookshelf_processor.cog import BookshelfProcessorCog

    swept = []

    def fake_expire(watch_dir):
        swept.append(watch_dir)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        cog = _bookshelf_cog(tmp)
        original = module._expire_stale_hints
        module._expire_stale_hints = fake_expire
        try:
            asyncio.run(BookshelfProcessorCog.scan_loop.coro(cog))
            assert len(swept) == 2, "the first tick should sweep both watch dirs"

            for _ in range(5):
                asyncio.run(BookshelfProcessorCog.scan_loop.coro(cog))
            assert len(swept) == 2, (
                f"swept {len(swept)} times across 6 ticks; at one tick every 10 "
                f"seconds that is ~17,000 directory scans a day to enforce a "
                f"14-day expiry"
            )
        finally:
            module._expire_stale_hints = original


def test_the_sweep_happens_again_once_the_interval_has_passed():
    """Throttling must not become never."""
    from plugins.bookshelf_processor import cog as module
    from plugins.bookshelf_processor.cog import (
        BookshelfProcessorCog,
        HINT_SWEEP_INTERVAL_SECONDS,
    )

    swept = []

    def fake_expire(watch_dir):
        swept.append(watch_dir)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        cog = _bookshelf_cog(tmp)
        original = module._expire_stale_hints
        module._expire_stale_hints = fake_expire
        try:
            asyncio.run(BookshelfProcessorCog.scan_loop.coro(cog))
            assert len(swept) == 2

            cog._last_hint_sweep = datetime.now() - timedelta(
                seconds=HINT_SWEEP_INTERVAL_SECONDS + 1
            )
            asyncio.run(BookshelfProcessorCog.scan_loop.coro(cog))
            assert len(swept) == 4, "an expired interval must sweep again"
        finally:
            module._expire_stale_hints = original


# ===================================================================
# 7. One request per show, not one per season
# ===================================================================

def test_show_episodes_are_fetched_in_one_request():
    from plugins.media_cleanup.cog import MediaCleanupCog

    calls = {"episodes": 0, "seasons": 0}

    class FakeEpisode:
        lastViewedAt = datetime.now() - timedelta(days=400)

    class FakeShow:
        type = "show"
        title = "Some Show"
        ratingKey = 42
        addedAt = datetime.now() - timedelta(days=500)

        def episodes(self):
            calls["episodes"] += 1
            return [FakeEpisode(), FakeEpisode()]

        def seasons(self):
            calls["seasons"] += 1
            raise AssertionError("seasons() costs one extra request per season")

    cog = object.__new__(MediaCleanupCog)
    cog.config = {
        "inactivity_days": 90,
        "notify_days_before": 7,
        "exempt_items": {},
        "dry_run": True,
    }
    cog._get_recent_request_timestamp = lambda item: None

    result = cog.check_item_for_cleanup(FakeShow())

    # check_item_for_cleanup swallows exceptions and returns None, so assert on
    # the result too - otherwise a raising fake would look like a pass.
    assert result is not None, "the show was not classified; an exception was swallowed"
    assert result["action"] == "delete"
    assert calls["seasons"] == 0, "seasons() costs 1 + N requests per show"
    assert calls["episodes"] == 1, "expected exactly one /allLeaves request"



def test_media_cleanup_runs_once_a_day_not_on_every_restart():
    """Each restart ran the cleanup check and sent its warnings again."""
    import asyncio
    from plugins.media_cleanup import cog as module
    store, ran = {}, []

    async def kv_get(ns, key, default=None):
        return store.get(key, default)

    async def kv_set(ns, key, value):
        store[key] = value

    async def load_data():
        ran.append(True)
    cog = object.__new__(module.MediaCleanupCog)
    cog.load_data = load_data
    cog.config = {"enabled": False}
    saved = module.kv_get, module.kv_set
    module.kv_get, module.kv_set = kv_get, kv_set
    try:
        check = lambda: asyncio.run(module.MediaCleanupCog.daily_cleanup_check.coro(cog))
        check()                                       # a new install: just starts the clock
        check(); check()                              # restarts within the day
        assert ran == [], "no check until a day has passed"
        store["last_check"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        check(); check()
    finally:
        module.kv_get, module.kv_set = saved
    assert ran == [True], "one check a day"


def test_media_cleanup_does_not_walk_seasons():
    """Pattern: the 1 + N shape must not come back."""
    text = (ROOT / "plugins/media_cleanup/cog.py").read_text()
    offenders = [
        node.lineno
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "seasons"
    ]
    assert offenders == [], (
        f"seasons() at line(s) {offenders}: use show.episodes(), which fetches "
        f"/allLeaves in a single request"
    )


# ===================================================================
# 6. One sessions() snapshot, shared between the plugins that poll it
# ===================================================================

def _services_with_counting_plex():
    """A BotServices whose Plex counts how often sessions() is actually called."""
    from core.config import Config
    from core.services import BotServices

    calls = {"sessions": 0}

    class CountingPlex:
        def sessions(self):
            calls["sessions"] += 1
            return [f"session-{calls['sessions']}"]

    services = BotServices(Config())
    services.plex_server = CountingPlex()
    return services, calls


def test_a_second_caller_in_the_window_reuses_the_snapshot():
    """watch_tracking and watch_party poll the same fact on the same cadence."""
    services, calls = _services_with_counting_plex()

    async def scenario():
        first = await services.plex_sessions()
        second = await services.plex_sessions()
        return first, second

    first, second = asyncio.run(scenario())
    assert calls["sessions"] == 1, (
        "each consumer issued its own request for one snapshot of who is streaming"
    )
    assert second == first


def test_concurrent_callers_collapse_to_one_request():
    """Two loops firing in the same moment is the case this exists to collapse."""
    services, calls = _services_with_counting_plex()

    async def scenario():
        return await asyncio.gather(*(services.plex_sessions() for _ in range(4)))

    results = asyncio.run(scenario())
    assert calls["sessions"] == 1, (
        f"{calls['sessions']} requests for 4 concurrent callers; the re-check "
        f"under the lock is missing or ineffective"
    )
    assert all(r == results[0] for r in results)


def test_a_stale_snapshot_is_refetched():
    """Sharing must not mean serving arbitrarily old data."""
    from core.services import PLEX_SESSIONS_MAX_AGE

    services, calls = _services_with_counting_plex()

    async def scenario():
        await services.plex_sessions()
        # Backdate the entry rather than sleeping.
        server, fetched_at, sessions = services._sessions_cache
        services._sessions_cache = (
            server,
            fetched_at - (PLEX_SESSIONS_MAX_AGE + 1),
            sessions,
        )
        return await services.plex_sessions()

    asyncio.run(scenario())
    assert calls["sessions"] == 2, "a snapshot past its age must be refetched"


def test_max_age_stays_under_the_ten_second_poll_interval():
    """Otherwise a 10-second display could serve data older than its own period."""
    from core.services import PLEX_SESSIONS_MAX_AGE

    assert 0 < PLEX_SESSIONS_MAX_AGE < 10


def test_a_reconnect_invalidates_the_snapshot():
    """A new PlexServer says nothing about what the old one reported."""
    services, calls = _services_with_counting_plex()

    class OtherPlex:
        def sessions(self):
            return ["from-the-new-server"]

    async def scenario():
        await services.plex_sessions()
        services.plex_server = OtherPlex()      # as service_health does on reconnect
        return await services.plex_sessions()

    result = asyncio.run(scenario())
    assert result == ["from-the-new-server"], (
        "the snapshot cached against the previous connection was served after a "
        "reconnect"
    )


def test_force_bypasses_the_cache_but_still_fills_it():
    """A liveness probe answered from cache is not a probe - but its result is
    perfectly good for everyone else.
    """
    services, calls = _services_with_counting_plex()

    async def scenario():
        await services.plex_sessions()            # 1 - a polling loop
        await services.plex_sessions(force=True)  # 2 - the health probe
        await services.plex_sessions()            # reuses the probe's result
        return None

    asyncio.run(scenario())
    assert calls["sessions"] == 2, (
        f"expected the forced probe to fetch and then be reused; got "
        f"{calls['sessions']} requests"
    )


def test_no_plex_server_raises_rather_than_reporting_an_empty_list():
    """An empty list reads as "nobody is watching", which is a wrong answer."""
    from core.config import Config
    from core.services import BotServices

    services = BotServices(Config())
    services.plex_server = None

    raised = False
    try:
        asyncio.run(services.plex_sessions())
    except RuntimeError:
        raised = True
    assert raised, "a missing Plex connection must not look like an idle server"


def test_service_health_probe_is_forced():
    """If the probe could be answered from cache it would stop being a probe."""
    from plugins.service_health.cog import ServiceHealthCog

    source = inspect.getsource(ServiceHealthCog._check_plex)
    assert "plex_sessions(force=True)" in source, (
        "the health probe must not be satisfied by another plugin's snapshot"
    )


def test_the_polling_consumers_share_the_snapshot():
    """Pattern: nobody should reach past the accessor to sessions() directly."""
    offenders = []
    for rel, text in _source_files():
        if rel == "core/services.py":
            continue  # the accessor itself
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Attribute) or node.attr != "sessions":
                continue
            # service_health verifies a brand-new connection before publishing it,
            # which is deliberately not the shared one.
            if rel == "plugins/service_health/cog.py":
                continue
            offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        "call services.plex_sessions() instead of fetching sessions directly:\n  "
        + "\n  ".join(offenders)
    )


# ===================================================================
# 9. No database transaction is held open across network I/O
# ===================================================================

#: Awaited calls that leave the process. A database transaction must not be open
#: across any of them: this SQLite database runs in `delete` journal mode, not
#: WAL, so a read transaction holds a SHARED lock (blocking writers) and a
#: flushed write holds RESERVED/EXCLUSIVE (blocking readers too) until commit.
#: busy_timeout is 5s, after which the blocked caller gets "database is locked".
NETWORK_AWAITS = (
    "send", "fetch_user", "fetch_channel", "fetch_message", "run_blocking",
    "removeFriend", "add_roles", "remove_roles", "_get_user_stats",
    "_remove_plex_role", "_dm",
)


def _opens_a_session(node):
    for item in node.items:
        expr = item.context_expr
        if isinstance(expr, ast.Call):
            func = expr.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "get_session":
                return True
    return False


def _sessions_held_across_network_io():
    for rel, text in _source_files():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            if not _opens_a_session(node):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Await) or not isinstance(inner.value, ast.Call):
                    continue
                func = inner.value.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                # session.execute / session.commit / session.delete are the point.
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "session"
                ):
                    continue
                if any(key in name for key in NETWORK_AWAITS):
                    yield f"{rel}:{inner.lineno} ({name}() inside session opened at line {node.lineno})"


def test_no_session_is_held_open_across_network_io():
    """Read, close, do the slow work, then write.

    check_inactive_users wrapped one session around its whole loop - including a
    plex.tv login, removeFriend, a Tautulli stats fetch, a DM and a Discord role
    edit, per user. The manual /remove-user command did the same, and
    invite_tracker flushed its row (taking the write lock) before awaiting
    add_roles. Every other plugin's kv_set queues behind those.
    """
    offenders = sorted(set(_sessions_held_across_network_io()))
    assert offenders == [], (
        "these hold a database transaction open across a network call:\n  "
        + "\n  ".join(offenders)
    )


def test_remove_inactive_user_reports_whether_the_row_should_go():
    """It no longer takes a session, so the caller needs a truthful answer."""
    from plugins.user_mgmt.cog import UserMgmtCog

    signature = inspect.signature(UserMgmtCog._remove_inactive_user)
    assert "session" not in signature.parameters, (
        "passing a session in is what forced the transaction to stay open across "
        "plex.tv and Discord calls"
    )

    source = inspect.getsource(UserMgmtCog._remove_inactive_user)
    tree = ast.parse(inspect.cleandoc("\n".join(source.splitlines()[1:])))
    returns = [
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant)
    ]
    assert True in returns, "no path reports success, so no row would ever be deleted"
    assert False in returns, (
        "a failed Plex removal must return False so the row is kept and retried"
    )


def test_a_failed_plex_removal_keeps_the_tracking_row():
    """Behavioural: the row is the only record that a retry is owed."""
    from plugins.user_mgmt.cog import UserMgmtCog
    from plugins.user_mgmt.models import PlexUser

    class FakeConfig:
        plex_username = None      # not configured -> removal cannot proceed
        plex_password = None

    class FakeServices:
        config = FakeConfig()

    cog = object.__new__(UserMgmtCog)
    cog.services = FakeServices()

    async def _stats(username):
        return {}

    cog._get_user_stats = _stats

    user = PlexUser(plex_username="someone", plex_email="a@example.com")
    removed = asyncio.run(cog._remove_inactive_user(user, 1))
    assert removed is False, (
        "returning anything truthy here would delete the row while the user still "
        "has Plex access, with no record left to retry against"
    )


# ===================================================================
# 8. Bulk key-value writes go in one transaction
# ===================================================================

def _kv_set_in_loops():
    """Awaited kv_set / kv_delete calls that sit inside a loop."""
    for rel, text in _source_files():
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Await) or not isinstance(inner.value, ast.Call):
                    continue
                func = inner.value.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in ("kv_set", "kv_delete"):
                    yield f"{rel}:{inner.lineno} ({name}() in loop at line {node.lineno})"


def test_no_per_key_kv_writes_in_a_loop():
    """A commit is an fsync, so N keys must not mean N transactions.

    new_media_added saved every tracked batch one key at a time whenever any
    single batch changed, and auto_link_users rewrote the entire invite namespace
    when one invite linked - most of those writes byte-identical to what was
    already stored. Use kv_set_many.
    """
    offenders = sorted(set(_kv_set_in_loops()))
    assert offenders == [], (
        "batch these with kv_set_many instead of one transaction per key:\n  "
        + "\n  ".join(offenders)
    )


def test_save_tracking_data_writes_every_batch_in_one_call():
    from plugins.new_media_added import cog as module

    calls = []

    async def fake_set_many(namespace, items):
        calls.append((namespace, dict(items)))

    class FakeBatch:
        def __init__(self, name):
            self.name = name

        def to_dict(self):
            return {"show": self.name}

    cog = object.__new__(module.NewMediaAddedCog)
    cog.active_batches = {"a:s1": FakeBatch("A"), "b:s2": FakeBatch("B")}

    original = module.kv_set_many
    module.kv_set_many = fake_set_many
    try:
        asyncio.run(module.NewMediaAddedCog.save_tracking_data(cog))
    finally:
        module.kv_set_many = original

    assert len(calls) == 1, f"expected one batched write, got {len(calls)}"
    namespace, items = calls[0]
    assert namespace == module.NEW_MEDIA_NAMESPACE
    assert items == {"a:s1": {"show": "A"}, "b:s2": {"show": "B"}}


def test_auto_link_writes_only_the_invites_that_changed():
    """One invite linking must not rewrite every other invite in the namespace.

    Uses a real temporary database: the link itself opens a session, and an
    earlier version of this test let that fail into auto_link_users' except
    clause - which left `written` empty and made every assertion below vacuous.
    """
    import tempfile as _tempfile

    from plugins.user_mgmt import cog as module
    import database.session as session_module

    invites = {
        "1": {"status": "linked", "email": "already@example.com"},
        "2": {"status": "approved", "email": "new@example.com"},
        "3": {"status": "approved", "email": "absent@example.com"},
        "4": {"status": "approved", "email": "removed@example.com"},
    }

    written = []

    async def fake_set_many(namespace, items):
        written.append((namespace, dict(items)))

    class FakeTautulli:
        configured = True

        async def users(self):
            # Only invite "2" has a matching Tautulli account that still has access;
            # "removed" was taken off Plex (Tautulli keeps them, inactive).
            return [{"email": "new@example.com", "friendly_name": "newuser", "user_id": 7, "is_active": 1},
                    {"email": "removed@example.com", "friendly_name": "gone", "user_id": 8, "is_active": 0}]

    class FakeConfig:
        tautulli_url = "http://tautulli.local"
        tautulli_token = "token"
        guild_id = 1

    class FakeServices:
        config = FakeConfig()
        tautulli = FakeTautulli()

    cog = object.__new__(module.UserMgmtCog)
    cog.services = FakeServices()
    cog.bot = None  # get_guild raises AttributeError, which the cog already handles

    async def fake_kv_get_all(namespace):
        return invites

    db_path = pathlib.Path(_tempfile.mkdtemp()) / "autolink.db"

    async def scenario():
        if session_module.engine is not None:
            await session_module.engine.dispose()
        await session_module.init_database(f"sqlite:///{db_path}")
        try:
            await module.UserMgmtCog.auto_link_users.coro(cog)
        finally:
            await session_module.engine.dispose()

    original_get_all = module.kv_get_all
    original_set_many = module.kv_set_many
    module.kv_get_all = fake_kv_get_all
    module.kv_set_many = fake_set_many
    try:
        asyncio.run(scenario())
    finally:
        module.kv_get_all = original_get_all
        module.kv_set_many = original_set_many

    assert len(written) == 1, (
        f"expected exactly one batched write, got {len(written)} - if zero, the "
        f"link failed and auto_link_users swallowed it, making this test vacuous"
    )
    _, items = written[0]
    assert set(items) == {"2"}, (
        f"only the invite that linked should be written back; got {sorted(items)}"
    )
    assert items["2"]["status"] == "linked"
    assert items["2"]["plex_username"] == "newuser"
