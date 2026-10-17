# path: tests/test_manage_posters.py
"""Manage's posters for tickets, blocked imports and titles leaving soon come from the server.

The page used to guess them, borrowing a request's poster when one had the same title,
and drew a cover for everything else. Now each payload carries its own: a ticket its
request's poster (or its title's, by TMDB id), a blocked import its show's or film's,
and a title leaving soon Plex's own. Every one is an address on the site's image proxy
(/img/tmdb or /img/plex), so browsers still meet no outside host, and it's None when
there's nothing to show (a book, a general ticket, Seerr down): the page draws a cover.
"""
import asyncio
import pathlib
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401

from core.clients import ServiceError
from portal import cleanup as countdown
from portal.admin import Admin
from test_blocked_imports import GOOD, Arr, Services, _db, _folder
from test_member_tickets import JORDAN, _actions, _init, _seen

POSTERS = {"movie/45745": "/sintelPoster.jpg", "tv/9999": "/pepperPoster.jpg"}


def _with_posters(actions, down=False):
    """Seerr's word on each title, with a poster; `down`: Seerr doesn't answer at all."""
    from test_member_tickets import SEERR
    asked = []

    async def seerr(path, ttl=0):
        asked.append(path)
        if down:
            raise ServiceError("Seerr answered HTTP 502", 502)
        if path not in SEERR:
            raise ServiceError("Seerr answered HTTP 404", 404)
        return {**SEERR[path], "posterPath": POSTERS.get(path)}
    actions.data._seerr = seerr
    return asked


def test_a_tickets_poster_is_its_requests_else_its_titles_and_a_general_one_has_none():
    from database.request_store import mark_resolved, save_request
    actions = _actions()
    _with_posters(actions)

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(301, user_id=7, media={"id": 10378, "media_type": "movie", "title": "Big Buck Bunny",
                                                  "poster_path": "/bunnyRequest.jpg"})
        await mark_resolved(301, "approved", "Sam")
        on_request = (await actions.ask_help(JORDAN, "301", {"reason": "stuck", "note": "0% all morning"}))["help"]["id"]
        on_title = (await actions.open_ticket(JORDAN, {"kind": "tv", "id": "9999", "reason": "episodes"}))["help"]["id"]
        general = (await actions.open_ticket(JORDAN, {"reason": "access"}))["help"]["id"]
        admin = Admin(actions.data)
        listed = {r["id"]: r["poster"] for r in (await admin.tickets())["rows"]}
        return listed, on_request, on_title, general, (await admin.ticket(on_title))["poster"]

    with _seen():
        listed, on_request, on_title, general, detail = asyncio.run(scenario())
    assert listed[on_request] == "/img/tmdb/w185/bunnyRequest.jpg?v=2", "the request's own poster"
    assert listed[on_title] == "/img/tmdb/w185/pepperPoster.jpg?v=2", "the show's poster, by its TMDB id"
    assert listed[general] is None, "nothing to show: the page draws a cover"
    assert detail == listed[on_title], "the ticket in full carries the same poster"



def test_a_members_own_tickets_carry_the_posters_manage_shows():
    """The member's list (/api/tickets) and the ticket they've just opened carry the same
    poster as Manage's list, so the app shows a title ticket's poster, not a drawn cover. It's
    left out when there's none (a general ticket)."""
    import tempfile as tmp

    from aiohttp.test_utils import TestClient, TestServer
    from database.request_store import mark_resolved, save_request
    from portal.app import build_app
    actions = _actions()
    _with_posters(actions)

    async def who(request):
        return JORDAN

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(301, user_id=7, media={"id": 10378, "media_type": "movie", "title": "Big Buck Bunny",
                                                  "poster_path": "/bunnyRequest.jpg"})
        await mark_resolved(301, "approved", "Sam")
        on_request = (await actions.ask_help(JORDAN, "301", {"reason": "stuck", "note": "0% all morning"}))["help"]["id"]
        opened = await actions.open_ticket(JORDAN, {"kind": "tv", "id": "9999", "reason": "episodes"})
        general = await actions.open_ticket(JORDAN, {"reason": "access"})
        manage = {r["id"]: r["poster"] for r in (await Admin(actions.data).tickets())["rows"]}
        client = TestClient(TestServer(build_app(actions.services, who=who, readonly=False, dist=None, image_cache=tmp.mkdtemp(),
                                                 data=actions.data, actions=actions)))
        await client.start_server()
        try:
            listed = await (await client.get("/api/tickets")).json()
        finally:
            await client.close()
        return on_request, opened, general, manage, {r["id"]: r for r in listed}

    with _seen():
        on_request, opened, general, manage, listed = asyncio.run(scenario())
    on_title, plain = opened["help"]["id"], general["help"]["id"]
    assert listed[on_request]["poster"] == manage[on_request] == "/img/tmdb/w185/bunnyRequest.jpg?v=2", "the request's own poster"
    assert listed[on_title]["poster"] == manage[on_title] == "/img/tmdb/w185/pepperPoster.jpg?v=2", "the show's, by its TMDB id"
    assert "poster" not in listed[plain], "nothing to show: the app draws a cover"
    assert opened["ticket"]["poster"] == listed[on_title]["poster"], "the ticket just opened carries it too"
    assert "poster" not in general["ticket"]


def test_tickets_still_list_when_seerr_cant_say_what_a_poster_is():
    actions = _actions()

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        _with_posters(actions)
        hid = (await actions.open_ticket(JORDAN, {"kind": "movie", "id": "45745", "reason": "audio"}))["help"]["id"]
        _with_posters(actions, down=True)
        actions.data.cache = type(actions.data.cache)()    # nothing remembered from opening it
        return hid, (await Admin(actions.data).tickets())["rows"]

    with _seen():
        hid, rows = asyncio.run(scenario())
    assert [(r["id"], r["poster"]) for r in rows] == [(hid, None)]


def test_a_blocked_import_carries_its_shows_poster():
    from portal.actions import Actions
    asked = []

    class Posters:
        async def poster(self, kind, tmdb):
            asked.append((kind, tmdb))
            return f"/img/tmdb/w185/p{tmdb}.jpg?v=2"

    async def body():
        actions = Actions(bot=None, services=Services(Arr(_folder(), GOOD)), data=Posters(), public_url="")
        return await actions.blocked_list({"user": {"id": "1"}})
    out = _db(body)
    assert [r["poster"] for r in out["rows"]] == ["/img/tmdb/w185/p555.jpg?v=2"] and asked == [("tv", 555)]


def test_a_poster_is_looked_up_once_and_only_for_films_and_shows():
    actions = _actions()
    asked = _with_posters(actions)

    async def scenario():
        return [await actions.data.poster("movie", "45745"), await actions.data.poster("movie", 45745),
                await actions.data.poster("ebook", "45745"), await actions.data.poster("tv", None),
                await actions.data.poster("tv", "../etc"), await actions.data.poster("movie", "457")]
    found = asyncio.run(scenario())
    assert found == ["/img/tmdb/w185/sintelPoster.jpg?v=2", "/img/tmdb/w185/sintelPoster.jpg?v=2", None, None, None, None]
    assert asked.count("movie/45745") == 1, "a second look is the cached answer"


class _Plex:
    """One film in one library, idle for 500 days, with or without a poster."""

    def __init__(self, thumb):
        self.thumb = thumb

    def history(self, mindate=None):
        return []

    def query(self, path):
        if path == "/library/sections":
            return ET.fromstring('<MediaContainer><Directory key="1" type="movie" title="Movies"/></MediaContainer>')
        added = int((datetime.now(timezone.utc) - timedelta(days=500)).timestamp())
        thumb = f' thumb="{self.thumb}"' if self.thumb else ""
        return ET.fromstring(f'<MediaContainer><Video ratingKey="8" title="Heat" addedAt="{added}"{thumb}>'
                             '<Guid id="tmdb://949"/></Video></MediaContainer>')


def test_a_title_leaving_soon_carries_plexs_own_poster_through_the_proxy():
    config = {"inactivity_days": 90, "notify_days_before": 7, "exempt_items": {}}
    leaving = countdown.compute(_Plex("/library/metadata/8/thumb/1700000000"), config, [], {})["8"]
    bare = countdown.compute(_Plex(None), config, [], {})["8"]
    assert Admin._row(leaving)["poster"] == "/img/plex?p=%2Flibrary%2Fmetadata%2F8%2Fthumb%2F1700000000&w=185&v=2"
    assert Admin._row(bare)["poster"] is None
    # A kept title too, from what the countdown knows of it.
    assert Admin._kept("8", {"title": "Heat", "type": "movie"}, {**leaving, "exempt": True})["poster"] == Admin._row(leaving)["poster"]
    assert Admin._kept("9", {"title": "Gone", "type": "movie"}, {})["poster"] is None
