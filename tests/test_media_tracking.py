# path: tests/test_media_tracking.py
"""Requests are tracked by media type and TMDB id.

A show requested with all its seasons used to be filed under movie:{id}, where
no episode arrival ever looked, so its requester was never told it was on Plex.
The same key let a film and a show sharing a TMDB id overwrite each other, and a
Radarr grab land on the show.
"""
import asyncio
import json
import tempfile
from pathlib import Path

import conftest  # noqa: F401

from core import media_tracking


class _Tracker:
    """A fresh tracker on a scratch file, put in as the bot-wide one."""

    def __init__(self, saved=None):
        self.path = Path(tempfile.mkdtemp()) / "media_tracking.json"
        if saved is not None:
            self.path.write_text(json.dumps(saved))

    def __enter__(self):
        self._file, self._manager = media_tracking.TRACKING_FILE, media_tracking._tracking_manager
        media_tracking.TRACKING_FILE = self.path
        media_tracking._tracking_manager = media_tracking.MediaTrackingManager()
        return media_tracking._tracking_manager

    def __exit__(self, *exc):
        media_tracking.TRACKING_FILE, media_tracking._tracking_manager = self._file, self._manager


def _request_all_seasons(tracker, tmdb_id=100, title="Bluey"):
    return tracker.register_request(tmdb_id=tmdb_id, media_type="tv", title=title, requester_user_id=5,
                                    season_number=None, requested_seasons="all")


def test_an_all_seasons_request_gets_its_arrival_dm():
    from test_arrival_announcements import _cog
    cog, _ = _cog(season_size=3)
    del cog._check_if_monitored                  # the real lookup, against the tracker
    told = []

    async def dm(tracked, media_kind, detail=None):
        told.append((tracked.title, media_kind, detail))
    cog._send_requester_availability_dm = dm

    with _Tracker() as tracker:
        _request_all_seasons(tracker)
        meta = {"grandparentTitle": "Bluey", "grandparentGuid": "tmdb://100", "parentIndex": 2,
                "index": 1, "title": "Dance Mode", "Guid": []}
        asyncio.run(cog._handle_new_episode(meta, {}))

    assert told == [("Bluey", "tv", "Season 2, Episode 1")], "the requester is told the show is on Plex"


def test_an_all_seasons_request_is_not_announced_on_the_specials():
    with _Tracker() as tracker:
        show = _request_all_seasons(tracker)
        assert not show.should_notify_for_episode_arrival(0, 1), "Season 0 is specials"
        assert show.should_notify_for_episode_arrival(1, 1)
        assert not show.should_notify_for_episode_arrival(1, 2)


def test_a_film_and_a_show_with_the_same_tmdb_id_are_kept_apart():
    with _Tracker() as tracker:
        tracker.register_request(tmdb_id=42, media_type="movie", title="The Film", requester_user_id=1)
        _request_all_seasons(tracker, tmdb_id=42, title="The Show")
        assert len(tracker.tracked_media) == 2, "the show must not replace the film"
        assert tracker.get_tracked_media(42).title == "The Film"
        assert tracker.get_tracked_media(42, 1).title == "The Show"


def test_a_radarr_grab_never_lands_on_a_show():
    from webhooks.radarr_handler import _handle_grab
    with _Tracker() as tracker:
        show = _request_all_seasons(tracker, tmdb_id=42, title="The Show")
        asyncio.run(_handle_grab({"movie": {"title": "The Film", "tmdbId": 42}, "downloadId": "abc"}))
        assert show.download_id is None and show.download_status == "pending"


def test_shows_saved_under_the_old_movie_key_move_at_start_up():
    show = media_tracking.TrackedMedia(tmdb_id=7, media_type="tv", title="Severance", requester_user_id=5,
                                       requested_seasons="all").to_dict()
    film = media_tracking.TrackedMedia(tmdb_id=8, media_type="movie", title="Arrival", requester_user_id=6).to_dict()
    season = media_tracking.TrackedMedia(tmdb_id=9, media_type="tv", title="Andor", season_number=1,
                                         requester_user_id=7).to_dict()
    saved = {"movie:7": show, "movie:8": film, "tv:9:s1": season}

    with _Tracker(saved) as tracker:
        assert set(tracker.tracked_media) == {"tv:7", "movie:8", "tv:9:s1"}
        assert tracker.tracked_media["tv:7"].to_dict() == show, "only the key changes"
        on_disk = json.loads(media_tracking.TRACKING_FILE.read_text())
        assert on_disk == {"tv:7": show, "movie:8": film, "tv:9:s1": season}, "and the file is rewritten once"
        assert tracker.get_tracked_media(7, 3).title == "Severance"
