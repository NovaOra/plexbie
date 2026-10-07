# path: tests/test_cleanup_safeguards.py
"""Media cleanup: what keeps a title, and how a removal goes.

The thresholds, the "keep forever" list, skipped libraries and a recent request are
all that stand between an idle title and its files being deleted; practice mode is
what a new install starts in. Each of these could stop working with every other
test still passing."""
import asyncio
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401

from core import media_tracking
from core.media_tracking import TrackedMedia
from test_cleanup_whole_removal import (Arr, BrokenArr, Guid, Movie, Seerr, Show, _cog, _doomed, _libraries,
                                        _removal, _warned)


def _idle(movie, days):
    """Added `days` days ago and never watched. An hour more, so a whole day is never
    lost to the seconds the test takes."""
    movie.addedAt = datetime.now(timezone.utc) - timedelta(days=days, hours=1)
    return movie


def _judged(days):
    return _cog().check_item_for_cleanup(_idle(Movie(1, "Dune", 2021, ["tmdb://438631"]), days), {})


def test_a_film_is_warned_about_from_its_last_week_and_due_at_90_days():
    assert _judged(82) is None
    for days, left in ((83, 7), (89, 1)):
        result = _judged(days)
        assert result["action"] == "notify" and result["days_until_deletion"] == left, (days, result)
    assert _judged(90)["action"] == "delete"
    assert _judged(500)["action"] == "delete"


def test_a_film_past_90_days_still_waits_out_its_whole_warning():
    cog = _cog()
    _libraries(cog, ("Movies", "movie", [_idle(Movie(1, "Dune", 2021, ["tmdb://438631"]), 90)]))
    warned = {}
    notify, delete = cog._scan_libraries_for_cleanup(warned)
    assert delete == [] and [(n["rating_key"], n["days_until_deletion"]) for n in notify] == [("1", 7)]
    assert set(warned) == {"1"}, "the day it was first warned about is kept"
    assert datetime.fromisoformat(warned["1"]) > datetime.now(timezone.utc) - timedelta(minutes=1), \
        "the warning starts today"

    warned = {"1": (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()}
    notify, delete = cog._scan_libraries_for_cleanup(warned)
    assert delete == [] and [n["days_until_deletion"] for n in notify] == [1]

    notify, delete = cog._scan_libraries_for_cleanup(_warned(1))
    assert notify == [] and [d["rating_key"] for d in delete] == ["1"]


def test_a_title_kept_forever_is_never_judged():
    cog = _cog()
    read = []

    class Untouchable(Show):
        def episodes(self):
            read.append(self.ratingKey)
            return super().episodes()
    show = Untouchable(76, [])
    show.addedAt = datetime.now(timezone.utc) - timedelta(days=500)
    assert cog.check_item_for_cleanup(show, {})["action"] == "delete", "due, but for the list"
    read.clear()

    cog.config["exempt_items"] = {"76": {"title": "The Boys", "type": "show"}}
    assert cog.check_item_for_cleanup(show, {}) is None
    assert read == [], "an exempt show's episodes were read"

    dune = _idle(Movie(1, "Dune", 2021, ["tmdb://438631"]), 500)
    assert cog.check_item_for_cleanup(dune, {})["action"] == "delete"
    cog.config["exempt_items"]["1"] = {"title": "Dune", "type": "movie"}
    assert cog.check_item_for_cleanup(dune, {}) is None


def test_a_skipped_library_is_never_judged_or_listed():
    cog = _cog()
    judged = []
    check = cog.check_item_for_cleanup

    def spy(item, views=None):
        judged.append(item.ratingKey)
        return check(item, views)
    cog.check_item_for_cleanup = spy
    _libraries(cog, ("Movies", "movie", [_idle(Movie(1, "Dune", 2021, ["tmdb://438631"]), 500)]),
               ("Kids", "movie", [_idle(Movie(2, "Heat", 1995, ["tmdb://949"]), 500),
                                  _idle(Movie(3, "Solaris", 2002, ["tmdb://2103"]), 85)]),
               excluded=["Kids"])
    notify, delete = cog._scan_libraries_for_cleanup(_warned(1, 2, 3))
    assert judged == [1]
    assert notify == [] and [d["rating_key"] for d in delete] == ["1"]


class _Tracker:
    def __init__(self, *media):
        self.tracked_media = {str(n): m for n, m in enumerate(media)}


def _requested(tmdb, kind, title, days):
    return TrackedMedia(tmdb_id=tmdb, media_type=kind, title=title,
                        request_timestamp=(datetime.now(timezone.utc) - timedelta(days=days)).isoformat())


def _judged_with_requests(item, *requests):
    cog = _cog()
    del cog._get_recent_request_timestamp           # the real lookup, against these requests
    saved = media_tracking.get_media_tracker
    media_tracking.get_media_tracker = lambda: _Tracker(*requests)
    try:
        return cog.check_item_for_cleanup(item, {})
    finally:
        media_tracking.get_media_tracker = saved


def test_a_request_three_days_ago_keeps_a_title_idle_for_500_days():
    dune = _idle(Movie(1, "Dune", 2021, ["tmdb://438631"]), 500)
    assert _judged_with_requests(dune, _requested(438631, "movie", "Dune", 3)) is None
    show = Show(76, [])
    show.addedAt = datetime.now(timezone.utc) - timedelta(days=500)
    assert _judged_with_requests(show, _requested(0, "tv", "The Boys", 3)) is None, "matched by title without ids"


def test_an_old_request_or_one_for_another_title_keeps_nothing():
    dune = _idle(Movie(1, "Dune", 2021, ["tmdb://438631"]), 500)
    assert _judged_with_requests(dune, _requested(438631, "movie", "Dune", 200))["action"] == "delete"
    assert _judged_with_requests(dune, _requested(949, "movie", "Dune", 3))["action"] == "delete", \
        "another film by the same name"


def test_a_show_whose_episodes_cant_be_read_is_kept():
    class Unreadable(Show):
        def episodes(self):
            raise ConnectionError("Plex is down")
    show = Unreadable(76, [])
    show.addedAt = datetime.now(timezone.utc) - timedelta(days=500)
    assert _cog().check_item_for_cleanup(show, {}) is None


class _Untouchable:
    """Sonarr/Radarr or Seerr in practice mode: any call fails the test."""
    configured, name = True, "Radarr"

    def __init__(self):
        self.calls = []

    async def get(self, path, **params):
        self.calls.append(("get", path))
        raise AssertionError(f"practice mode read {path}")

    async def delete(self, path, **params):
        self.calls.append(("delete", path))
        raise AssertionError(f"practice mode deleted {path}")


def test_practice_mode_deletes_nothing_anywhere():
    radarr, sonarr, seerr = _Untouchable(), _Untouchable(), _Untouchable()
    cog = _removal(radarr, seerr)
    cog.services.sonarr = sonarr
    cog.config["dry_run"] = True

    class Kept(Movie):
        def delete(self):
            raise AssertionError("practice mode deleted Plex's copy")
    movie = Kept(1, "Dune", 2021, ["tmdb://438631"])
    show = Show(76, [])
    show.delete = movie.delete
    items = [_doomed(movie), {"item": show, "type": "show", "rating_key": "76", "title": "The Boys"}]
    assert [i["title"] for i in asyncio.run(cog.delete_media_items(items))] == ["Dune", "The Boys"], \
        "reported as what would go"
    assert radarr.calls == sonarr.calls == seerr.calls == []
    assert cog.tracking_data == {}


class _RecordingArr(Arr):
    def __init__(self, name, rows):
        super().__init__(name, rows)
        self.params = []

    async def delete(self, path, **params):
        self.params.append(params)
        await super().delete(path, **params)


def test_removing_a_film_deletes_its_files_through_radarr():
    radarr = _RecordingArr("Radarr", [{"id": 7, "title": "Dune", "year": 2021, "tmdbId": 438631}])
    cog = _removal(radarr)
    movie = Movie(1, "Dune", 2021, ["tmdb://438631"])
    assert len(asyncio.run(cog.delete_media_items([_doomed(movie)]))) == 1
    assert radarr.deleted == ["movie/7"] and radarr.params == [{"deleteFiles": "true", "addImportExclusion": "false"}]
    assert movie.deleted and cog.tracking_data["1"]["type"] == "movie"


def test_a_film_plex_has_no_ids_for_is_found_by_title_and_year():
    for rows in ([{"id": 1, "title": "Dune", "year": 1984}, {"id": 2, "title": "Dune", "year": 2021}],
                 [{"id": 2, "title": "Dune", "year": 2021}, {"id": 1, "title": "Dune", "year": 1984}]):
        radarr = Arr("Radarr", rows)
        asyncio.run(_removal(radarr).delete_media_items([_doomed(Movie(1, "Dune", 2021))]))
        assert radarr.deleted == ["movie/2"], rows


def test_a_sonarr_error_keeps_the_show_too():
    seerr = Seerr([{"id": 133, "tmdbId": 76479, "mediaType": "tv", "status": 5}])
    cog = _removal(Arr("Radarr", []), seerr)
    cog.services.sonarr = BrokenArr("Sonarr", [])
    removed = []
    show = Show(76, [])
    show.guids = [Guid("tmdb://76479")]
    show.delete = lambda: removed.append(76)
    item = {"item": show, "type": "show", "rating_key": "76", "title": "The Boys"}
    assert asyncio.run(cog.delete_media_items([item])) == []
    assert removed == [] and seerr.deleted == [] and cog.tracking_data == {}
