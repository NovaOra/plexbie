# path: tests/test_plex_match.py
"""Fixing a film Plex matched to the wrong title (core/plex_match). Obsession
(2026): Curry Barker's 109-minute film, matched by Plex to a 2-minute short."""
import asyncio

import conftest  # noqa: F401

from core import plex_match

plex_match.SETTLE_SECONDS = 0
FILM = {"title": "Obsession", "year": 2026, "tmdbId": 1339713, "runtime": 109, "alternateTitles": [],
        "movieFile": {"sceneName": "Obsession.2026.2160p.UHD.BluRay.Remux.DV.P7.HDR.MULTi-Ben.The.Men"}}


class Guid:
    def __init__(self, id):
        self.id = id


class Item:
    def __init__(self, server, minutes):
        self.server, self.title, self.duration = server, "Obsession", minutes * 60000
        self.guids = [Guid(g) for g in server.guids]

    def matches(self, title=None):
        self.server.asked.append(title)
        return ["plex://movie/6856893830a4aaafd5c4291d"] if title == "tmdb-1339713" else []

    def fixMatch(self, searchResult=None):
        self.server.fixed.append(searchResult)
        self.server.guids = ["imdb://tt37287335", "tmdb://1339713"]


class Server:
    def __init__(self, minutes):
        self.minutes, self.guids, self.asked, self.fixed = minutes, ["imdb://tt39365308"], [], []

    def fetchItem(self, key):
        assert key == 11509
        return Item(self, self.minutes)


class Radarr:
    def __init__(self, titles):
        self.titles = titles

    async def get(self, path, **params):
        return {"parsedMovieInfo": {"movieTitles": self.titles}}


def _services(minutes, titles=("Obsession",)):
    return type("S", (), {"plex_server": Server(minutes), "radarr": Radarr(list(titles))})()


def test_a_file_the_length_of_the_requested_film_is_matched_to_it():
    services = _services(109)
    text = asyncio.run(plex_match.fix_film_match(services, FILM, "11509"))
    assert services.plex_server.fixed == ["plex://movie/6856893830a4aaafd5c4291d"]
    assert text == ("Plex had matched Obsession (2026) to another film. Its file runs 109 minutes, the length of "
                    "Obsession (2026), so Plexbie matched it to the right one.")


def test_anything_less_certain_is_left_to_the_admins():
    short = _services(2)
    assert asyncio.run(plex_match.fix_film_match(short, FILM, "11509")) is None and short.plex_server.fixed == []
    namesake = _services(109, titles=["Obsession Du sollst mich lieben"])
    assert asyncio.run(plex_match.fix_film_match(namesake, FILM, "11509")) is None, "the download is another film's"
    assert namesake.plex_server.asked == []
    no_runtime = _services(109)
    assert asyncio.run(plex_match.fix_film_match(no_runtime, {**FILM, "runtime": 0}, "11509")) is None
    assert plex_match.same_length(112, 109) and not plex_match.same_length(120, 109)
