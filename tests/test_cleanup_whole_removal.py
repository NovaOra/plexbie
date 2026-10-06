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
        season_search.POLL_SECONDS = 0
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
