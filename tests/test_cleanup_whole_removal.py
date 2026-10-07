# path: tests/test_cleanup_whole_removal.py
"""Media cleanup: what counts as watched, and removing a title everywhere.

The Boys was removed three times after 90 days. Each time Sonarr and Plex lost it but
Seerr kept the old request, so the next request ("Season 1") was refused as "no seasons
available to request" and never reached Sonarr. And Plex's lastViewedAt is only the
owner's: a show the household was watching looked untouched."""
import asyncio
from datetime import datetime, timedelta

import conftest  # noqa: F401

from plugins.media_cleanup.cog import MediaCleanupCog


def _cog(history=None, history_fails=False):
    cog = object.__new__(MediaCleanupCog)
    cog.config = {"inactivity_days": 90, "notify_days_before": 7, "exempt_items": {}, "dry_run": False,
                  "exclude_libraries": []}
    cog._get_recent_request_timestamp = lambda item: None

    class H:
        def __init__(self, key, days, show=None):
            self.ratingKey, self.grandparentRatingKey = key, show
            self.viewedAt = datetime.now() - timedelta(days=days)

    class Server:
        def history(self, mindate=None):
            if history_fails:
                raise ConnectionError("Plex is down")
            return [H(*h) for h in history or []]

    class Services:
        plex_server = Server()
    cog.services = Services()
    return cog


class Episode:
    def __init__(self, viewed=None, added=400):
        self.lastViewedAt = datetime.now() - timedelta(days=viewed) if viewed is not None else None
        self.addedAt = datetime.now() - timedelta(days=added)


class Show:
    type = "show"

    def __init__(self, key, eps, title="The Boys"):
        self.ratingKey, self.title, self._eps = key, title, eps
        self.addedAt = datetime.now() - timedelta(days=500)

    def episodes(self):
        return self._eps


def test_a_show_the_household_watches_is_kept_though_the_owner_never_did():
    cog = _cog(history=[("e9", 5, 76)])                     # a member watched an episode 5 days ago
    views = cog._everyones_views()
    assert cog.check_item_for_cleanup(Show(76, [Episode(viewed=None), Episode(viewed=None)]), views) is None
    assert cog.check_item_for_cleanup(Show(77, [Episode(viewed=None)]), views)["action"] == "delete"


def test_any_episode_keeps_the_whole_show_and_a_new_season_counts_as_fresh():
    cog = _cog()
    nine_seasons = [Episode(viewed=None) for _ in range(80)] + [Episode(viewed=3)]   # someone's on season 9
    assert cog.check_item_for_cleanup(Show(1, nine_seasons), {}) is None
    new_season = [Episode(viewed=None, added=400) for _ in range(10)] + [Episode(viewed=None, added=10)]
    assert cog.check_item_for_cleanup(Show(2, new_season), {}) is None, "a season added 10 days ago isn't stale"


def test_no_history_means_nothing_is_deleted_that_day():
    cog = _cog(history_fails=True)

    class Lib:
        title, type = "TV", "show"

        def all(self):
            return [Show(1, [Episode(viewed=None)])]

    class Library:
        def sections(self):
            return [Lib()]
    cog.services.plex_server.library = Library()
    assert cog._scan_libraries_for_cleanup() == ([], [])


class Arr:
    configured = True

    def __init__(self, name, rows):
        self.name, self.rows, self.deleted = name, rows, []

    async def get(self, path, **params):
        return self.rows

    async def delete(self, path, **params):
        self.deleted.append(path)

    async def series(self):
        return self.rows

    async def movies(self):
        return self.rows


class Seerr:
    configured = True

    def __init__(self, media):
        self.media, self.deleted = media, []

    async def get(self, path, **params):
        if path == "media":
            skip = params.get("skip", 0)
            return {"results": self.media[skip:skip + params.get("take", 100)], "pageInfo": {"results": len(self.media)}}
        tmdb = int(path.split("/")[1])
        m = next((x for x in self.media if x["tmdbId"] == tmdb), None)
        return {"mediaInfo": {"id": m["id"]} if m else None}

    async def delete(self, path):
        self.deleted.append(path)


def test_removing_a_show_finds_it_in_sonarr_by_id_and_clears_it_from_seerr():
    cog = _cog()
    sonarr = Arr("Sonarr", [{"id": 68, "title": "The Boys (2019)", "tvdbId": 355567, "tmdbId": 76479}])
    seerr = Seerr([{"id": 133, "tmdbId": 76479, "tvdbId": 355567, "mediaType": "tv", "status": 7}])
    cog.services.sonarr, cog.services.seerr = sonarr, seerr
    cog.tracking_data = {}

    async def nothing():
        return None
    cog.save_tracking_data = nothing

    class G:
        def __init__(self, i):
            self.id = i
    show = Show(76, [])
    show.guids = [G("tvdb://355567"), G("tmdb://76479")]
    show.delete = lambda: None
    asyncio.run(cog.delete_media_items([{"item": show, "type": "show", "rating_key": "76", "title": "The Boys"}]))
    assert sonarr.deleted == ["series/68"], "found by TheTVDB id, though Sonarr's title differs"
    assert seerr.deleted == ["media/133"], "Seerr's old request goes too, so it can be asked for again"
    assert cog.tracking_data["76"]["tmdb"] == "76479"


def test_seerrs_leftovers_from_earlier_removals_are_cleared_unless_still_in_sonarr_or_radarr():
    cog = _cog()
    cog.services.sonarr = Arr("Sonarr", [{"id": 5, "tvdbId": 111, "tmdbId": 222}])
    cog.services.radarr = Arr("Radarr", [{"id": 9, "tmdbId": 333}])
    seerr = Seerr([
        {"id": 1, "tmdbId": 76479, "tvdbId": 355567, "mediaType": "tv", "status": 7},   # The Boys: gone everywhere
        {"id": 2, "tmdbId": 222, "tvdbId": 111, "mediaType": "tv", "status": 7},       # still in Sonarr
        {"id": 3, "tmdbId": 333, "mediaType": "movie", "status": 7},                   # still in Radarr
        {"id": 4, "tmdbId": 444, "mediaType": "movie", "status": 5},                   # available: not touched
        {"id": 5, "tmdbId": 555, "mediaType": "movie", "status": 7},                   # gone
    ])
    cog.services.seerr = seerr
    assert asyncio.run(cog.reconcile_seerr()) == 2
    assert seerr.deleted == ["media/1", "media/5"]


class Guid:
    def __init__(self, i):
        self.id = i


class Movie:
    type = "movie"

    def __init__(self, key, title, year=None, guids=(), delete_fails=False):
        self.ratingKey, self.title, self.year = key, title, year
        self.guids = [Guid(g) for g in guids]
        self.addedAt = datetime.now() - timedelta(days=400)
        self.lastViewedAt = None
        self.delete_fails, self.deleted = delete_fails, False

    def delete(self):
        if self.delete_fails:
            raise ConnectionError("Plex is down")
        self.deleted = True


class BrokenArr(Arr):
    async def get(self, path, **params):
        from core.clients import ServiceError
        raise ServiceError("Radarr answered 503", 503)


class RefusingArr(Arr):
    async def delete(self, path, **params):
        from core.clients import ServiceError
        raise ServiceError("Radarr answered 500", 500)


def _removal(radarr, seerr=None):
    cog = _cog()
    cog.services.radarr = radarr
    cog.services.seerr = seerr or Seerr([])
    cog.tracking_data = {}

    async def nothing():
        return None
    cog.save_tracking_data = nothing
    return cog


def _doomed(movie):
    return {"item": movie, "type": "movie", "rating_key": str(movie.ratingKey), "title": movie.title,
            "days_inactive": 120}


def test_removal_reads_plex_ids_and_year_off_the_event_loop():
    """plexapi fetches an unset attribute of a partial object over HTTP: reading guids or
    year while deleting would block the loop (and the Discord heartbeat) for each title."""
    def off_the_loop():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise AssertionError("a lazy Plex attribute was read on the event loop")

    class LazyMovie(Movie):
        @property
        def guids(self):
            off_the_loop()
            return self._guids

        @guids.setter
        def guids(self, value):
            self._guids = value

        @property
        def year(self):
            off_the_loop()
            return self._year

        @year.setter
        def year(self, value):
            self._year = value

    radarr = Arr("Radarr", [{"id": 7, "title": "Dune", "year": 2021, "tmdbId": 438631}])
    cog = _removal(radarr)
    movie = LazyMovie(1, "Dune", 2021, ["tmdb://438631"])
    doomed = cog.check_item_for_cleanup(movie, {})            # in the worker thread, as the scan runs it
    assert doomed["action"] == "delete" and doomed["ids"] == {"tmdb": "438631"} and doomed["year"] == 2021
    deleted = asyncio.run(cog.delete_media_items([doomed]))
    assert radarr.deleted == ["movie/7"] and len(deleted) == 1


def test_a_sonarr_or_radarr_error_deletes_nothing_and_counts_nothing():
    """With Radarr down, Plex's copy went anyway: the files were gone while Radarr still
    monitored the title, and it was announced as removed. Now it waits for the next day."""
    seerr = Seerr([{"id": 133, "tmdbId": 438631, "mediaType": "movie", "status": 5}])
    cog = _removal(BrokenArr("Radarr", []), seerr)
    movie = Movie(1, "Dune", 2021, ["tmdb://438631"])
    assert asyncio.run(cog.delete_media_items([_doomed(movie)])) == []
    assert not movie.deleted, "no Plex-only delete while Radarr can't be asked"
    assert seerr.deleted == [] and cog.tracking_data == {}


def test_a_failed_sonarr_or_radarr_delete_keeps_the_title_too():
    seerr = Seerr([{"id": 133, "tmdbId": 438631, "mediaType": "movie", "status": 5}])
    radarr = RefusingArr("Radarr", [{"id": 7, "title": "Dune", "year": 2021, "tmdbId": 438631}])
    cog = _removal(radarr, seerr)
    movie = Movie(1, "Dune", 2021, ["tmdb://438631"])
    assert asyncio.run(cog.delete_media_items([_doomed(movie)])) == []
    assert not movie.deleted, "no Plex-only delete when Radarr found it but couldn't remove it"
    assert seerr.deleted == [] and cog.tracking_data == {}


def test_a_scan_says_how_many_titles_were_kept():
    from test_cleanup_settings_load import ADMIN, _website
    cog = _removal(BrokenArr("Radarr", []))
    movie = Movie(1, "Dune", 2021, ["tmdb://438631"])

    async def ready():
        return True

    async def monitor():
        return {}

    async def quiet(items, kind):
        pass
    cog.load_data, cog.enforce_request_monitor_cleanup, cog.send_cleanup_notification = ready, monitor, quiet
    cog._scan_libraries_for_cleanup = lambda: ([], [_doomed(movie)])
    out = asyncio.run(_website(cog).cleanup_scan(ADMIN))
    assert out["ok"] and "0 were removed" in out["message"], out
    assert "1 couldn't be removed and will be tried again at the next check" in out["message"], out


def test_not_in_radarr_but_removed_from_plex_counts_as_deleted():
    cog = _removal(Arr("Radarr", []))
    movie = Movie(1, "Home Video", 2020)
    assert len(asyncio.run(cog.delete_media_items([_doomed(movie)]))) == 1
    assert movie.deleted and "1" in cog.tracking_data


def test_a_title_neither_radarr_nor_plex_removed_is_not_counted():
    seerr = Seerr([{"id": 133, "tmdbId": 438631, "mediaType": "movie", "status": 5}])
    cog = _removal(Arr("Radarr", []), seerr)
    movie = Movie(1, "Dune", 2021, ["tmdb://438631"], delete_fails=True)
    assert asyncio.run(cog.delete_media_items([_doomed(movie)])) == []
    assert seerr.deleted == [], "Seerr keeps its record of a title still on disk"
    assert cog.tracking_data == {}


def test_the_title_fallback_needs_one_exact_match_with_the_same_year_and_no_conflicting_id():
    rows = [{"id": 1, "title": "Dune", "year": 1984, "tmdbId": 841},
            {"id": 2, "title": "Dune", "year": 2021, "tmdbId": 438631},
            {"id": 3, "title": "Solaris", "year": 2002},
            {"id": 4, "title": "Solaris", "year": 2002},
            {"id": 5, "title": "Heat", "year": 1995, "imdbId": "tt0113277"}]

    def removed(movie):
        radarr = Arr("Radarr", rows)
        asyncio.run(_removal(radarr).delete_media_items([_doomed(movie)]))
        return radarr.deleted, movie.deleted

    assert removed(Movie(1, "Dune", 2021, ["tmdb://999"])) == ([], True), "another film: only Plex has it"
    assert removed(Movie(1, "Dune")) == ([], False), "no year in Plex: either Dune could be meant, so it's kept"
    assert removed(Movie(1, "Solaris", 2002)) == ([], False), "two entries fit: neither is guessed, so it's kept"
    assert removed(Movie(1, "Heat", 1995, ["tmdb://949"])) == (["movie/5"], True), "no conflicting id: still found"


def test_a_request_that_never_reaches_sonarr_opens_a_ticket():
    """Seerr refused The Boys ("no seasons available to request") and Plexbie took it as
    handled; ten minutes on, it still wasn't in Sonarr and nobody was told."""
    from core import season_search
    told = []

    class Sonarr:
        async def series(self):
            return []

    class Services:
        sonarr = Sonarr()

    async def missing():
        told.append("ticket")

    async def body():
        saved = season_search.POLL_SECONDS
        season_search.POLL_SECONDS = 0.001
        try:
            season_search.follow_up_new_show(Services(), tmdb_id=76479, seasons=[1], title="The Boys",
                                             wait_for_sonarr=0, on_missing=missing)
            for _ in range(50):
                await asyncio.sleep(0.01)
                if told:
                    break
        finally:
            season_search.POLL_SECONDS = saved
    asyncio.run(body())
    assert told == ["ticket"]
