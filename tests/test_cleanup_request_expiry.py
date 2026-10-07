# path: tests/test_cleanup_request_expiry.py
"""Media cleanup: request expiry, which turns Sonarr/Radarr monitoring off 90 days
after a title's newest request.

Practice mode still switched monitoring off, though it promises to change nothing. A
title kept permanently, or an airing show the household watches every week, stopped
getting new episodes and upgrades all the same. And every day it forced the requested
seasons back on, undoing an admin who had switched a series off by hand."""
import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401

import plugins.media_cleanup.cog as cleanup
from plugins.media_cleanup.cog import MediaCleanupCog

SHOW_TMDB, SHOW_TVDB, FILM_TMDB = 76479, 355567, 603


class Sonarr:
    def __init__(self, monitored=True):
        self.rows = [{"id": 68, "title": "The Boys", "tmdbId": SHOW_TMDB, "tvdbId": SHOW_TVDB, "monitored": monitored}]
        self.calls = []

    async def series(self):
        return [dict(row) for row in self.rows]

    async def set_series_monitored(self, series_id, monitored):
        self.calls.append(("series", series_id, monitored))
        for row in self.rows:
            if row["id"] == series_id:
                row["monitored"] = monitored

    async def set_episodes_monitored(self, series_id, seasons, monitored, existing_episodes=None):
        self.calls.append(("episodes", series_id, seasons, monitored))


class Radarr:
    def __init__(self, monitored=True):
        self.rows = [{"id": 12, "title": "The Matrix", "tmdbId": FILM_TMDB, "imdbId": "tt0133093", "monitored": monitored}]
        self.calls = []

    async def movies(self):
        return [dict(row) for row in self.rows]

    async def set_movie_monitored(self, movie_id, monitored):
        self.calls.append(("movie", movie_id, monitored))
        for row in self.rows:
            if row["id"] == movie_id:
                row["monitored"] = monitored


class Plex:
    """Two libraries, one film and one show, and who watched what (days ago)."""

    def __init__(self, played=None, fails=False, show_guids=None):
        self.played, self.fails = played or {}, fails
        self.show_guids = show_guids or [f"tvdb://{SHOW_TVDB}", f"tmdb://{SHOW_TMDB}"]

    def history(self, mindate=None):
        if self.fails:
            raise ConnectionError("Plex is down")

        class H:
            def __init__(self, key, days):
                self.ratingKey, self.grandparentRatingKey = f"{key}-ep", key
                self.viewedAt = datetime.now() - timedelta(days=days)
        return [H(key, days) for key, days in self.played.items() if mindate is None or days < (datetime.now() - mindate).days]

    def query(self, path):
        if self.fails:
            raise ConnectionError("Plex is down")
        if path == "/library/sections":
            return ET.fromstring('<MediaContainer><Directory key="1" type="movie" title="Films"/>'
                                 '<Directory key="2" type="show" title="TV"/></MediaContainer>')
        if path.startswith("/library/sections/1/"):
            return ET.fromstring(f'<MediaContainer><Video ratingKey="500" title="The Matrix">'
                                 f'<Guid id="imdb://tt0133093"/><Guid id="tmdb://{FILM_TMDB}"/></Video></MediaContainer>')
        guids = "".join(f'<Guid id="{guid}"/>' for guid in self.show_guids)
        return ET.fromstring(f'<MediaContainer><Directory ratingKey="76" title="The Boys">{guids}</Directory></MediaContainer>')


def _request(tmdb, days, kind, aware=True, seasons=None):
    when = datetime.now(timezone.utc) - timedelta(days=days)
    if not aware:
        when = when.replace(tzinfo=None)
    media = {"id": tmdb, "media_type": kind, **({"title": "The Matrix"} if kind == "movie" else {"name": "The Boys"})}
    return {"media": media, "timestamp": when.isoformat(), "seasons": seasons}


def _cog(plex=None, dry_run=False, exempt=None, sonarr=None, radarr=None, exclude=None):
    cog = object.__new__(MediaCleanupCog)
    cog.config = {"enabled": True, "inactivity_days": 90, "notify_days_before": 7, "exempt_items": exempt or {},
                  "dry_run": dry_run, "exclude_libraries": exclude or []}
    cog._prune_media_tracking_cache = lambda latest, cutoff: 0

    class Config:
        sonarr_url, sonarr_token, radarr_url, radarr_token = "http://sonarr", "s", "http://radarr", "r"

    class Services:
        config = Config()
        plex_server = Plex() if plex is None else plex
    cog.services = Services()
    cog.services.sonarr = sonarr or Sonarr()
    cog.services.radarr = radarr or Radarr()
    return cog


def _expire(cog, requests, store=None, broken=()):
    """Run request expiry once against `requests`, with `store` as the database;
    `broken` names the database calls ("get", "set") that fail."""
    store = {} if store is None else store

    async def all_requests():
        return {str(i): r for i, r in enumerate(requests)}

    async def kv_get(namespace, key, default=None):
        if "get" in broken:
            raise OSError("database is locked")
        return store.get((namespace, key), default)

    async def kv_set(namespace, key, value):
        if "set" in broken:
            raise OSError("database is locked")
        store[(namespace, key)] = value

    saved = cleanup.all_requests, cleanup.kv_get, cleanup.kv_set
    cleanup.all_requests, cleanup.kv_get, cleanup.kv_set = all_requests, kv_get, kv_set
    try:
        return asyncio.run(cog.enforce_request_monitor_cleanup())
    finally:
        cleanup.all_requests, cleanup.kv_get, cleanup.kv_set = saved


def _off(arr):
    return [call for call in arr.calls if call[-1] is False]


def _record(store):
    return store.get((cleanup.CLEANUP_NAMESPACE, "expired_monitoring"))


def test_a_request_older_than_90_days_turns_monitoring_off():
    cog = _cog()
    summary = _expire(cog, [_request(SHOW_TMDB, 100, "tv"), _request(FILM_TMDB, 100, "movie")])
    assert ("series", 68, False) in cog.services.sonarr.calls
    assert ("episodes", 68, None, False) in cog.services.sonarr.calls
    assert cog.services.radarr.calls == [("movie", 12, False)]
    assert summary["tv_unmonitored"] == 1 and summary["movie_unmonitored"] == 1


def test_a_new_request_turns_back_on_what_expiry_turned_off():
    store = {}
    sonarr, radarr = Sonarr(), Radarr()
    _expire(_cog(sonarr=sonarr, radarr=radarr), [_request(SHOW_TMDB, 100, "tv"), _request(FILM_TMDB, 100, "movie")], store)
    assert not sonarr.rows[0]["monitored"] and not radarr.rows[0]["monitored"]

    again = [_request(SHOW_TMDB, 100, "tv"), _request(SHOW_TMDB, 10, "tv", seasons=[2]), _request(FILM_TMDB, 10, "movie")]
    summary = _expire(_cog(sonarr=sonarr, radarr=radarr), again, store)
    assert sonarr.rows[0]["monitored"] and radarr.rows[0]["monitored"]
    assert ("episodes", 68, [2], True) in sonarr.calls, "the requested season is monitored again"
    assert summary["tv_reenabled"] == 1 and summary["movie_reenabled"] == 1

    # Back on, and left alone after that: no forcing it on again every day.
    sonarr.calls.clear()
    _expire(_cog(sonarr=sonarr, radarr=radarr), again, store)
    assert sonarr.calls == []


def test_a_series_switched_off_by_hand_stays_off():
    sonarr, radarr = Sonarr(monitored=False), Radarr(monitored=False)
    summary = _expire(_cog(sonarr=sonarr, radarr=radarr),
                      [_request(SHOW_TMDB, 10, "tv", seasons=[1]), _request(FILM_TMDB, 10, "movie")])
    assert sonarr.calls == [] and radarr.calls == [], "the admin's own change isn't undone"
    assert summary["tv_reenabled"] == 0 and summary["movie_reenabled"] == 0


def test_practice_mode_changes_nothing_in_sonarr_or_radarr():
    cog = _cog(dry_run=True)
    store = {}
    summary = _expire(cog, [_request(SHOW_TMDB, 100, "tv"), _request(FILM_TMDB, 100, "movie")], store)
    assert cog.services.sonarr.calls == [] and cog.services.radarr.calls == []
    assert summary["tv_unmonitored"] == 1 and summary["movie_unmonitored"] == 1, "it reports what it would do"
    assert not any(value for value in store.values()), "nothing is remembered as switched off"


def test_kept_titles_and_titles_watched_lately_stay_monitored():
    exempt = {"500": {"title": "The Matrix", "type": "movie"}}
    cog = _cog(plex=Plex(played={"76": 6}), exempt=exempt)       # a member watched an episode 6 days ago
    summary = _expire(cog, [_request(SHOW_TMDB, 100, "tv"), _request(FILM_TMDB, 100, "movie")])
    assert _off(cog.services.sonarr) == [] and _off(cog.services.radarr) == []
    assert summary["tv_unmonitored"] == 0 and summary["movie_unmonitored"] == 0

    long_ago = _cog(plex=Plex(played={"76": 120}))
    _expire(long_ago, [_request(SHOW_TMDB, 100, "tv")])
    assert ("series", 68, False) in long_ago.services.sonarr.calls, "a play before the request window doesn't count"


def test_nothing_is_switched_off_while_plex_cant_be_read():
    cog = _cog(plex=Plex(fails=True))
    _expire(cog, [_request(SHOW_TMDB, 100, "tv"), _request(FILM_TMDB, 100, "movie")])
    assert _off(cog.services.sonarr) == [] and _off(cog.services.radarr) == []


def test_requests_with_and_without_a_time_zone_are_compared():
    cog = _cog()
    summary = _expire(cog, [_request(SHOW_TMDB, 100, "tv", aware=True), _request(SHOW_TMDB, 10, "tv", aware=False),
                            _request(FILM_TMDB, 10, "movie", aware=True), _request(FILM_TMDB, 100, "movie", aware=False)])
    assert _off(cog.services.sonarr) == [] and _off(cog.services.radarr) == [], "the newest request, naive or not, wins"
    assert summary["tv_unmonitored"] == 0 and summary["movie_unmonitored"] == 0

    stale = _cog()
    _expire(stale, [_request(SHOW_TMDB, 120, "tv", aware=True), _request(SHOW_TMDB, 100, "tv", aware=False)])
    assert ("series", 68, False) in stale.services.sonarr.calls


def test_after_an_upgrade_titles_already_switched_off_are_taken_as_its_own():
    # Before the record existed, every title with an expired request was switched off
    # and kept off - kept titles and shows watched every week included.
    exempt = {"500": {"title": "The Matrix", "type": "movie"}}
    sonarr, radarr = Sonarr(monitored=False), Radarr(monitored=False)
    summary = _expire(_cog(plex=Plex(played={"76": 6}), exempt=exempt, sonarr=sonarr, radarr=radarr),
                      [_request(SHOW_TMDB, 100, "tv"), _request(FILM_TMDB, 100, "movie")])
    assert sonarr.rows[0]["monitored"] and radarr.rows[0]["monitored"]
    assert summary["tv_reenabled"] == 1 and summary["movie_reenabled"] == 1

    # One nobody watches stays off, but on record, so a later play turns it back on.
    store, sonarr = {}, Sonarr(monitored=False)
    _expire(_cog(sonarr=sonarr), [_request(SHOW_TMDB, 100, "tv")], store)
    assert sonarr.calls == [] and _record(store)["tv"] == [68]
    _expire(_cog(plex=Plex(played={"76": 2}), sonarr=sonarr), [_request(SHOW_TMDB, 100, "tv")], store)
    assert sonarr.rows[0]["monitored"] and _record(store)["tv"] == []


def test_a_title_is_on_record_before_it_is_switched_off():
    class EpisodesTimeOut(Sonarr):
        async def set_episodes_monitored(self, series_id, seasons, monitored, existing_episodes=None):
            raise TimeoutError("Sonarr took too long")

    store, sonarr = {}, EpisodesTimeOut()
    old = [_request(SHOW_TMDB, 100, "tv"), _request(FILM_TMDB, 100, "movie")]
    summary = _expire(_cog(sonarr=sonarr), old, store)
    assert not sonarr.rows[0]["monitored"] and _record(store)["tv"] == [68], "half switched off, still on record"
    assert summary["movie_unmonitored"] == 1, "one title failing doesn't stop the rest"

    sonarr = Sonarr(monitored=False)
    _expire(_cog(sonarr=sonarr), old + [_request(SHOW_TMDB, 5, "tv")], store)
    assert sonarr.rows[0]["monitored"], "so a new request still turns it back on"

    # And if the record can't be saved, nothing is switched off at all.
    cog = _cog()
    _expire(cog, old, broken=("set",))
    assert _off(cog.services.sonarr) == [] and _off(cog.services.radarr) == []


def test_the_tracking_prune_still_runs_when_the_record_cant_be_read():
    cog = _cog()
    cog._prune_media_tracking_cache = lambda latest, cutoff: 3
    summary = _expire(cog, [_request(SHOW_TMDB, 100, "tv")], broken=("get",))
    assert cog.services.sonarr.calls == [], "Sonarr is left alone that day"
    assert summary["media_tracking_pruned"] == 3


def test_skipped_libraries_and_guids_with_a_suffix_are_kept():
    # Plex knows the show by tvdb alone, with a language suffix; the film's library is skipped by cleanup.
    cog = _cog(plex=Plex(played={"76": 6}, show_guids=[f"tvdb://{SHOW_TVDB}?lang=en"]), exclude=["Films"])
    _expire(cog, [_request(SHOW_TMDB, 100, "tv"), _request(FILM_TMDB, 100, "movie")])
    assert _off(cog.services.sonarr) == [] and _off(cog.services.radarr) == []


def test_deleted_titles_leave_the_record():
    # Sonarr and Radarr can give a new title a deleted one's id; it mustn't inherit "switched off by Plexbie".
    store = {(cleanup.CLEANUP_NAMESPACE, "expired_monitoring"): {"tv": [68, 99], "movie": [12, 40]}}
    _expire(_cog(), [], store)
    assert _record(store) == {"tv": [68], "movie": [12]}
