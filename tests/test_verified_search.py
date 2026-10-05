# path: tests/test_verified_search.py
"""Searches that check a release really is the film or show (core/verified_search).
The data is Obsession (2026): Radarr grabbed "Obsession - Du sollst mich lieben",
another film, because an indexer labelled it with Obsession's TMDB id."""
import asyncio

import conftest  # noqa: F401

from core import verified_search as vs

OBSESSION = {"id": 231, "title": "Obsession", "originalTitle": "Obsession", "sortTitle": "obsession",
             "year": 2026, "secondaryYear": 2025, "tmdbId": 1339713, "imdbId": "tt37287335",
             "qualityProfileId": 8, "hasFile": False,
             "alternateTitles": [{"title": "Obsessão"}, {"title": "Saplantı"}, {"title": "Одержимость"}]}

PROFILE = {"minFormatScore": -1000, "items": [
    {"allowed": False, "quality": {"id": 1, "name": "SDTV"}},
    {"allowed": True, "name": "WEB 1080p", "items": [{"quality": {"id": 3, "name": "WEBDL-1080p"}},
                                                      {"quality": {"id": 15, "name": "WEBRip-1080p"}}]},
    {"allowed": True, "quality": {"id": 7, "name": "Bluray-1080p"}},
    {"allowed": True, "quality": {"id": 19, "name": "Bluray-2160p"}},
]}

GERMAN = "Obsession.Du.sollst.mich.lieben.2025.German.DL.2160p.UHD.BluRay.HEVC-UNTHEVC-FTP"


def test_a_release_is_the_film_only_under_one_of_its_own_titles():
    assert vs.is_own_title(["Obsession"], OBSESSION)
    assert vs.is_own_title(["OBSESSAO"], OBSESSION), "accents and case don't count"
    assert vs.is_own_title(["Saplanti Obsession"], OBSESSION), "two of its titles run together"
    assert not vs.is_own_title(["Obsession Du sollst mich lieben"], OBSESSION), "a longer title is another film"
    assert not vs.is_own_title([], OBSESSION) and not vs.is_own_title([""], OBSESSION)
    assert vs.norm("Tom & Jerry") == vs.norm("Tom.and.Jerry")


def test_quality_ranks_follow_the_profile_and_skip_what_it_doesnt_allow():
    ranks = vs.quality_ranks(PROFILE)
    assert 1 not in ranks and ranks[3] == ranks[15] < ranks[7] < ranks[19]


class FakeRadarr:
    def __init__(self, releases=(), queue=(), parse=None, takes=lambda title: True):
        self._releases, self._queue, self._parse, self.takes = list(releases), list(queue), parse or {}, takes
        self.grabbed, self.pushed, self.removed = [], [], []

    async def releases(self, **params):
        return self._releases

    async def post(self, path, body):
        assert path == "release"
        self.grabbed.append(body["guid"])

    async def push(self, body):
        self.pushed.append(body)
        ok = self.takes(body["title"])
        return {"approved": ok, "rejections": [] if ok else ["Not enough disk space"]}

    async def queue(self, **params):
        return self._queue

    async def delete(self, path, **params):
        self.removed.append((path, params))

    async def get(self, path, **params):
        if path == "parse":
            return self._parse.get(params["title"])
        if path.startswith("qualityprofile/"):
            return PROFILE
        if path.startswith("movie/"):
            return OBSESSION
        raise AssertionError(path)


def _release(title, titles, approved=True, guid=None):
    return {"title": title, "movieTitles": titles, "approved": approved, "guid": guid or title, "indexerId": 43,
            "rejections": [] if approved else ["Existing file on disk is of equal or higher preference"]}


def test_by_id_grabs_radarrs_favourite_that_is_the_film_and_skips_a_namesake():
    radarr = FakeRadarr(releases=[_release(GERMAN, ["Obsession Du sollst mich lieben"]),
                                  _release("Obsession.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-KyoGo", ["Obsession"]),
                                  _release("Obsession.2026.720p.WEB.h264-ETHEL", ["Obsession"])])
    out = asyncio.run(vs.movie_by_id(radarr, OBSESSION))
    assert radarr.grabbed == ["Obsession.2026.1080p.AMZN.WEB-DL.DDP5.1.H.264-KyoGo"], "Radarr's order: its favourite first"
    assert out["grabbed"].startswith("Obsession.2026.1080p") and out["namesakes"] == [GERMAN]


def test_by_id_with_only_a_namesake_to_take_grabs_nothing_and_says_why():
    radarr = FakeRadarr(releases=[_release(GERMAN, ["Obsession Du sollst mich lieben"]),
                                  _release("Obsession.2026.2160p.Remux", ["Obsession"], approved=False)])
    out = asyncio.run(vs.movie_by_id(radarr, OBSESSION))
    assert radarr.grabbed == [] and out["grabbed"] is None
    text = vs.describe_by_id("Obsession", {"state": "searched", **out})
    assert "2 releases came back" in text and "another film" in text and GERMAN in text
    assert "equal or higher preference" in text, "and why Radarr turned the real ones down"


def test_a_download_of_another_film_is_removed_and_blocklisted_then_searched_by_id():
    queue = [{"id": 9, "movieId": 231, "title": GERMAN, "downloadId": "SAB_1"}]
    parse = {GERMAN: {"parsedMovieInfo": {"movieTitles": ["Obsession Du sollst mich lieben"]}}}
    radarr = FakeRadarr(queue=queue, parse=parse,
                        releases=[_release("Obsession.2026.1080p.BluRay.x264-LuCY", ["Obsession"])])
    out = asyncio.run(vs.verify_movie(radarr, OBSESSION))
    assert radarr.removed == [("queue/9", {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"})]
    assert out["state"] == "searched" and out["removed"] == [GERMAN] and out["grabbed"].endswith("LuCY")
    assert vs.describe_by_id("Obsession", out).startswith(f"Stopped {GERMAN}: it isn't Obsession. Found Obsession")


def test_the_right_download_is_left_alone():
    queue = [{"id": 9, "movieId": 231, "title": "Obsession.2026.1080p.BluRay.x264-LuCY"}]
    parse = {queue[0]["title"]: {"parsedMovieInfo": {"movieTitles": ["Obsession"]}}}
    radarr = FakeRadarr(queue=queue, parse=parse)
    out = asyncio.run(vs.verify_movie(radarr, OBSESSION))
    assert out == {"state": "downloading", "removed": []} and radarr.removed == [] and radarr.grabbed == []


class FakeHydra:
    configured = True

    def __init__(self, results):
        self.results, self.queries = results, []

    async def search(self, query, categories, **kw):
        self.queries.append((query, list(categories)))
        return [{"title": t, "link": f"http://hydra/{i}", "size": s, "pubDate": "Thu, 01 Oct 2026 14:09:28 +0000"}
                for i, (t, s) in enumerate(self.results.get(query, []))]


def _parsed(titles, year, quality, score=0):
    return {"parsedMovieInfo": {"movieTitles": titles, "year": year, "quality": {"quality": {"id": quality}}},
            "customFormatScore": score}


def test_by_name_offers_the_best_release_of_the_film_first_and_stops_when_radarr_takes_one():
    hydra = FakeHydra({"Obsession 2026": [
        ("Obsession.2026.1080p.AMZN.WEB-DL-KyoGo", 6_000), ("Obsession.2026.2160p.BluRay-LuCY", 30_000),
        ("Obsession.2026.2160p.BluRay-HDT", 20_000), (GERMAN, 40_000), ("Obsession.2026.480p.SDTV-x", 500),
        ("Totally.Different.2026.1080p", 1)]})
    parse = {"Obsession.2026.1080p.AMZN.WEB-DL-KyoGo": _parsed(["Obsession"], 2026, 3),
             "Obsession.2026.2160p.BluRay-LuCY": _parsed(["Obsession"], 2026, 19),
             "Obsession.2026.2160p.BluRay-HDT": _parsed(["Obsession"], 2026, 19),
             GERMAN: _parsed(["Obsession Du sollst mich lieben"], 2025, 19),
             "Obsession.2026.480p.SDTV-x": _parsed(["Obsession"], 2026, 1)}
    radarr = FakeRadarr(parse=parse, takes=lambda title: "HDT" in title)
    out = asyncio.run(vs.movie_by_name(radarr, hydra, OBSESSION))
    assert [p["title"] for p in radarr.pushed] == ["Obsession.2026.2160p.BluRay-LuCY", "Obsession.2026.2160p.BluRay-HDT"]
    first = radarr.pushed[0]
    assert first["tmdbId"] == 1339713 and first["imdbId"] == 37287335 and first["protocol"] == "usenet"
    assert first["downloadUrl"] == "http://hydra/1" and first["publishDate"].startswith("2026-10-01T14:09:28")
    assert out["grabbed"] == "Obsession.2026.2160p.BluRay-HDT" and hydra.queries == [("Obsession 2026", ["2000"])]
    assert out["matching"] == 4, "the German release and the stranger aren't the film"
    assert vs.describe_by_name("Obsession", out) == \
        'Searched NZBHydra for "Obsession 2026": found Obsession and grabbed Obsession.2026.2160p.BluRay-HDT.'


def test_by_name_tries_the_other_year_only_when_the_main_one_had_nothing():
    hydra = FakeHydra({"Obsession 2026": [], "Obsession 2025": [("Obsession.2025.1080p.BluRay-AAA", 9)]})
    radarr = FakeRadarr(parse={"Obsession.2025.1080p.BluRay-AAA": _parsed(["Obsession"], 2025, 7)}, takes=lambda t: False)
    out = asyncio.run(vs.movie_by_name(radarr, hydra, OBSESSION))
    assert [q for q, _ in hydra.queries] == ["Obsession 2026", "Obsession 2025"]
    assert out["grabbed"] is None and out["matching"] == 1 and out["why"] == "Not enough disk space"
    assert "1 release(s) were Obsession, none of them acceptable. Turned down: Not enough disk space." in \
        vs.describe_by_name("Obsession", out)


SHOW = {"id": 7, "title": "Coven Academy", "tvdbId": 4242, "qualityProfileId": 4, "alternateTitles": []}


class FakeSonarr:
    def __init__(self, parse, takes):
        self._parse, self.takes, self.pushed = parse, takes, []

    async def episodes(self, series_id):
        return [{"id": 20 + n, "seasonNumber": 2, "episodeNumber": n, "hasFile": n == 1,
                 "airDateUtc": "2000-01-01T00:00:00Z"} for n in range(1, 4)]

    async def get(self, path, **params):
        if path == "parse":
            return self._parse.get(params["title"])
        return PROFILE

    async def push(self, body):
        self.pushed.append(body["title"])
        return {"approved": self.takes(body["title"])}


def _ep(season, episodes, quality=3, full=False, title="Coven Academy"):
    return {"parsedEpisodeInfo": {"seriesTitle": title, "seasonNumber": season, "episodeNumbers": episodes,
                                  "fullSeason": full, "quality": {"quality": {"id": quality}}}}


def test_show_by_name_prefers_a_season_pack_then_falls_back_to_single_episodes():
    results = {"Coven Academy S02": [("Coven.Academy.S02.1080p.WEB-ETHEL", 9), ("Coven.Academy.S02E02.1080p.WEB-A", 1),
                                     ("Coven.Academy.S02E03.1080p.WEB-A", 1), ("Coven.Academy.S02E01.1080p.WEB-A", 1),
                                     ("Coven.Academy.Reunion.S02E02.1080p", 1)]}
    parse = {"Coven.Academy.S02.1080p.WEB-ETHEL": _ep(2, [], full=True),
             "Coven.Academy.S02E02.1080p.WEB-A": _ep(2, [2]), "Coven.Academy.S02E03.1080p.WEB-A": _ep(2, [3]),
             "Coven.Academy.S02E01.1080p.WEB-A": _ep(2, [1]),
             "Coven.Academy.Reunion.S02E02.1080p": _ep(2, [2], title="Coven Academy Reunion")}
    pack = FakeSonarr(parse, takes=lambda t: True)
    out = asyncio.run(vs.show_by_name(pack, FakeHydra(results), SHOW, [2]))
    assert pack.pushed == ["Coven.Academy.S02.1080p.WEB-ETHEL"] and out["grabbed"] == pack.pushed
    singles = FakeSonarr(parse, takes=lambda t: "ETHEL" not in t)
    out = asyncio.run(vs.show_by_name(singles, FakeHydra(results), SHOW, [2]))
    assert singles.pushed == ["Coven.Academy.S02.1080p.WEB-ETHEL", "Coven.Academy.S02E02.1080p.WEB-A",
                              "Coven.Academy.S02E03.1080p.WEB-A"], "episode 1 is on disk; the reunion show isn't it"
    assert out["nothing"] == [] and len(out["grabbed"]) == 2


def test_name_search_on_a_help_request_reports_back_and_resolves_it_when_found():
    from portal import help as helpdesk
    import database.request_store as request_store
    store = {"h1": {"request": "20", "title": "Obsession", "status": "open"}}
    told = []

    async def kv_get(ns, key):
        return store.get(key)

    async def kv_set(ns, key, value):
        store[key] = value

    async def get_request(key):
        return {"media": {"media_type": "movie", "id": 1339713, "title": "Obsession"}}

    async def find_movie(radarr, tmdb_id):
        return OBSESSION

    async def by_name(radarr, hydra, movie):
        await asyncio.sleep(0)
        return {"grabbed": "Obsession.2026.2160p.BluRay-HDT", "queries": ["Obsession 2026"], "matching": 3}

    class Radarr:
        configured = True

    class Services:
        radarr, hydra = Radarr(), FakeHydra({})

    async def tell(text):
        told.append(text)

    saved = (helpdesk.kv_get, helpdesk.kv_set, helpdesk.get_request, vs.find_movie, vs.movie_by_name, request_store.get_request)
    helpdesk.kv_get, helpdesk.kv_set, helpdesk.get_request = kv_get, kv_set, get_request
    vs.find_movie, vs.movie_by_name = find_movie, by_name

    async def scenario():
        message = await helpdesk.name_search_for_help(Services(), "h1", "Omar", tell=tell)
        while vs._tasks:
            await asyncio.sleep(0)
        return message
    try:
        message = asyncio.run(scenario())
    finally:
        (helpdesk.kv_get, helpdesk.kv_set, helpdesk.get_request, vs.find_movie, vs.movie_by_name,
         request_store.get_request) = saved
    assert message.startswith('Plexbie is searching NZBHydra for "Obsession 2026"')
    h = store["h1"]
    assert [a["by"] for a in h["actions"]] == ["Omar", "Plexbie"]
    assert h["status"] == "resolved" and h["resolved_by"] == "Plexbie" and "grabbed Obsession.2026.2160p.BluRay-HDT" in h["reply"]
    assert told and told[0].startswith("✅ **Obsession**: Searched NZBHydra")


def test_the_discord_button_finds_its_help_request_from_the_alert_footer():
    import discord
    from portal.help_view import footer, help_id, HelpByNameView
    from core.permissions import AdminActionView
    embed = discord.Embed(title="🆘 Help asked on No. 0226: Obsession").set_footer(text=footer("158a46c137d5"))

    class Message:
        embeds = [embed]
    assert help_id(Message()) == "158a46c137d5"
    assert help_id(type("M", (), {"embeds": [discord.Embed(title="x")]})()) is None
    assert issubclass(HelpByNameView, AdminActionView), "admins only, one click at a time"


def test_a_wrong_upgrade_of_a_film_on_disk_is_removed_without_searching():
    queue = [{"id": 9, "movieId": 231, "title": GERMAN}]
    radarr = FakeRadarr(queue=queue, parse={GERMAN: {"parsedMovieInfo": {"movieTitles": ["Obsession Du sollst mich lieben"]}}})

    async def on_disk(path, **params):
        return {**OBSESSION, "hasFile": True} if path == "movie/231" else await FakeRadarr.get(radarr, path, **params)
    radarr.get = on_disk
    out = asyncio.run(vs.verify_movie(radarr, OBSESSION))
    assert out == {"state": "on disk", "removed": [GERMAN]} and radarr.grabbed == []
    assert vs.describe_by_id("Obsession", out) == f"Stopped {GERMAN}: it isn't Obsession. Obsession is already on disk."


def test_a_film_thats_not_out_yet_isnt_searched_and_nobody_is_asked():
    radarr = FakeRadarr(releases=[_release("Verity.2026.1080p.WEB.H264-NTb", ["Verity"])])
    verity = {**OBSESSION, "title": "Verity", "inCinemas": "2099-09-30T00:00:00Z", "digitalRelease": "2099-10-27T00:00:00Z"}

    async def get(path, **params):
        return verity
    radarr.get = get
    out = asyncio.run(vs.verify_movie(radarr, verity))
    assert out["state"] == "upcoming" and radarr.grabbed == [], "anything posted before it's out is fake"
    assert vs.describe_by_id("Verity", out).startswith("Verity isn't out yet, so there's nothing real to find. Out to stream Oct 27, 2099.")
