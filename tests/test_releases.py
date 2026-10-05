# path: tests/test_releases.py
"""The "Upcoming" stage: when a requested title comes out (core/releases)."""
import asyncio
from datetime import datetime, timezone

import conftest  # noqa: F401

from core import releases

NOW = datetime(2026, 10, 4, 15, 0, tzinfo=timezone.utc)
VERITY = {"title": "Verity", "status": "inCinemas", "inCinemas": "2026-09-30T00:00:00Z", "digitalRelease": "2026-10-27T00:00:00Z"}
PRIMETIME = {"title": "Primetime", "status": "inCinemas", "inCinemas": "2026-09-23T00:00:00Z"}
DOOMSDAY = {"title": "Avengers: Doomsday", "status": "announced", "inCinemas": "2026-12-16T00:00:00Z"}


def test_a_film_with_a_streaming_date_says_when():
    up = releases.movie_upcoming(VERITY, NOW)
    assert up["stage"] == "upcoming" and up["releaseDate"] == "2026-10-27" and up["releaseKind"] == "digital"
    assert up["detail"] == "Out to stream Oct 27. Plexbie gets it then. In cinemas since Sep 30."


def test_a_film_in_cinemas_without_a_streaming_date_says_when_plexbie_starts_looking():
    up = releases.movie_upcoming(PRIMETIME, NOW)
    assert up["releaseKind"] == "cinemas" and up["expected"] == "2026-12-22"
    assert up["detail"].startswith("In cinemas since Sep 23. Its streaming date isn't announced yet; Plexbie starts looking around Dec 22")
    up = releases.movie_upcoming(DOOMSDAY, NOW)
    assert up["detail"].startswith("In cinemas Dec 16.") and "Mar 16, 2027" in up["detail"]


def test_a_film_thats_out_is_searchable():
    assert releases.movie_upcoming({**VERITY, "digitalRelease": "2026-10-01T00:00:00Z"}, NOW) is None
    assert releases.movie_upcoming({**PRIMETIME, "inCinemas": "2026-05-01T00:00:00Z"}, NOW) is None, "90 days after cinemas"
    assert releases.movie_upcoming({"title": "Over the Hedge", "status": "released"}, NOW) is None, "old films lack dates"
    assert releases.movie_upcoming({"title": "X", "status": "announced"}, NOW)["releaseKind"] == "unannounced"
    disc_first = releases.movie_upcoming({**VERITY, "physicalRelease": "2026-10-20T00:00:00Z"}, NOW)
    assert disc_first["releaseKind"] == "disc" and disc_first["detail"].startswith("Out on disc Oct 20.")


def _season(n, aired, next_airing=None):
    stats = {"episodeCount": aired, "episodeFileCount": 0}
    if next_airing:
        stats["nextAiring"] = next_airing
    return {"seasonNumber": n, "statistics": stats}


def test_a_season_that_hasnt_started_says_when_it_does():
    up = releases.show_upcoming([_season(3, 0, "2026-10-15T01:00:00Z")], NOW)
    assert up["releaseDate"] == "2026-10-15" and up["detail"] == "Season 3 starts Oct 15. Plexbie gets each episode as it airs."
    assert releases.show_upcoming([_season(3, 0)], NOW)["releaseKind"] == "unannounced"
    assert releases.show_upcoming([_season(2, 4), _season(3, 0, "2026-10-15T01:00:00Z")], NOW) is None, "season 2 has aired"
    assert releases.show_upcoming([{"seasonNumber": 1}], NOW) is None, "no statistics: not claimed upcoming"
    assert releases.next_episode([_season(29, 3, "2026-10-07T23:00:00Z")], NOW) == "next episode Oct 7"


def test_progress_shows_an_unreleased_film_as_upcoming_not_searching():
    from portal.cache import TTLCache
    from portal.progress import Progress

    class Svc:
        plex_server = None
        radarr = sonarr = sab = type("C", (), {"configured": False})()
    p = Progress(Svc(), TTLCache())

    async def movies():
        return {123: {**PRIMETIME, "id": 9, "hasFile": False, "inCinemas": "2099-01-01T00:00:00Z"},
                124: {"id": 10, "title": "Old", "status": "released", "hasFile": False}}

    async def empty():
        return {}

    async def no_slots():
        return []
    p._radarr_movies, p._radarr_queue, p._sab_slots = movies, empty, no_slots
    up = asyncio.run(p.video({"media_type": "movie", "id": 123}, None))
    old = asyncio.run(p.video({"media_type": "movie", "id": 124}, None))
    assert up["stage"] == "upcoming" and up["releaseKind"] == "cinemas"
    assert old["stage"] == "searching"
