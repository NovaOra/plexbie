# path: tests/test_request_store.py
"""Media requests moved from one big JSON file to one row per request.

The records are NOT disposable, which is why this is a migration and not a prune.
media_cleanup reads them daily and, for each medium's newest request, either
ensures Sonarr/Radarr is monitoring it (within REQUEST_EXPIRY_DAYS) or un-monitors
it beyond that. A medium with no record is skipped entirely - so losing a record
would leave an approved title un-monitored with nothing left to correct it.

The load-bearing test here is therefore not "the store works" but "media_cleanup
reaches exactly the same conclusions it did from the file".
"""
import asyncio
import json
import pathlib
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401

from database.request_store import (
    REQUESTS_NAMESPACE,
    STATUS_APPROVED,
    STATUS_DECLINED,
    STATUS_PENDING,
    all_requests,
    get_request,
    mark_resolved,
    pending_requests,
    save_request,
)


def _tmp_db(name="requests.db"):
    return str(pathlib.Path(tempfile.mkdtemp()) / name)


def _no_legacy():
    """Point the migration at nothing.

    _LEGACY_REQUESTS_FILE is module-level state, so a test that sets it leaks into
    every later test in the file - which is exactly how this helper came to exist:
    the migration quietly injected one test's fixture into another's database.
    """
    import database.session as session_module

    session_module._LEGACY_REQUESTS_FILE = (
        pathlib.Path(tempfile.mkdtemp()) / "no-legacy-file.json"
    )


async def _init(path):
    import database.session as session_module

    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


async def _dispose():
    import database.session as session_module

    if session_module.engine is not None:
        await session_module.engine.dispose()


#: Mirrors the real file's awkward shapes: naive and aware timestamps in the same
#: field, two requests for one medium, and a book record carrying no tmdb id.
FIXTURE = {
    "1001": {
        "user_id": 11, "status": "pending",
        "media": {"id": 500, "media_type": "tv", "name": "Show A"},
        "seasons": [1, 2], "monitor": True,
        "timestamp": "2025-11-09T23:49:35.367435",              # naive
    },
    "1002": {
        "user_id": 12, "status": "pending",
        "media": {"id": 500, "media_type": "tv", "name": "Show A"},
        "seasons": "all", "monitor": False,
        "timestamp": "2026-09-01T10:00:00+00:00",               # aware, newer
    },
    "1003": {
        "user_id": 13, "status": "pending",
        "media": {"id": 900, "media_type": "movie", "title": "Film B"},
        "timestamp": "2026-08-01T10:00:00+00:00",
    },
    "1004": {
        "user_id": 14, "status": "pending",
        "media": {"title": "Some Audiobook", "author": "Someone"},   # no tmdb id
        "media_type": "audiobook",
        "timestamp": "2026-07-01T10:00:00+00:00",
    },
}


def _write_legacy(path_dir, data=None):
    """Put a legacy media_requests.json where the migration will find it."""
    import database.session as session_module

    legacy = pathlib.Path(path_dir) / "media_requests.json"
    legacy.write_text(json.dumps(data if data is not None else FIXTURE, indent=2))
    session_module._LEGACY_REQUESTS_FILE = legacy
    return legacy


# ===================================================================
# the store
# ===================================================================

def test_save_and_get_roundtrip():
    path = _tmp_db()
    _no_legacy()

    async def scenario():
        await _init(path)
        await save_request(4242, user_id=7, media={"id": 1, "name": "X"},
                           seasons=[3], monitor=True)
        got = await get_request(4242)
        await _dispose()
        return got

    record = asyncio.run(scenario())
    assert record["user_id"] == 7
    assert record["media"] == {"id": 1, "name": "X"}
    assert record["seasons"] == [3]
    assert record["monitor"] is True
    assert record["status"] == STATUS_PENDING


def test_a_missing_request_is_none_not_an_error():
    path = _tmp_db()
    _no_legacy()

    async def scenario():
        await _init(path)
        got = await get_request(999999)
        await _dispose()
        return got

    assert asyncio.run(scenario()) is None


def test_timestamps_are_written_timezone_aware():
    """The file held 154 naive and 61 aware values in one field; a consumer that
    forgets to coerce raises TypeError on the naive ones.
    """
    path = _tmp_db()
    _no_legacy()

    async def scenario():
        await _init(path)
        await save_request(1, user_id=1, media={"id": 1})
        got = await get_request(1)
        await _dispose()
        return got

    stamp = datetime.fromisoformat(asyncio.run(scenario())["timestamp"])
    assert stamp.tzinfo is not None, "new records must be timezone-aware"


def test_appending_does_not_rewrite_other_records():
    """The point of the change: one keyed write, not a whole-store rewrite."""
    path = _tmp_db()
    _no_legacy()

    async def scenario():
        await _init(path)
        await save_request(1, user_id=1, media={"id": 1})
        first = await get_request(1)
        await save_request(2, user_id=2, media={"id": 2})
        again = await get_request(1)
        await _dispose()
        return first, again

    first, again = asyncio.run(scenario())
    assert first == again, "writing one request must leave the others untouched"


# ===================================================================
# resolution
# ===================================================================

def test_mark_resolved_records_the_outcome_and_keeps_the_record():
    path = _tmp_db()
    _no_legacy()

    async def scenario():
        await _init(path)
        await save_request(7, user_id=1, media={"id": 5, "media_type": "tv"})
        ok = await mark_resolved(7, STATUS_APPROVED, "admin#1")
        got = await get_request(7)
        await _dispose()
        return ok, got

    ok, record = asyncio.run(scenario())
    assert ok is True
    assert record["status"] == STATUS_APPROVED
    assert record["resolved_by"] == "admin#1"
    assert record["resolved_at"]
    # the monitoring reconciliation still needs everything else
    assert record["media"] == {"id": 5, "media_type": "tv"}
    assert record["timestamp"], "the original request time must survive"


def test_mark_resolved_on_an_unknown_request_is_false_not_a_crash():
    path = _tmp_db()
    _no_legacy()

    async def scenario():
        await _init(path)
        result = await mark_resolved(12345, STATUS_DECLINED, "admin")
        await _dispose()
        return result

    assert asyncio.run(scenario()) is False


def test_pending_requests_excludes_resolved_ones():
    """Before this, every record claimed to be pending - including ones actioned
    a year earlier - so 'what is still waiting on me' had no answer.
    """
    path = _tmp_db()
    _no_legacy()

    async def scenario():
        await _init(path)
        await save_request(1, user_id=1, media={"id": 1})
        await save_request(2, user_id=2, media={"id": 2})
        await save_request(3, user_id=3, media={"id": 3})
        await mark_resolved(2, STATUS_APPROVED, "a")
        await mark_resolved(3, STATUS_DECLINED, "a")
        pend = await pending_requests()
        every = await all_requests()
        await _dispose()
        return pend, every

    pending, every = asyncio.run(scenario())
    assert set(pending) == {"1"}
    assert set(every) == {"1", "2", "3"}, "resolving must not delete anything"


# ===================================================================
# the migration
# ===================================================================

def test_migration_copies_every_record_losslessly():
    path = _tmp_db()
    _no_legacy()
    tmpdir = tempfile.mkdtemp()
    _write_legacy(tmpdir)

    async def scenario():
        await _init(path)
        got = await all_requests()
        await _dispose()
        return got

    migrated = asyncio.run(scenario())
    assert set(migrated) == set(FIXTURE), (
        f"expected {sorted(FIXTURE)}, got {sorted(migrated)}"
    )
    for key, original in FIXTURE.items():
        assert migrated[key] == original, f"record {key} changed during migration"


def test_migration_leaves_the_file_on_disk():
    """It is the backup. Losing both copies at once is the failure to avoid."""
    path = _tmp_db()
    _no_legacy()
    tmpdir = tempfile.mkdtemp()
    legacy = _write_legacy(tmpdir)

    async def scenario():
        await _init(path)
        await _dispose()

    asyncio.run(scenario())
    assert legacy.exists()
    assert json.loads(legacy.read_text()) == FIXTURE


def test_migration_never_clobbers_a_status_the_bot_recorded():
    """Re-running startup must not resurrect 'pending' over a real outcome.

    This is why the insert is ON CONFLICT DO NOTHING rather than DO UPDATE.
    """
    path = _tmp_db()
    _no_legacy()
    tmpdir = tempfile.mkdtemp()
    _write_legacy(tmpdir)

    async def scenario():
        await _init(path)
        await mark_resolved(1001, STATUS_APPROVED, "admin#1")
        await _dispose()
        # a second startup, with the same stale file still on disk
        await _init(path)
        got = await get_request(1001)
        await _dispose()
        return got

    record = asyncio.run(scenario())
    assert record["status"] == STATUS_APPROVED, (
        "the file overwrote a status the bot had already recorded"
    )
    assert record["resolved_by"] == "admin#1"


def test_migration_is_idempotent():
    path = _tmp_db()
    _no_legacy()
    tmpdir = tempfile.mkdtemp()
    _write_legacy(tmpdir)

    async def scenario():
        for _ in range(3):
            await _init(path)
            await _dispose()

    asyncio.run(scenario())
    with sqlite3.connect(path) as con:
        n = con.execute(
            "SELECT COUNT(*) FROM key_value_store WHERE namespace=?",
            (REQUESTS_NAMESPACE,),
        ).fetchone()[0]
    assert n == len(FIXTURE), f"expected {len(FIXTURE)} rows, found {n}"


def test_a_missing_or_empty_legacy_file_is_not_an_error():
    path = _tmp_db()
    _no_legacy()
    tmpdir = tempfile.mkdtemp()

    async def scenario():
        import database.session as session_module
        session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tmpdir) / "absent.json"
        await _init(path)
        a = await all_requests()
        await _dispose()

        empty = _write_legacy(tmpdir, {})
        await _init(path)
        b = await all_requests()
        await _dispose()
        return a, b

    a, b = asyncio.run(scenario())
    assert a == {} and b == {}


# ===================================================================
# the consumer whose behaviour must not change
# ===================================================================

def test_media_cleanup_reaches_identical_conclusions():
    """The load-bearing test.

    _latest_requests_by_media drives whether Sonarr/Radarr monitoring is enabled
    or disabled for a title. Run it over the legacy file and over the migrated
    store and require the same answer - same media, same chosen timestamps, same
    requested seasons.
    """
    from plugins.media_cleanup.cog import MediaCleanupCog

    path = _tmp_db()
    _no_legacy()
    tmpdir = tempfile.mkdtemp()
    _write_legacy(tmpdir)

    cog = object.__new__(MediaCleanupCog)

    async def scenario():
        await _init(path)
        got = await all_requests()
        await _dispose()
        return got

    from_store = asyncio.run(scenario())

    from_file = cog._latest_requests_by_media(FIXTURE)
    from_db = cog._latest_requests_by_media(from_store)

    assert set(from_db) == set(from_file), (
        f"different media selected: file={sorted(from_file)} db={sorted(from_db)}"
    )
    for key in from_file:
        assert from_db[key]["timestamp"] == from_file[key]["timestamp"], (
            f"{key}: chose a different request"
        )
        assert from_db[key]["seasons"] == from_file[key]["seasons"]
        assert from_db[key]["monitor"] == from_file[key]["monitor"]
        assert from_db[key]["media"] == from_file[key]["media"]


def test_the_newest_request_per_medium_still_wins():
    """Two requests for one show: the later one decides the monitoring state,
    and it is the one with the *aware* timestamp here - so the naive/aware mix
    must not change the ordering.
    """
    from plugins.media_cleanup.cog import MediaCleanupCog

    cog = object.__new__(MediaCleanupCog)
    latest = cog._latest_requests_by_media(FIXTURE)
    show = latest[("tv", 500)]
    assert show["seasons"] == "all", (
        "expected the 2026 request to win over the 2025 one"
    )


def test_resolving_a_request_does_not_change_the_monitoring_decision():
    """Recording an outcome is additive; it must not alter what cleanup decides."""
    from plugins.media_cleanup.cog import MediaCleanupCog

    path = _tmp_db()
    _no_legacy()
    tmpdir = tempfile.mkdtemp()
    _write_legacy(tmpdir)
    cog = object.__new__(MediaCleanupCog)

    async def scenario():
        await _init(path)
        before = cog._latest_requests_by_media(await all_requests())
        for key in FIXTURE:
            await mark_resolved(int(key), STATUS_APPROVED, "admin")
        after = cog._latest_requests_by_media(await all_requests())
        await _dispose()
        return before, after

    before, after = asyncio.run(scenario())
    assert set(before) == set(after)
    for key in before:
        assert before[key] == after[key], f"{key} changed after being resolved"


# ===================================================================
# pattern: nothing reads the old file any more
# ===================================================================

def test_no_plugin_still_reads_the_legacy_requests_file():
    root = pathlib.Path(conftest.PROJECT_ROOT)
    offenders = []
    for p in sorted((root / "plugins").rglob("*.py")):
        if "sync-conflict" in p.name:
            continue
        text = p.read_text()
        if "media_requests.json" in text or "REQUESTS_FILE" in text:
            offenders.append(p.relative_to(root).as_posix())
    assert offenders == [], (
        "these still reference the file the store replaced:\n  " + "\n  ".join(offenders)
    )
