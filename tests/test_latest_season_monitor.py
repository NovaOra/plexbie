# path: tests/test_latest_season_monitor.py
"""The "Latest season + new episodes" choice: the newest season now, and every later one as it comes out.

The choice only ever requested the newest season. Nothing told Sonarr to fetch
what came after it, though the member was promised new episodes and the admin
card said "Monitor enabled", and request expiry later switched the show off
altogether. Now the show stays monitored in Sonarr with new seasons monitored as
they're added (the older seasons stay as they were), straight away for a show
Sonarr already has and as soon as Seerr has added a new one; and request expiry
leaves such a show alone, and sets it up once if approval couldn't. Only an
approved request counts. Without Sonarr set up, and for the plain season
choices, nothing changes in Sonarr.
"""
import asyncio
import copy
from types import SimpleNamespace

import conftest  # noqa: F401

from core import season_search
from core.clients import Arr

TMDB = 456


class FakeSonarr(Arr):
    """Sonarr's API in memory. A PUT of a series replaces it with what was sent, as
    Sonarr does, so a partial object would show up as fields gone."""

    configured = True

    def __init__(self, has_show=True, appear_after=0):
        super().__init__(SimpleNamespace(config=SimpleNamespace()), "sonarr")
        self.show = {"id": 7, "title": "The Show", "tmdbId": TMDB, "tvdbId": 999, "monitored": False,
                     "monitorNewItems": "none", "qualityProfileId": 4, "path": "/tv/The Show", "tags": [2],
                     "seasons": [{"seasonNumber": n, "monitored": n == 3} for n in (1, 2, 3)]}
        self.has_show, self.appear_after, self.polls = has_show, appear_after, 0
        self.calls = []

    def _episodes(self):
        return [{"id": 100 + n, "seasonNumber": n, "episodeNumber": 1, "hasFile": True, "monitored": n == 3,
                 "airDateUtc": "2020-01-01T00:00:00Z"} for n in (1, 2, 3)]

    async def _api(self, method, path, **kw):
        self.calls.append((method, path))
        if method == "GET" and path == "series":
            self.polls += 1
            return [copy.deepcopy(self.show)] if self.has_show or self.polls > self.appear_after else []
        if method == "GET" and path == f"series/{self.show['id']}":
            return copy.deepcopy(self.show)
        if method == "PUT" and path == f"series/{self.show['id']}":
            lost = set(self.show) - set(kw["json"])
            assert not lost, f"a partial series was sent; Sonarr would reset {sorted(lost)}"
            self.show = copy.deepcopy(kw["json"])
            return self.show
        if method == "PUT" and path == "series/editor":
            self.show["monitored"] = kw["json"]["monitored"]
            return []
        if method == "GET" and path == "episode":
            return self._episodes()
        if method == "PUT" and path == "episode/monitor":
            return []
        raise AssertionError(f"unexpected Sonarr call {method} {path}")

    def season_flags(self):
        return {s["seasonNumber"]: s["monitored"] for s in self.show["seasons"]}

    def series_puts(self):
        return [c for c in self.calls if c == ("PUT", f"series/{self.show['id']}")]


def _view(sonarr, monitor=True, seasons=(3,)):
    from plugins.media_requests import cog
    view = cog.AdminApprovalView({"id": TMDB, "media_type": "tv", "name": "The Show"}, 1,
                                 SimpleNamespace(sonarr=sonarr), seasons=list(seasons), monitor=monitor)

    async def submitted():
        return True
    view._submit_to_seerr = submitted
    return view


def _approve(view, follow_up=None):
    """Approve the request; `follow_up` stands in for the new-show follow-up when given."""
    saved = season_search.follow_up_new_show, season_search.start, season_search.POLL_SECONDS
    season_search.start = lambda *a, **kw: None
    season_search.POLL_SECONDS = 0.01
    if follow_up is not None:
        season_search.follow_up_new_show = follow_up

    async def go():
        result = await view._fulfill_request()
        for _ in range(200):
            if not season_search._tasks:
                break
            await asyncio.sleep(0.01)
        return result
    try:
        return asyncio.run(go())
    finally:
        season_search.follow_up_new_show, season_search.start, season_search.POLL_SECONDS = saved


def test_a_show_already_in_sonarr_follows_new_seasons_and_leaves_older_ones_alone():
    sonarr = FakeSonarr()
    result = _approve(_view(sonarr))
    assert result["success"]
    assert sonarr.show["monitored"] is True
    assert sonarr.show["monitorNewItems"] == "all", "seasons Sonarr adds later are monitored"
    assert sonarr.season_flags() == {1: False, 2: False, 3: True}, "older seasons stay unmonitored"
    assert sonarr.show["qualityProfileId"] == 4 and sonarr.show["tags"] == [2]


def test_a_new_show_follows_new_seasons_once_seerr_has_added_it():
    sonarr = FakeSonarr(has_show=False, appear_after=2)
    result = _approve(_view(sonarr))
    assert result["success"]
    assert sonarr.polls > 2, "waited for Seerr to add it"
    assert sonarr.show["monitored"] is True and sonarr.show["monitorNewItems"] == "all"
    assert sonarr.season_flags() == {1: False, 2: False, 3: True}
    assert len(sonarr.series_puts()) == 1, "set once"


def test_without_sonarr_nothing_is_sent_to_it():
    sonarr = FakeSonarr(has_show=False)
    sonarr.configured = False
    handed = {}

    def follow_up(services, **kw):
        handed.update(kw)
    result = _approve(_view(sonarr), follow_up)
    assert result["success"], "the season is still requested through Seerr"
    assert sonarr.calls == []
    assert handed.get("on_found") is None


def test_the_plain_season_choices_change_nothing_new_in_sonarr():
    sonarr = FakeSonarr()
    _approve(_view(sonarr, monitor=False))
    assert sonarr.show["monitorNewItems"] == "none"

    handed = {}

    def follow_up(services, **kw):
        handed.update(kw)
    _approve(_view(FakeSonarr(has_show=False, appear_after=99), monitor=False), follow_up)
    assert handed and handed.get("on_found") is None


def test_the_monitor_choice_sends_seerr_no_made_up_fields():
    from plugins.media_requests import cog
    sent = []

    class Seerr:
        configured = True

        async def post(self, path, body, raw=False):
            sent.append(body)
            return 201, "{}"

    async def has_tvdb(services, tmdb_id):
        return False
    view = cog.AdminApprovalView({"id": TMDB, "media_type": "tv", "name": "The Show"}, 1,
                                 SimpleNamespace(seerr=Seerr()), seasons=[3], monitor=True)
    saved = cog.no_tvdb_entry
    cog.no_tvdb_entry = has_tvdb
    try:
        assert asyncio.run(view._submit_to_seerr())
    finally:
        cog.no_tvdb_entry = saved
    assert sent == [{"mediaType": "tv", "mediaId": TMDB, "seasons": [3]}]


# ------------------------------------------------------------ request expiry

def _followed(days, status="approved"):
    from test_cleanup_request_expiry import SHOW_TMDB, _request
    return {**_request(SHOW_TMDB, days, "tv", seasons=[3]), "monitor": True, "status": status}


def test_request_expiry_leaves_a_show_followed_for_new_seasons_alone():
    from test_cleanup_request_expiry import SHOW_TMDB, _cog, _expire, _off, _request
    followed = _followed

    cog = _cog()
    summary = _expire(cog, [followed(100)])
    assert _off(cog.services.sonarr) == [] and summary["tv_unmonitored"] == 0

    later_plain = _cog()
    _expire(later_plain, [followed(200), _request(SHOW_TMDB, 100, "tv", seasons=[1])])
    assert _off(later_plain.services.sonarr) == [], "a later request for one season doesn't undo it"

    declined = _cog()
    _expire(declined, [followed(100, status="declined")])
    assert ("series", 68, False) in declined.services.sonarr.calls, "a declined request promised nothing"

    pending = _cog()
    _expire(pending, [followed(100, status="pending")])
    assert ("series", 68, False) in pending.services.sonarr.calls, "nor did one nobody approved"


class ExpirySonarr:
    """The listing request expiry reads, as Sonarr v4 gives it, with the full series behind it."""

    def __init__(self, new_items="none", monitored=True):
        from test_cleanup_request_expiry import SHOW_TMDB
        self.inner = FakeSonarr()
        self.inner.show.update(tmdbId=SHOW_TMDB, monitored=monitored, monitorNewItems=new_items)
        self.calls = []

    async def series(self):
        return await self.inner.series()

    async def follow_new_seasons(self, series_id):
        self.calls.append(("follow", series_id))
        return await self.inner.follow_new_seasons(series_id)

    async def set_series_monitored(self, series_id, monitored):
        self.calls.append(("series", series_id, monitored))
        self.inner.show["monitored"] = monitored

    async def set_episodes_monitored(self, series_id, seasons, monitored, existing_episodes=None):
        self.calls.append(("episodes", series_id, seasons, monitored))


def test_request_expiry_sets_up_new_seasons_once_when_approval_could_not():
    """A show Seerr added after the wait, a Sonarr call that failed, or a restart
    while waiting: the daily check sets it up, once, and leaves the seasons alone."""
    from test_cleanup_request_expiry import _cog, _expire

    sonarr, store = ExpirySonarr(), {}
    cog = _cog(sonarr=sonarr)
    summary = _expire(cog, [_followed(10)], store)
    assert sonarr.inner.show["monitorNewItems"] == "all" and sonarr.inner.show["monitored"] is True
    assert sonarr.inner.season_flags() == {1: False, 2: False, 3: True}, "older seasons stay unmonitored"
    assert summary["tv_following_new_seasons"] == 1

    sonarr.inner.show["monitorNewItems"] = "none"       # an admin changes it by hand later
    summary = _expire(cog, [_followed(10)], store)
    assert sonarr.inner.show["monitorNewItems"] == "none" and summary["tv_following_new_seasons"] == 0

    already = ExpirySonarr(new_items="all")
    store = {}
    _expire(_cog(sonarr=already), [_followed(10)], store)
    assert ("follow", 7) not in already.calls, "nothing to do when Sonarr already follows it"
    already.inner.show["monitorNewItems"] = "none"
    _expire(_cog(sonarr=already), [_followed(10)], store)
    assert already.inner.show["monitorNewItems"] == "none", "seen set once, so a later change by hand stands"


def test_request_expiry_sets_up_new_seasons_only_where_it_should():
    from test_cleanup_request_expiry import _cog, _expire

    practice = ExpirySonarr()
    _expire(_cog(sonarr=practice, dry_run=True), [_followed(10)])
    assert ("follow", 7) not in practice.calls and practice.inner.show["monitorNewItems"] == "none"

    by_hand = ExpirySonarr(monitored=False)
    _expire(_cog(sonarr=by_hand), [_followed(10)], {})
    assert ("follow", 7) not in by_hand.calls, "a series switched off by hand stays off"

    for status in ("pending", "declined"):
        undecided = ExpirySonarr()
        _expire(_cog(sonarr=undecided), [_followed(10, status=status)], {})
        assert ("follow", 7) not in undecided.calls

    plain = ExpirySonarr()
    _expire(_cog(sonarr=plain), [{**_followed(10), "monitor": False}], {})
    assert ("follow", 7) not in plain.calls

    v3 = ExpirySonarr()
    del v3.inner.show["monitorNewItems"]
    _expire(_cog(sonarr=v3), [_followed(10)], {})
    assert ("follow", 7) not in v3.calls, "Sonarr v3 has no such setting"


def test_request_expiry_turns_back_on_and_sets_up_a_show_it_had_turned_off():
    from test_cleanup_request_expiry import _cog, _expire, _record

    from plugins.media_cleanup import cog as cleanup
    sonarr = ExpirySonarr(monitored=False)
    store = {(cleanup.CLEANUP_NAMESPACE, "expired_monitoring"): {"tv": [7], "movie": []}}
    _expire(_cog(sonarr=sonarr), [_followed(100)], store)
    assert sonarr.inner.show["monitored"] is True and sonarr.inner.show["monitorNewItems"] == "all"
    assert _record(store) == {"tv": [], "movie": [], "following": [7]}


def test_a_failed_sonarr_call_is_tried_again_next_day():
    from test_cleanup_request_expiry import _cog, _expire

    sonarr, store = ExpirySonarr(), {}
    real = sonarr.follow_new_seasons

    async def busy(series_id):
        raise ConnectionError("Sonarr is busy")
    sonarr.follow_new_seasons = busy
    _expire(_cog(sonarr=sonarr), [_followed(10)], store)
    assert sonarr.inner.show["monitorNewItems"] == "none"
    sonarr.follow_new_seasons = real
    _expire(_cog(sonarr=sonarr), [_followed(10)], store)
    assert sonarr.inner.show["monitorNewItems"] == "all"
