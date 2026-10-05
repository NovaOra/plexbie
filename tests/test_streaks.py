# path: tests/test_streaks.py
"""Watch-streak bookkeeping, and the ghosts it used to accumulate.

update_watch_streaks only ever added and updated, never removed, so the file grew
entries for accounts that no longer exist: renamed accounts, removed users, and an
empty-string key left behind by Plex's nameless /accounts/0 sentinel. Seven of
them had built up on the live server.
"""
import asyncio
import json
import pathlib
import tempfile
from datetime import datetime

import conftest  # noqa: F401


def _cog(streaks_file):
    from plugins.watch_tracking import cog as module
    from plugins.watch_tracking.cog import WatchTrackingCog

    module.STREAKS_FILE = streaks_file
    cog = object.__new__(WatchTrackingCog)
    cog.bot = None
    cog._aliases_cache = {}
    cog._aliases_stamp = None
    return module, cog


def _run(cog, module, streaks_file, existing, watched_by_user):
    """Drive one pass of update_watch_streaks with a stubbed Plex walk."""
    from plugins.watch_tracking.cog import WatchTrackingCog

    streaks_file.write_text(json.dumps(existing, indent=2))

    class FakeServices:
        plex_server = object()

        async def reconnect_plex(self):
            return True

    cog.services = FakeServices()

    original = module._collect_watched_today
    module._collect_watched_today = lambda plex: watched_by_user
    try:
        asyncio.run(WatchTrackingCog.update_watch_streaks.coro(cog))
    finally:
        module._collect_watched_today = original

    return json.loads(streaks_file.read_text())


def test_entries_for_departed_accounts_are_pruned():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "watch_streaks.json"
    module, cog = _cog(tmp)

    existing = {
        "NovaOra": {"current": 2, "longest": 3, "last_watched": "2026-09-21"},
        "Dave": {"current": 0, "longest": 0, "last_watched": None},
        "Qbert": {"current": 0, "longest": 1, "last_watched": "2026-01-24"},
        "": {"current": 0, "longest": 3, "last_watched": "2026-09-25"},
    }
    result = _run(cog, module, tmp, existing, {"NovaOra": False})

    assert set(result) == {"NovaOra"}, f"ghosts survived: {sorted(result)}"
    assert result["NovaOra"]["longest"] == 3, "a surviving entry must keep its history"


def test_the_empty_string_key_is_removed():
    """Plex account id 0 has no name; it is a sentinel, not a user."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "watch_streaks.json"
    module, cog = _cog(tmp)
    result = _run(cog, module, tmp, {"": {"current": 0, "longest": 9,
                                          "last_watched": "2026-09-25"}},
                  {"real": False})
    assert "" not in result


def test_current_accounts_are_never_pruned_even_with_no_history():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "watch_streaks.json"
    module, cog = _cog(tmp)
    result = _run(cog, module, tmp, {}, {"newcomer": False, "other": False})
    assert set(result) == {"newcomer", "other"}, (
        "accounts Plex still reports must get an entry, not be pruned"
    )


def test_a_watch_today_still_extends_the_streak():
    """Pruning must not disturb the thing the loop is actually for."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "watch_streaks.json"
    module, cog = _cog(tmp)
    yesterday = (datetime.now().date().toordinal()) - 1
    from datetime import date
    y = date.fromordinal(yesterday).isoformat()

    result = _run(
        cog, module, tmp,
        {"keen": {"current": 4, "longest": 4, "last_watched": y}},
        {"keen": True},
    )
    assert result["keen"]["current"] == 5
    assert result["keen"]["longest"] == 5


def test_pruning_survives_an_empty_plex_response():
    """If Plex reports nothing, do not wipe the file wholesale."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "watch_streaks.json"
    module, cog = _cog(tmp)
    existing = {"NovaOra": {"current": 1, "longest": 1, "last_watched": "2026-09-21"}}
    result = _run(cog, module, tmp, existing, {})
    assert result == existing, (
        "an empty Plex response is not evidence that every account left; the file "
        "must be left alone rather than wiped"
    )


def test_watching_again_the_same_day_keeps_the_streak():
    """Live updates refresh streaks after every stop: the second run of a day must not reset it."""
    import pathlib
    text = (pathlib.Path(conftest.PROJECT_ROOT) / "plugins/watch_tracking/cog.py").read_text()
    assert "== today:" in text and "not in (today, yesterday)" in text
