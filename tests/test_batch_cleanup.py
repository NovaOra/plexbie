# path: tests/test_batch_cleanup.py
"""Pruned episode batches must actually be gone.

cleanup_old_batches removed batches from its in-memory dict and then called
save_tracking_data, which upserts the survivors - so every removed row stayed in
the database and was loaded straight back on the next restart. The log said
"Cleaned up 47 old batches" twenty times over, the same 47 each time, and 48 of 56
rows were stale by the time anyone counted.
"""
import asyncio
import json
import pathlib
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import io
import tokenize

import conftest  # noqa: F401


def _code(path: pathlib.Path) -> str:
    """Source with comments stripped.

    Matching raw text caught the explanatory comment that records *why* the
    reference was removed - the same way an earlier test in this project matched
    its own prose instead of the code it was auditing.
    """
    out = []
    with open(path, "rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type != tokenize.COMMENT:
                out.append(token.string)
    return "\n".join(out)

from database.kv_store import kv_delete_many, kv_get_all, kv_set_many

NAMESPACE = "new_media_tracking"


def _tmp_db(name="batches.db"):
    return str(pathlib.Path(tempfile.mkdtemp()) / name)


def _no_legacy():
    """Keep the media-requests migration out of these tests."""
    import database.session as session_module

    session_module._LEGACY_REQUESTS_FILE = (
        pathlib.Path(tempfile.mkdtemp()) / "absent.json"
    )


async def _init(path):
    import database.session as session_module

    if session_module.engine is not None:
        await session_module.engine.dispose()
    _no_legacy()
    await session_module.init_database(f"sqlite:///{path}")


async def _dispose():
    import database.session as session_module

    if session_module.engine is not None:
        await session_module.engine.dispose()


def _batch(key, days_old, monitored=False):
    when = datetime.now(timezone.utc) - timedelta(days=days_old)
    return key, {
        "show_title": key.split(":")[0],
        "season": 1,
        "episodes": [1],
        "last_update": when.isoformat(),
        "is_monitored": monitored,
        "channel_id": 1,
        "message_id": 2,
    }


# ===================================================================
# kv_delete_many
# ===================================================================

def test_delete_many_removes_exactly_the_named_keys():
    path = _tmp_db()

    async def scenario():
        await _init(path)
        await kv_set_many("ns", {"a": 1, "b": 2, "c": 3})
        removed = await kv_delete_many("ns", ["a", "c"])
        left = await kv_get_all("ns")
        await _dispose()
        return removed, left

    removed, left = asyncio.run(scenario())
    assert removed == 2
    assert set(left) == {"b"}


def test_delete_many_of_nothing_is_a_no_op():
    path = _tmp_db()

    async def scenario():
        await _init(path)
        await kv_set_many("ns", {"a": 1})
        removed = await kv_delete_many("ns", [])
        left = await kv_get_all("ns")
        await _dispose()
        return removed, left

    removed, left = asyncio.run(scenario())
    assert removed == 0 and set(left) == {"a"}


def test_delete_many_ignores_keys_that_are_not_there():
    path = _tmp_db()

    async def scenario():
        await _init(path)
        await kv_set_many("ns", {"a": 1})
        removed = await kv_delete_many("ns", ["a", "nope"])
        await _dispose()
        return removed

    assert asyncio.run(scenario()) == 1


def test_delete_many_does_not_touch_other_namespaces():
    path = _tmp_db()

    async def scenario():
        await _init(path)
        await kv_set_many("one", {"k": 1})
        await kv_set_many("two", {"k": 2})
        await kv_delete_many("one", ["k"])
        a, b = await kv_get_all("one"), await kv_get_all("two")
        await _dispose()
        return a, b

    a, b = asyncio.run(scenario())
    assert a == {} and b == {"k": 2}


# ===================================================================
# the cleanup loop
# ===================================================================

def _cog():
    from plugins.new_media_added.cog import NewMediaAddedCog

    cog = object.__new__(NewMediaAddedCog)
    cog.active_batches = {}
    cog._data_loaded = False
    cog.bot = None
    cog.services = None
    return cog


def _run_cleanup(path, seeded):
    from plugins.new_media_added.cog import NewMediaAddedCog

    async def scenario():
        await _init(path)
        await kv_set_many(NAMESPACE, dict(seeded))
        cog = _cog()
        await NewMediaAddedCog.cleanup_old_batches.coro(cog)
        in_db = await kv_get_all(NAMESPACE)
        in_memory = set(cog.active_batches)
        await _dispose()
        return in_db, in_memory

    return asyncio.run(scenario())


def test_a_pruned_batch_is_deleted_from_the_database():
    """The bug: it was only removed from memory, so a restart brought it back."""
    path = _tmp_db()
    seeded = dict([_batch("old show:s1", 5), _batch("fresh show:s1", 0)])
    in_db, in_memory = _run_cleanup(path, seeded)

    assert "old show:s1" not in in_db, (
        "the stale batch is still in the database and will be reloaded on restart"
    )
    assert "old show:s1" not in in_memory
    assert "fresh show:s1" in in_db, "a recent batch must be kept"


def test_the_prune_survives_a_restart():
    """The symptom that gave the bug away: the same batches cleaned over and over."""
    from plugins.new_media_added.cog import NewMediaAddedCog

    path = _tmp_db()
    seeded = dict([_batch("old:s1", 9), _batch("keep:s1", 0)])

    async def scenario():
        await _init(path)
        await kv_set_many(NAMESPACE, seeded)

        first = _cog()
        await NewMediaAddedCog.cleanup_old_batches.coro(first)

        # a fresh cog, as after a restart: it loads from the database
        second = _cog()
        await second.load_tracking_data()
        reloaded = set(second.active_batches)

        # and a second pass must find nothing left to do
        await NewMediaAddedCog.cleanup_old_batches.coro(second)
        after = await kv_get_all(NAMESPACE)
        await _dispose()
        return reloaded, after

    reloaded, after = asyncio.run(scenario())
    assert "old:s1" not in reloaded, "the batch came back after a restart"
    assert set(after) == {"keep:s1"}


def test_a_monitored_batch_is_never_pruned_however_old():
    """is_monitored means a requester is still waiting; dropping it would lose
    their "your request is available" notification.
    """
    path = _tmp_db()
    seeded = dict([_batch("awaited:s2", 400, monitored=True)])
    in_db, in_memory = _run_cleanup(path, seeded)
    assert set(in_db) == {"awaited:s2"}
    assert "awaited:s2" in in_memory


def test_a_recent_batch_is_kept():
    path = _tmp_db()
    seeded = dict([_batch("today:s1", 0)])
    in_db, _ = _run_cleanup(path, seeded)
    assert set(in_db) == {"today:s1"}


def test_nothing_to_prune_leaves_the_store_alone():
    path = _tmp_db()
    seeded = dict([_batch("a:s1", 0), _batch("b:s1", 0, monitored=True)])
    in_db, _ = _run_cleanup(path, seeded)
    assert set(in_db) == {"a:s1", "b:s1"}


def test_saving_after_a_prune_does_not_resurrect_anything():
    """save_tracking_data upserts, so it must never be asked to write a set that
    still contains a pruned key.
    """
    from plugins.new_media_added.cog import NewMediaAddedCog

    path = _tmp_db()
    seeded = dict([_batch("gone:s1", 30), _batch("stays:s1", 0)])

    async def scenario():
        await _init(path)
        await kv_set_many(NAMESPACE, seeded)
        cog = _cog()
        await NewMediaAddedCog.cleanup_old_batches.coro(cog)
        await cog.save_tracking_data()          # the dangerous follow-up
        after = await kv_get_all(NAMESPACE)
        await _dispose()
        return after

    after = asyncio.run(scenario())
    assert set(after) == {"stays:s1"}, f"resurrected: {sorted(set(after))}"


# ===================================================================
# the dead pruner is gone, and stays gone
# ===================================================================

def test_media_cleanup_no_longer_prunes_that_namespace():
    """Two writers on one store, one of them holding an in-memory copy, would
    resurrect whatever the other deleted. new_media_added owns it alone.
    """
    from plugins.media_cleanup.cog import MediaCleanupCog

    assert not hasattr(MediaCleanupCog, "_prune_new_media_tracking_cache"), (
        "reinstating this would fight new_media_added's in-memory cache"
    )
    root = pathlib.Path(conftest.PROJECT_ROOT)
    text = _code(root / "plugins" / "media_cleanup" / "cog.py")
    assert "new_media_tracking.json" not in text, (
        "media_cleanup still points at the abandoned JSON file"
    )


def test_the_summary_no_longer_reports_a_figure_it_cannot_produce():
    """It always said 0, because it was reading a 3-byte file."""
    root = pathlib.Path(conftest.PROJECT_ROOT)
    text = _code(root / "plugins" / "media_cleanup" / "cog.py")
    assert "new_media_tracking_pruned" not in text


def test_the_unused_tracking_pruner_is_gone():
    """cleanup_old_completed was defined and called from nowhere - easy to mistake
    for coverage that existed.
    """
    from core.media_tracking import MediaTrackingManager

    assert not hasattr(MediaTrackingManager, "cleanup_old_completed")
