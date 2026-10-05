# path: tests/test_season_search.py
"""Season search with the episode-by-episode fallback (core/season_search)."""
import asyncio

import conftest  # noqa: F401

from core import season_search

season_search.POLL_SECONDS = 0
season_search.SETTLE_SECONDS = 0


class FakeSonarr:
    """Season 2 has episodes 21-25 missing. `finds` decides what a search grabs."""

    def __init__(self, finds):
        self.finds = finds
        self.commands = []
        self.queued = set()

    async def episodes(self, series_id):
        return [{"id": 20 + n, "seasonNumber": 2, "episodeNumber": n, "hasFile": False,
                 "airDateUtc": "2000-01-01T00:00:00Z"} for n in range(1, 6)] + \
               [{"id": 10, "seasonNumber": 1, "episodeNumber": 1, "hasFile": True, "airDateUtc": "1999-01-01T00:00:00Z"}]

    async def command(self, name, **body):
        self.commands.append((name, body))
        self.queued |= set(self.finds(name, body))
        return {"id": len(self.commands)}

    async def get(self, path, **params):
        return {"status": "completed"}

    async def queue(self, **params):
        return [{"seriesId": 7, "episodeId": e} for e in self.queued]


def _run(finds):
    sonarr = FakeSonarr(finds)
    outcome = asyncio.run(season_search.search_season(sonarr, 7, 2))
    return outcome, sonarr.commands


def test_a_season_release_is_used_when_there_is_one():
    outcome, commands = _run(lambda name, body: [21, 22, 23, 24, 25] if name == "SeasonSearch" else [])
    assert outcome == "season" and [c[0] for c in commands] == ["SeasonSearch"]


def test_no_season_release_falls_back_to_single_episodes():
    outcome, commands = _run(lambda name, body: body.get("episodeIds", []) if name == "EpisodeSearch" else [])
    assert outcome == "episodes"
    assert commands == [("SeasonSearch", {"seriesId": 7, "seasonNumber": 2}),
                        ("EpisodeSearch", {"episodeIds": [21, 22]}),          # the first two, as a probe
                        ("EpisodeSearch", {"episodeIds": [23, 24, 25]})]      # then the rest


def test_nothing_anywhere_is_reported():
    outcome, commands = _run(lambda name, body: [])
    assert outcome == "nothing"
    assert [c[0] for c in commands] == ["SeasonSearch", "EpisodeSearch"], "no point searching the rest"


def test_episode_by_episode_skips_the_season_search():
    sonarr = FakeSonarr(lambda name, body: body.get("episodeIds", []))
    outcome = asyncio.run(season_search.search_season(sonarr, 7, 2, season_first=False))
    assert outcome == "episodes" and sonarr.commands[0][0] == "EpisodeSearch"


def test_a_complete_season_is_left_alone():
    sonarr = FakeSonarr(lambda name, body: [])
    outcome = asyncio.run(season_search.search_season(sonarr, 7, 1))
    assert outcome == "complete" and sonarr.commands == []


def test_seasons_nobody_has_are_handed_to_on_nothing():
    class Services:
        sonarr = FakeSonarr(lambda name, body: [])
    told = []

    async def nothing(seasons):
        told.append(seasons)

    async def go():
        season_search.start(Services(), 7, [2], on_nothing=nothing)
        await asyncio.sleep(0.2)
    asyncio.run(go())
    assert told == [[2]]


def test_cant_be_found_is_a_help_reason():
    from portal import help as helpdesk
    assert helpdesk.REASONS["notfound"] == "Can't be found"


class Later(FakeSonarr):
    """A new show: episodes airing at `airs`, Sonarr adds it a moment after approval."""

    def __init__(self, finds, airs, appear_after=1):
        super().__init__(finds)
        self.airs, self.polls, self.appear_after = airs, 0, appear_after

    async def series(self):
        self.polls += 1
        return [{"id": 7, "tmdbId": 456}] if self.polls > self.appear_after else []

    async def episodes(self, series_id):
        return [{"id": 20 + n, "seasonNumber": 1, "episodeNumber": n, "hasFile": False, "airDateUtc": self.airs}
                for n in range(1, 4)]


def _follow(sonarr, retries=0):
    from datetime import datetime, timedelta, timezone   # noqa: F401
    told = []

    class Services:
        pass
    Services.sonarr = sonarr

    async def nothing(seasons):
        told.append(seasons)

    async def go():
        season_search.follow_up_new_show(Services(), tmdb_id=456, seasons=[1], title="Coven Academy",
                                         on_nothing=nothing, wait_for_sonarr=5, retries=retries)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not season_search._tasks:
                break
    asyncio.run(go())
    return told


def test_a_streaming_drop_listed_as_airing_later_today_is_still_searched():
    """Coven Academy: out on Disney+, Sonarr's air dates hours ahead, so its own search skipped it."""
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat()
    sonarr = Later(lambda name, body: [21, 22, 23] if name == "SeasonSearch" else [], soon)
    told = _follow(sonarr)
    assert ("SeasonSearch", {"seriesId": 7, "seasonNumber": 1}) in sonarr.commands
    assert told == [], "found: no help request"


def test_a_show_that_really_hasnt_aired_is_not_reported_missing():
    from datetime import datetime, timedelta, timezone
    later = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    sonarr = Later(lambda name, body: [], later)
    told = _follow(sonarr, retries=0)
    assert sonarr.commands == [], "nothing to search before it airs"
    assert told == [], "not out yet isn't 'can't be found'"
