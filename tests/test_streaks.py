# path: tests/test_streaks.py
"""Watch-streak bookkeeping, and the ghosts it used to accumulate.

update_watch_streaks only ever added and updated, never removed, so the file grew
entries for accounts that no longer exist: renamed accounts, removed users, and an
empty-string key left behind by Plex's nameless /accounts/0 sentinel. Seven of
them had built up on the live server.
"""
import asyncio
import json
import logging
import os
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
    streaks_file.write_text(json.dumps(existing, indent=2))
    _pass(cog, module, watched_by_user)
    return json.loads(streaks_file.read_text())


def _pass(cog, module, watched_by_user):
    from plugins.watch_tracking.cog import WatchTrackingCog

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


class _Logged:
    """Collects what the watch_tracking cog logs."""

    def __enter__(self):
        self.records = []
        self.logger = logging.getLogger("plugins.watch_tracking.cog")
        self.handler = logging.Handler()
        self.handler.emit = self.records.append
        self.logger.addHandler(self.handler)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)


def test_a_corrupt_file_is_logged_and_left_alone():
    """An unreadable file must not be read as {} and written back over the history."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "watch_streaks.json"
    module, cog = _cog(tmp)
    broken = '{"keen": {"current": 40, "longest": 90, "last_wat'
    tmp.write_text(broken)

    with _Logged() as log:
        _pass(cog, module, {"keen": True, "other": False})

    assert tmp.read_text() == broken, (
        "a half-written file was replaced by this hour's state, losing every streak"
    )
    assert any(r.levelno >= logging.ERROR and str(tmp) in r.getMessage()
               for r in log.records), "a corrupt streaks file must be logged"


def test_a_failed_write_keeps_the_previous_file():
    """The file is replaced in one step: a write that fails leaves the old one whole."""
    tmp = pathlib.Path(tempfile.mkdtemp()) / "watch_streaks.json"
    module, cog = _cog(tmp)
    existing = {"keen": {"current": 4, "longest": 9, "last_watched": "2026-09-21"}}
    tmp.write_text(json.dumps(existing, indent=2))

    def full_disk(src, dst):
        raise OSError(28, "No space left on device")

    real = os.replace
    os.replace = full_disk
    try:
        _pass(cog, module, {"keen": True})
    finally:
        os.replace = real

    assert json.loads(tmp.read_text()) == existing
    assert sorted(p.name for p in tmp.parent.iterdir()) == [tmp.name], (
        "a failed write must not leave its temp file behind"
    )


def test_overlapping_saves_never_mix_their_output():
    """The hourly update and a refresh after a watch can save at the same moment."""
    from utils import standings

    target = pathlib.Path(tempfile.mkdtemp()) / "watch_streaks.json"
    first = {"keen": {"current": 4, "longest": 9, "last_watched": "2026-09-21"}}
    second = {"keen": {"current": 5, "longest": 9, "last_watched": "2026-09-22"}}
    real_dump = json.dump
    interrupted = []

    def dump_with_a_second_save_midway(obj, f, **kwargs):
        if interrupted:
            return real_dump(obj, f, **kwargs)
        interrupted.append(True)
        text = json.dumps(obj, **kwargs)
        f.write(text[:len(text) // 2])
        f.flush()
        standings.save_streaks(second, target)
        f.write(text[len(text) // 2:])

    json.dump = dump_with_a_second_save_midway
    try:
        standings.save_streaks(first, target)
    finally:
        json.dump = real_dump

    assert json.loads(target.read_text()) in (first, second)
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_the_streaks_file_is_read_and_written_off_the_loop():
    tmp = pathlib.Path(tempfile.mkdtemp()) / "watch_streaks.json"
    module, cog = _cog(tmp)
    tmp.write_text("{}")
    ran = []
    real = module.run_blocking

    async def recording(func, *args, **kwargs):
        ran.append(getattr(func, "__name__", repr(func)))
        return await real(func, *args, **kwargs)

    module.run_blocking = recording
    try:
        _pass(cog, module, {"keen": True})
    finally:
        module.run_blocking = real
    assert "load_streaks" in ran and "save_streaks" in ran, ran


def test_load_streaks_reads_missing_and_corrupt_files_as_empty():
    """The display and the website only show streaks: a bad file shows none."""
    from utils.standings import load_streaks

    folder = pathlib.Path(tempfile.mkdtemp())
    assert load_streaks(folder / "absent.json") == {}

    corrupt = folder / "corrupt.json"
    corrupt.write_text("{ not json")
    assert load_streaks(corrupt) == {}

    wrong_shape = folder / "list.json"
    wrong_shape.write_text("[1, 2]")
    assert load_streaks(wrong_shape) == {}

    good = folder / "good.json"
    good.write_text('{"keen": {"current": 1, "longest": 2, "last_watched": null}}')
    assert load_streaks(good) == {"keen": {"current": 1, "longest": 2, "last_watched": None}}


def test_load_streaks_strict_raises_on_a_corrupt_file():
    """The hourly update needs to tell "no file yet" from "a file it cannot read"."""
    from utils.standings import load_streaks

    folder = pathlib.Path(tempfile.mkdtemp())
    assert load_streaks(folder / "absent.json", strict=True) == {}

    corrupt = folder / "corrupt.json"
    corrupt.write_text("{ not json")
    try:
        load_streaks(corrupt, strict=True)
    except ValueError:
        pass
    else:
        raise AssertionError("a corrupt file must raise in strict mode")


def test_the_website_reads_streaks_through_the_shared_loader():
    from portal import data
    from utils import standings

    assert data.load_streaks is standings.load_streaks
    assert not hasattr(data, "_read_streaks")
