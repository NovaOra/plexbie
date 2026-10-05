# path: tests/test_tracking_load.py
"""Episode batches must survive a restart, whatever else is in their namespace.

1.0 kept the movie de-dupe map as an "announced" row among the episode batches.
Loading the batches read every dict there as a batch, hit "announced" (no
show_title) and gave up: "Error loading tracking data: 'show_title'" on every
start and every hourly cleanup, with every batch saved after that row forgotten -
so the next episode of a season got a fresh post (and ping) instead of an edit.
"""
import asyncio
from datetime import datetime, timezone

import conftest  # noqa: F401

from database.kv_store import kv_get, kv_get_all, kv_set_many
from test_arrival_announcements import _cog
from test_batch_cleanup import _batch, _dispose, _init, _tmp_db

from plugins.new_media_added import cog as module


def _fresh_cog():
    """A cog that really reads and writes the database."""
    cog, channel = _cog(season_size=3)
    del cog.load_tracking_data, cog.save_tracking_data
    cog._data_loaded = False
    return cog, channel


def test_the_live_announced_row_no_longer_breaks_loading():
    """The exact row found on TheHub, next to a batch and one unreadable record."""
    path = _tmp_db()
    when = datetime.now(timezone.utc).isoformat()

    async def scenario():
        await _init(path)
        await kv_set_many(module.NEW_MEDIA_NAMESPACE, {
            "announced": {"movie:10193": when, "movie:301528": when},
            "coven:s1": _batch("coven:s1", 0)[1],
            "broken:s2": {"season": 2, "episodes": [1]},
        })
        cog, _ = _fresh_cog()
        await cog.load_tracking_data()
        moved = await kv_get(module.ANNOUNCED_NAMESPACE, module.ANNOUNCED_KEY)
        left = await kv_get_all(module.NEW_MEDIA_NAMESPACE)
        await _dispose()
        return cog, moved, left

    cog, moved, left = asyncio.run(scenario())
    assert cog._data_loaded, "one bad row must not leave the whole load failed"
    assert set(cog.active_batches) == {"coven:s1"}
    assert moved == {"movie:10193": when, "movie:301528": when}, "movie de-dupe times are kept"
    assert "announced" not in left
    assert "broken:s2" in left, "an unreadable batch is skipped, not deleted"


def test_a_movie_then_an_episode_still_edits_after_a_restart():
    """End to end: the movie announcement no longer lands among the batches."""
    path = _tmp_db()
    meta = lambda n: {"grandparentTitle": "Coven", "parentIndex": 1, "index": n, "title": "Ep", "Guid": []}

    async def scenario():
        await _init(path)
        first, channel = _fresh_cog()
        await first.handle_plex_webhook({"event": "library.new", "Metadata": {
            "type": "movie", "title": "Film", "Guid": [{"id": "tmdb://42"}]}})
        await first._handle_new_episode(meta(1), {})

        rows = await kv_get_all(module.NEW_MEDIA_NAMESPACE)

        restarted, again = _fresh_cog()
        await restarted._handle_new_episode(meta(2), {})
        await restarted.handle_plex_webhook({"event": "library.new", "Metadata": {
            "type": "movie", "title": "Film", "Guid": [{"id": "tmdb://42"}]}})
        await _dispose()
        return channel, rows, restarted, again

    channel, rows, restarted, again = asyncio.run(scenario())
    assert len(channel.posts) == 2, "the movie and the first episode"
    assert set(rows) == {"coven:s1"}, f"only batches belong here, found {sorted(rows)}"
    assert restarted._data_loaded and "coven:s1" in restarted.active_batches
    assert not again.posts, "after a restart: an edit for episode 2, no second movie post"
    assert len(again.edits) == 1
