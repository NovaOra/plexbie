# path: tests/test_member_tickets.py
"""Tickets that aren't on a request: about a film or show on Plex, or general.

Until these, every ticket hung off a request, so a member with a problem on something
they never asked for (a film that plays washed out, a show missing an episode, Plex not
working for them at all) could only say so in Discord. These pin who may open one and
how often, that the admins hear about it with what Plexbie can see, that only the
member sees and answers their own, and that the tools built for request tickets (search
buttons, My requests, "Something wrong?" on a request) never trip over one without a
request.
"""
import asyncio
import contextlib
import json
import pathlib
import tempfile
from datetime import datetime, timezone

import conftest  # noqa: F401
from aiohttp import web
from helpers import FakeServices
from test_portal import ADMIN, MEMBER, OK_HEADERS, OUTSIDER, _Actions, _init, _run

from core.config import Config

JORDAN = {"user": {"id": "7", "name": "Jordan", "via": "discord"}, "member": True, "admin": False, "discordId": "7"}
PAT = {"user": {"id": "88", "name": "Pat", "via": "plex"}, "member": True, "admin": False, "plexAccountId": "88",
       "plexName": "pat"}
SAM = {**ADMIN, "user": {"id": "1", "name": "Sam", "via": "discord"}, "discordId": "1"}

#: Seerr's word on each title, and what Plex lists.
SEERR = {
    "movie/45745": {"title": "Sintel", "releaseDate": "2010-09-30", "mediaInfo": {"status": 5}},
    "movie/10378": {"title": "Big Buck Bunny", "releaseDate": "2008-05-30", "mediaInfo": {"status": 5}},
    "movie/457": {"title": "Not Out Yet", "releaseDate": "2027-01-01", "mediaInfo": None},
    "tv/9999": {"name": "Pepper & Carrot", "firstAirDate": "2021-01-01", "externalIds": {"tvdbId": 400},
                "mediaInfo": {"status": 4}},
}
PLEX = {"tmdb://45745": "501", "tmdb://10378": "502", "tvdb://400": "503"}
LIBRARY = {
    "plex:library:movie": [{"kind": "movie", "id": "45745", "tmdb": "45745", "ratingKey": "501", "title": "Sintel", "year": "2010"},
                           {"kind": "movie", "id": "10378", "tmdb": "10378", "ratingKey": "502", "title": "Big Buck Bunny", "year": "2008"},
                           {"kind": "movie", "id": "plex:504", "tmdb": None, "ratingKey": "504", "title": "Sintel's Home Movies", "year": ""}],
    "plex:library:tv": [{"kind": "tv", "id": "9999", "tmdb": "9999", "ratingKey": "503", "title": "Pepper & Carrot", "year": "2021"}],
}


class _Bot:
    """The admin channel and the member's DMs, recorded."""

    def __init__(self):
        self.posted, self.dms = [], []

    def get_channel(self, cid):
        posted = self.posted

        class Channel:
            async def send(self, content=None, embed=None, **kw):
                posted.append(embed or content)
        return Channel()

    def get_guild(self, gid):
        return None

    def get_user(self, uid):
        return None

    async def fetch_user(self, uid):
        dms = self.dms

        class U:
            id, name, display_name = uid, "Jordan", "Jordan"

            async def send(self, content=None, embed=None, view=None):
                dms.append({"title": embed.title if embed else None, "text": content or embed.description,
                            "footer": embed.footer.text if embed and embed.footer else None,
                            "button": bool(view and view.children)})
                return type("Sent", (), {"id": 555, "channel": type("C", (), {"id": 444})()})()
        return U()


class _Server:
    machineIdentifier = "abc123"


def _actions(bot=None):
    from core.clients import ServiceError
    from portal.data import Data
    actions = _Actions(FakeServices(Config()))
    actions.bot = bot or _Bot()
    actions.config.admin_channel_id = 9
    actions.public_url = "https://plexbie.example"
    actions.services.plex_server = _Server()
    actions.data = Data(actions.services)

    async def seerr(path, ttl=0):
        if path not in SEERR:
            raise ServiceError("Seerr answered HTTP 404", 404)
        return SEERR[path]

    async def plex_key(kind, *ids):
        return next((PLEX[i] for i in ids if i in PLEX), None)

    async def plex(fn, *args, key, ttl):
        return LIBRARY[key]
    actions.data._seerr, actions.data.progress._plex_key, actions.data._plex = seerr, plex_key, plex
    return actions


@contextlib.contextmanager
def _seen(lines=("File: 4K WEB-DL, Dolby Vision + HDR10, 27.7 GB",)):
    """Plexbie sees these lines (ticket_facts has its own test), and admin alerts are kept."""
    from core import notify
    from portal import ticket_facts
    saved = ticket_facts.title_facts, ticket_facts.general_facts, notify.push_to_admins
    asked, pushes = [], []

    async def title(services, progress, user, kind, tmdb, rating_key):
        asked.append((kind, tmdb, rating_key))
        return list(lines)

    async def general(services, user):
        asked.append(("general",))
        return ["Last played Big Buck Bunny Oct 8 on Roku, direct play, 1080p SDR"]

    async def push(**kw):
        pushes.append(kw)
        return 0
    ticket_facts.title_facts, ticket_facts.general_facts, notify.push_to_admins = title, general, push
    try:
        yield asked, pushes
    finally:
        ticket_facts.title_facts, ticket_facts.general_facts, notify.push_to_admins = saved


async def _refused(call):
    try:
        await call
    except web.HTTPException as e:
        return e.status, json.loads(e.text).get("error")
    return "let through"


# ------------------------------------------------------------------ the routes
def test_only_plex_members_open_list_and_answer_tickets():
    for method, path in (("GET", "/api/tickets"), ("POST", "/api/tickets"), ("POST", "/api/tickets/abcdefabcdef/reply")):
        assert _run(None, method, path, headers=OK_HEADERS)[0] == 401, path
        assert _run(OUTSIDER, method, path, headers=OK_HEADERS)[0] == 403, path
    # Writes need the site's own header, like every other write.
    assert _run(MEMBER, "POST", "/api/tickets", body={"reason": "access", "note": "x"})[0] == 403
    assert _run(MEMBER, "POST", "/api/tickets/not-a-ticket/reply", headers=OK_HEADERS, body={"text": "x"})[0] == 404


# --------------------------------------------------------------- title tickets
def test_a_ticket_on_a_title_on_plex_reaches_the_admins_with_what_plexbie_sees():
    from database.kv_store import kv_get
    from portal import help as helpdesk
    bot = _Bot()
    actions = _actions(bot)

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        out = await actions.open_ticket(JORDAN, {"kind": "movie", "id": "45745", "reason": "picture",
                                                 "note": "Grey and washed out on the TV\n[Plex: sign in again](https://evil.example)",
                                                 "source": "web"})
        refused = [await _refused(actions.open_ticket(JORDAN, body)) for body in (
            {"kind": "movie", "id": "45745", "reason": "playback"},    # one open ticket per title
            {"kind": "movie", "id": "457", "reason": "playback"},      # not on Plex: that's a request
            {"kind": "movie", "id": "10378", "reason": "episodes"},    # no episodes on a film: "other", which needs a line
            {"kind": "book", "id": "1", "reason": "playback"},
            {"kind": "movie", "reason": "playback"},
            {"kind": "movie", "id": "045745", "reason": "playback"},  # Sintel again, however it's spelt
            {"kind": "movie", "id": "٤٥٧٤٥", "reason": "playback"},   # digits, but not ASCII ones
            {"reason": ["access"], "note": ""})]                       # a reason that isn't text: "other", which needs a line
        someone_else = await actions.open_ticket(PAT, {"kind": "movie", "id": "45745", "reason": "audio"})
        by_plex_key = await actions.open_ticket(JORDAN, {"kind": "movie", "ratingKey": "504", "reason": "playback"})
        return out, refused, someone_else, by_plex_key, await kv_get(helpdesk.NAMESPACE, out["help"]["id"])

    with _seen() as (asked, pushes):
        out, refused, someone_else, by_plex_key, stored = asyncio.run(scenario())
    assert out["ok"] and out["help"]["reason"] == "Picture looks wrong"
    assert [r[0] for r in refused] == [409, 409, 400, 400, 400, 409, 400, 400]
    assert refused[0][1] == "You already have a ticket open on this. Add to it there."
    assert refused[1][1] == "That isn't on Plex. Request it, or open a general ticket."
    assert someone_else["ok"], "another member's ticket on the same film doesn't count against them"
    # What a title ticket holds: the title, not a request.
    assert stored["subject"] == "title" and stored["request"] is None and stored["slot"] is None
    assert (stored["title"], stored["year"], stored["tmdb"], stored["rating_key"], stored["kind"]) == \
        ("Sintel", "2010", 45745, "501", "movie")
    assert stored["status_then"] == "File: 4K WEB-DL, Dolby Vision + HDR10, 27.7 GB"
    assert asked[0] == ("movie", 45745, "501")
    # Found by Plex's own key (Discord's /ticket, a title Plex has no TMDB match for).
    plex_only = by_plex_key["ticket"]
    assert (plex_only["title"], plex_only["tmdb"], plex_only["kind"]) == ("Sintel's Home Movies", None, "movie")
    # The admins: an embed that says what it's on, with what Plexbie sees and a Plex link,
    # and an alert that opens the ticket itself.
    card = bot.posted[0]
    assert card.title == "🆘 Ticket on Sintel (2010)"
    assert "Picture looks wrong" in card.description and "Grey and washed out" in card.description
    # Escaped (Discord shows "\[…](…)" as written, the address in plain sight), never a masked link.
    assert "\\[Plex: sign in again](https://evil.example)" in card.description
    assert all(line.startswith("> ") for line in card.description.splitlines()[1:])
    fields = {f.name: f.value for f in card.fields}
    assert fields["From"] == "Jordan (<@7>)" and fields["Plexbie sees"] == stored["status_then"]
    assert "app.plex.tv" in fields["On Plex"] and "501" in fields["On Plex"]
    assert card.url == f"https://plexbie.example/manage?tab=tickets&ticket={out['help']['id']}"
    assert pushes[0]["title"] == "Ticket on Sintel (2010)" and pushes[0]["url"] == f"/manage?tab=tickets&ticket={out['help']['id']}"
    assert pushes[0]["body"].startswith("Jordan: Picture looks wrong. Grey and washed out on the TV")
    # What the member gets back: the ticket as they see it.
    mine = out["ticket"]
    assert (mine["subject"], mine["title"], mine["kind"], mine["tmdb"], mine["status"]) == ("title", "Sintel", "movie", "45745", "open")
    assert [(e["kind"], e["by"]) for e in mine["thread"]] == [("member", "You")]


def test_a_tickets_reason_has_a_fixed_code_older_tickets_included():
    from portal import help as helpdesk
    from portal.admin import _ticket_row
    # Opened now: kept with the ticket, so new words for a reason never change what it is.
    assert helpdesk.reason_code({"reason": "Whatever the words are now", "reason_code": "access"}) == "access"
    # Opened before codes were kept: worked out from the words it was opened with.
    assert helpdesk.reason_code({"reason": "Can't get in, or nothing plays"}) == "access"
    assert helpdesk.reason_code({"reason": "Downloaded, but won't import"}) == "blocked"
    assert helpdesk.reason_code({"reason": "Picture looks wrong"}) == "picture"
    # An admin's own ticket has words no code has.
    assert helpdesk.reason_code({"reason": "Opened by an admin"}) == ""
    row = _ticket_row("h1", {"subject": "general", "reason": "Can't get in, or nothing plays", "who": "Jordan",
                             "status": "open", "created_at": "2026-10-09T10:00:00+00:00"})
    assert row["reason"] == "Can't get in, or nothing plays" and row["reasonCode"] == "access"


def test_general_tickets_are_capped_and_only_their_member_lists_and_answers_them():
    from portal import help as helpdesk
    from portal.admin import Admin
    bot = _Bot()
    actions = _actions(bot)

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        made = [await actions.open_ticket(JORDAN, {"reason": "access", "note": f"Try {n}"}) for n in range(3)]
        fourth = await _refused(actions.open_ticket(JORDAN, {"reason": "access"}))
        show = await actions.open_ticket(JORDAN, {"kind": "tv", "id": "9999", "reason": "episodes"})   # 4 today
        hid = made[0]["help"]["id"]
        await actions.ticket_comment(SAM, hid, {"kind": "reply", "text": "Which device is it on?"})
        await actions.ticket_status(SAM, hid, {"status": "waiting"})
        listed, pats = await helpdesk.tickets_of(JORDAN), await helpdesk.tickets_of(PAT)
        stranger = await _refused(actions.ticket_reply(PAT, hid, {"text": "Me too"}))
        answered = await actions.ticket_reply(JORDAN, hid, {"text": "The Roku\nin the lounge"})       # 5 today
        over = await _refused(actions.ticket_reply(JORDAN, hid, {"text": "And the TV"}))              # the daily allowance
        solved = await actions.ticket_status(SAM, hid, {"status": "resolved", "message": ""})
        closed = await _refused(actions.ticket_reply(JORDAN, hid, {"text": "It's back"}))
        searched = [await actions.help_search(SAM, hid), await actions.help_search(SAM, show["help"]["id"])]
        admin = Admin(actions.data)
        return (made, fourth, listed, pats, stranger, answered, over, solved, closed, searched,
                await admin.tickets(), await admin.ticket(hid), await helpdesk.open_for(), await actions.data.my_requests(7))

    with _seen() as (asked, pushes):
        (made, fourth, listed, pats, stranger, answered, over, solved, closed, searched,
         tickets, detail, request_help, my_requests) = asyncio.run(scenario())
    assert all(m["ok"] for m in made) and fourth == (409, "You have 3 general tickets open already. Add to one of those.")
    hid = made[0]["help"]["id"]
    # Their own list: everything they opened, the one waiting on their answer first, without
    # the admins' wording of its status; nobody else's.
    assert len(listed) == 4 and listed[0]["id"] == hid and listed[0]["waiting"] and listed[0]["subject"] == "general"
    assert [e["kind"] for e in listed[0]["thread"]] == ["member", "reply"] and listed[0]["title"] is None
    assert {r["subject"] for r in listed} == {"general", "title"} and pats == []
    assert stranger == (404, "No such ticket.") and answered["ok"]
    assert over[0] == 429 and solved["ok"] and closed == (409, "This ticket is closed. Open a new one if it's still wrong.")
    # The member's DMs speak of their ticket, not a request, and point at Home.
    reply, last = bot.dms[0], bot.dms[-1]
    assert reply["title"] == "🛠️ About your ticket" and reply["text"] == "**Message from Sam**\nWhich device is it on?"
    assert reply["button"] and "Home, Your tickets" in reply["footer"]
    assert last["text"] == "An admin looked into your ticket and it should be sorted now." and not last["button"]
    answer = next(p for p in bot.posted if isinstance(p, str) and "answered on" in p)
    assert "**Jordan** answered on their general ticket: The Roku in the lounge\nhttps://" in answer and answer.count("\n") == 1
    general_card = bot.posted[0]
    assert general_card.title == "🆘 General ticket from Jordan" and "On Plex" not in {f.name for f in general_card.fields}
    assert {f.name: f.value for f in general_card.fields}["Plexbie sees"].startswith("Last played Big Buck Bunny")
    # Manage says what each is about, and the search buttons only work for a title.
    assert {r["subject"] for r in tickets["rows"]} == {"general", "title"} and all(r["requestKey"] is None for r in tickets["rows"])
    # Each carries its reason as a fixed code beside the words: what Manage reads to put
    # "can't get in" first, whatever the words say.
    assert {r["reasonCode"] for r in tickets["rows"]} == {"access", "episodes"}
    assert detail["subject"] == "general" and detail["request"] is None and detail["plexUrl"] is None
    assert searched[0] == {"ok": False, "message": "This ticket isn't about one film or show Plexbie can look up, so there's nothing to search for"}
    assert searched[1] == {"ok": False, "message": "Sonarr isn't set up"}
    # Request-only places never see them.
    assert request_help == {} and my_requests == []


def test_a_request_ticket_is_still_answered_by_its_request_and_now_by_its_id():
    """Older apps answer on a request's ticket by the request; Discord's Reply now goes by id."""
    from database.request_store import mark_resolved, save_request
    from portal import help as helpdesk
    actions = _actions()

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(301, user_id=7, media={"id": 45745, "media_type": "movie", "title": "Sintel"})
        await mark_resolved(301, "approved", "Sam")
        asked = await actions.ask_help(JORDAN, "301", {"reason": "stuck", "note": "0% all morning"})
        hid = asked["help"]["id"]
        old = await actions.member_reply(JORDAN, "301", {"text": "Still 0%"})
        new = await actions.ticket_reply(JORDAN, hid, {"text": "Now 5%"})
        # A title ticket on the film they requested would be a second ticket on it.
        twice = await _refused(actions.open_ticket(JORDAN, {"kind": "movie", "id": "45745", "reason": "playback"}))
        return old, new, twice, await helpdesk.tickets_of(JORDAN), (await helpdesk.open_for({"301"}))["301"]

    with _seen():
        old, new, twice, listed, ticket = asyncio.run(scenario())
    assert old["ok"] and new["ok"] and twice[0] == 409
    assert [e["text"] for e in helpdesk.thread_of(ticket) if e["kind"] == "member"][-2:] == ["Still 0%", "Now 5%"]
    assert listed[0]["subject"] == "request" and listed[0]["requestKey"] == "301" and listed[0]["slot"] == 1


def test_a_ticket_an_admin_opened_quietly_is_neither_shown_on_the_title_nor_in_the_way():
    from database.request_store import mark_resolved, save_request
    from portal import help as helpdesk
    actions = _actions()
    actions.services.plex_server = None

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(302, user_id=7, media={"id": 45745, "media_type": "movie", "title": "Sintel"})
        await mark_resolved(302, "approved", "Sam")
        await actions.admin_ticket(ADMIN, "302", {"note": "Checking the release names", "tell": False})
        page = await actions.data.title("movie", "45745", 7)
        theirs = await actions.open_ticket(JORDAN, {"kind": "movie", "id": "45745", "reason": "playback"})
        return page, theirs, await helpdesk.tickets_of(JORDAN)

    with _seen():
        page, theirs, listed = asyncio.run(scenario())
    assert "yourTicket" not in page
    assert theirs["ok"] and [t["subject"] for t in listed] == ["title"]


def test_sends_at_once_meet_each_other_in_the_checks():
    """A double tap, a retry or a second pick in Discord waits for the first send."""
    from portal import help as helpdesk
    actions = _actions()

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        film = await asyncio.gather(*(actions.open_ticket(JORDAN, {"kind": "movie", "id": "45745", "reason": "audio"})
                                      for _ in range(2)), return_exceptions=True)
        general = await asyncio.gather(*(actions.open_ticket(PAT, {"reason": "access"}) for _ in range(5)),
                                       return_exceptions=True)
        return film, general, await helpdesk.open_general(PAT)

    with _seen():
        film, general, open_general = asyncio.run(scenario())
    assert sorted("ok" if isinstance(r, dict) else str(r.status) for r in film) == ["409", "ok"]
    assert sorted("ok" if isinstance(r, dict) else str(r.status) for r in general) == ["409", "409", "ok", "ok", "ok"]
    assert open_general == 3


def test_messages_names_the_ticket_its_button_files_a_dm_on():
    """With several open, both mean the one updated last."""
    from core import message_log
    from portal.admin import Admin
    actions = _actions()

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        general = (await actions.open_ticket(JORDAN, {"reason": "access"}))["help"]["id"]
        await actions.open_ticket(JORDAN, {"kind": "movie", "id": "45745", "reason": "audio"})   # opened later
        await actions.ticket_reply(JORDAN, general, {"text": "Still can't get in"})               # updated last
        key = await message_log.record(channel="discord", direction="in", text="any news?", discord_id=7, discord_name="Jordan",
                                       context="Discord DM")
        named = next(p for p in await Admin(actions.data).message_people() if p["id"] == "d7")["ticket"]
        filed = await actions.message_to_ticket(ADMIN, key)
        return general, named, filed

    with _seen():
        general, named, filed = asyncio.run(scenario())
    assert named["id"] == general == filed["ticket"] and named["subject"] == "general"
    assert filed["message"] == "Added to their general ticket."


def test_an_alert_about_one_ticket_never_replaces_anothers():
    from core import notify
    actions = _actions()
    tags = []

    async def notify_member(services, **kw):
        tags.append(kw.get("tag"))
        return "push"

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        ids = [(await actions.open_ticket(PAT, {"reason": "access", "note": f"TV {n}"}))["help"]["id"] for n in (1, 2)]
        for hid in ids:
            await actions.ticket_comment(SAM, hid, {"kind": "reply", "text": "Which TV?"})
        return ids

    saved = notify.notify_member
    notify.notify_member = notify_member
    try:
        with _seen():
            ids = asyncio.run(scenario())
    finally:
        notify.notify_member = saved
    assert tags == [f"ticket-{hid}" for hid in ids]


def test_a_cancelled_look_cancels_what_it_started():
    from portal import ticket_facts
    cancelled = []

    async def scenario():
        started = asyncio.Event()

        async def slow():
            started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        look = asyncio.ensure_future(ticket_facts._gather([slow()]))
        await started.wait()
        look.cancel()
        try:
            await look
        except asyncio.CancelledError:
            pass
        for _ in range(3):
            await asyncio.sleep(0)
        return list(cancelled)       # before asyncio.run's own clean-up cancels what's left

    assert asyncio.run(scenario()) == [True]


def test_a_title_page_says_whether_a_ticket_can_be_opened_and_shows_yours():
    actions = _actions()
    actions.services.plex_server = None

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        before = await actions.data.title("movie", "45745", 7)
        await actions.open_ticket(JORDAN, {"kind": "movie", "id": "45745", "reason": "audio"})
        after = await actions.data.title("movie", "45745", 7)
        theirs = await actions.data.title("movie", "45745", None, "88")
        absent = await actions.data.title("movie", "457", 7)
        return before, after, theirs, absent

    with _seen():
        before, after, theirs, absent = asyncio.run(scenario())
    assert before["onPlex"] is True and "yourTicket" not in before
    mine = after["yourTicket"]
    assert (mine["subject"], mine["reason"], mine["status"]) == ("title", "Sound or subtitles", "open")
    assert [e["by"] for e in mine["thread"]] == ["You"]
    assert "yourTicket" not in theirs, "someone else's ticket isn't theirs"
    assert absent["onPlex"] is False


# --------------------------------------------------------------- Plexbie sees
def test_plexbie_sees_the_file_and_how_the_member_last_played_it():
    from core.clients import ServiceError
    from database.session import get_session
    from plugins.user_mgmt.models import PlexUser
    from portal import ticket_facts
    services = FakeServices(Config())
    services.config.tautulli_url, services.config.tautulli_token = "http://tautulli:8181", "t"
    asked, answers = [], {}
    when = 1791547200

    async def tautulli(cmd, timeout=0, **params):
        asked.append((cmd, params))
        answer = answers[cmd]
        if isinstance(answer, Exception):
            raise answer
        if answer == "slow":
            await asyncio.sleep(1)
        return answer
    services.tautulli.call = tautulli

    async def episode_files(path, **params):
        assert path == "episodefile" and params == {"seriesId": 12}
        return ([{"quality": {"quality": {"name": "WEBDL-1080p"}}, "mediaInfo": {"videoDynamicRangeType": ""}, "size": 1e9}] * 20
                + [{"quality": {"quality": {"name": "HDTV-720p"}}, "mediaInfo": {}, "size": 5e8}] * 2)
    services.sonarr.get = episode_files

    class Progress:
        async def _radarr_movies(self):
            return {45745: {"id": 3, "hasFile": True, "movieFile": {"quality": {"quality": {"name": "WEBDL-2160p"}},
                                                                     "mediaInfo": {"videoDynamicRangeType": "DV HDR10"}, "size": 27.7e9}},
                    10378: {"id": 4, "hasFile": False}}

        async def _sonarr_series(self):
            return {9999: {"id": 12}}

    at = datetime.fromtimestamp(when, timezone.utc).astimezone()
    day = f"{at:%b} {at.day}"

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        async with get_session() as s:
            s.add(PlexUser(plex_username="jordan", discord_id=7, plex_user_id=700))
        answers["get_history"] = {"data": [{"row_id": 77, "date": when, "player": "SHIELD", "product": "Plex for Android (TV)",
                                            "transcode_decision": "direct play", "media_type": "movie"}]}
        answers["get_stream_data"] = {"video_full_resolution": "4k", "video_dynamic_range": "HDR",
                                      "stream_video_full_resolution": "4k", "stream_video_dynamic_range": "HDR"}
        film = await ticket_facts.title_facts(services, Progress(), PAT, "movie", 45745, "501")
        no_file = await ticket_facts.title_facts(services, Progress(), PAT, "movie", 10378, None)
        answers["get_stream_data"] = {"video_full_resolution": "4k", "video_dynamic_range": "HDR", "video_decision": "transcode",
                                      "stream_video_full_resolution": "1080p", "stream_video_dynamic_range": "SDR"}
        answers["get_history"]["data"][0].update(transcode_decision="transcode", media_type="episode",
                                                 parent_media_index=2, media_index=10)
        show = await ticket_facts.title_facts(services, Progress(), JORDAN, "tv", 9999, "503")
        answers["get_history"] = {"data": []}
        never = await ticket_facts.title_facts(services, Progress(), JORDAN, "tv", 9999, "503")
        answers["get_history"] = ServiceError("Tautulli timed out")
        down = await ticket_facts.title_facts(services, Progress(), JORDAN, "movie", 45745, "501")
        answers["get_history"] = {"data": [{"row_id": 78, "date": when, "platform": "Roku", "transcode_decision": "copy",
                                            "full_title": "Big Buck Bunny"}]}
        answers["get_stream_data"] = ServiceError("no stream data")
        general = await ticket_facts.general_facts(services, JORDAN)
        answers["get_history"] = "slow"
        saved = ticket_facts.LOOK_SECONDS
        ticket_facts.LOOK_SECONDS = 0.05
        try:
            slow = await ticket_facts.title_facts(services, Progress(), JORDAN, "movie", 45745, "501")
        finally:
            ticket_facts.LOOK_SECONDS = saved
        return film, no_file, show, never, down, general, slow

    film, no_file, show, never, down, general, slow = asyncio.run(scenario())
    assert film == ["File: 4K WEB-DL, Dolby Vision + HDR10, 27.7 GB",
                    f"Last played {day} on SHIELD (Plex for Android (TV)), direct play, 4K HDR"]
    assert ("get_history", {"length": 1, "user_id": 88, "rating_key": "501"}) in asked
    assert ("get_stream_data", {"row_id": 77}) in asked
    assert no_file == ["File: Radarr has none for it"]
    # A show: its files summed up, the episode they played, and a play Plex converted.
    assert show == ["Files: 22 episodes, mostly 1080p WEB-DL (20 of 22), SDR, 21.0 GB",
                    f"Last played S02E10 {day} on SHIELD (Plex for Android (TV)), converted from 4K HDR to 1080p SDR"]
    assert ("get_history", {"length": 1, "user_id": 700, "grandparent_rating_key": "503"}) in asked, \
        "a Discord member is found in Tautulli by the Plex account linked to them"
    assert never[1] == "Tautulli has no play of it by them"
    assert down == ["File: 4K WEB-DL, Dolby Vision + HDR10, 27.7 GB"], "Tautulli down leaves its line out"
    assert general == [f"Last played Big Buck Bunny {day} on Roku, direct stream"]
    assert slow == ["File: 4K WEB-DL, Dolby Vision + HDR10, 27.7 GB"], "a slow Tautulli doesn't hold up the ticket"


def test_a_members_own_list_is_timed_by_what_they_can_see():
    from portal import help as helpdesk
    rec = {"subject": "general", "status": "open", "created_at": "2026-10-09T10:00:00+00:00", "reason": "Something else",
           "thread": [{"id": "a", "at": "2026-10-09T10:00:00+00:00", "by": "Jordan", "kind": "member", "text": "Hi"},
                      {"id": "b", "at": "2026-10-09T11:00:00+00:00", "by": "Sam", "kind": "note", "text": "Checking"},
                      {"id": "c", "at": "2026-10-09T12:00:00+00:00", "by": "Sam", "kind": "status", "text": "Took it"}]}
    row = helpdesk.member_row("abcdefabcdef", rec)
    assert row["updatedAt"] == "2026-10-09T10:00:00+00:00", "an admin's note or taking it isn't news to the member"
    assert [e["id"] for e in row["thread"]] == ["a"]


def test_a_device_name_can_t_pass_for_a_line_of_plexbie_sees():
    from portal.ticket_facts import play_text
    line = play_text({"date": 1791547200, "player": "SHIELD\nFile: Radarr has none for it", "product": "Plex\nfor TV",
                      "transcode_decision": "direct play"}, {})
    assert "\n" not in line and "SHIELD File: Radarr has none for it (Plex for TV)" in line


def test_quality_and_picture_names_read_plainly():
    from portal.ticket_facts import dynamic_range, quality_name, size_text
    assert quality_name("WEBDL-2160p") == "4K WEB-DL" and quality_name("Bluray-1080p Remux") == "1080p Blu-ray Remux"
    assert quality_name("Remux-2160p") == "4K Remux" and quality_name("SDTV") == "SDTV" and quality_name(None) == "Unknown quality"
    assert dynamic_range({"videoDynamicRangeType": "DV HDR10Plus"}) == "Dolby Vision + HDR10+"
    assert dynamic_range({"videoDynamicRangeType": "", "videoDynamicRange": "HDR"}) == "HDR"
    assert dynamic_range(None) == "SDR" and size_text(0) is None and size_text(512e6) == "512 MB"


# ------------------------------------------------------------------- /ticket
class _Response:
    def __init__(self):
        self.sent, self.modal, self.deferred = [], None, False

    def is_done(self):
        return self.deferred

    async def send_message(self, content, ephemeral=False, **kw):
        self.sent.append(content)

    async def defer(self, ephemeral=False, thinking=False):
        self.deferred = True

    async def send_modal(self, modal):
        self.modal = modal


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content, ephemeral=False, view=None):
        self.sent.append((content, view))


class _Auth:
    def __init__(self, member: bool):
        self.member, self.asked = member, []

    async def describe(self, s):
        self.asked.append(s)
        return {"user": {"id": s["id"], "name": s["name"], "via": "discord"}, "member": self.member, "admin": False,
                "discordId": s["id"], "plexName": "jordan", "plexAccountId": None}


def _pressed(actions, auth):
    user = type("U", (), {"id": 7, "name": "jordan", "display_name": "Jordan"})()
    client = type("Bot", (), {"portal_actions": actions, "portal_auth": auth})()
    return type("Interaction", (), {"user": user, "client": client, "response": _Response(), "followup": _Followup()})()


def test_slash_ticket_lists_whats_on_plex_with_general_first():
    from portal import ticket_view
    actions = _actions()

    async def scenario():
        return (await ticket_view.about_choices(_pressed(actions, _Auth(True)), "sint"),
                await ticket_view.about_choices(_pressed(actions, _Auth(True)), ""),
                await ticket_view.about_choices(_pressed(None, _Auth(True)), "sint"))

    typed, empty, no_site = asyncio.run(scenario())
    stranger = asyncio.run(ticket_view.about_choices(_pressed(actions, _Auth(False)), "sint"))
    assert [c.value for c in stranger] == ["general"], "what's on Plex is for its members"
    assert [(c.name, c.value) for c in typed] == [("General: not about one title", "general"), ("Sintel (2010) · Film", "movie:45745"),
                                                  ("Sintel's Home Movies · Film", "movie:plex:504")]
    assert len(empty) == 5 and empty[-1].value == "tv:9999"
    assert [c.value for c in no_site] == ["general"]


def test_slash_ticket_is_for_plex_members_and_asks_whats_wrong_before_it_sends():
    from portal import ticket_view
    actions = _actions()
    sent = []

    async def open_ticket(user, body):
        sent.append((user, body))
        return {"ok": True}

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        stranger = _pressed(actions, _Auth(False))
        await ticket_view.start(stranger, "movie:45745")
        typed = _pressed(actions, _Auth(True))
        await ticket_view.start(typed, "Sintel")                     # typed, not picked: the one title with that name
        unknown = _pressed(actions, _Auth(True))
        await ticket_view.start(unknown, "Nothing by that name")
        general = _pressed(actions, _Auth(True))
        await ticket_view.start(general, "general")
        offline = _pressed(None, _Auth(True))
        await ticket_view.start(offline, "general")
        # Pick a reason, then the box; sending it opens the ticket as the website would.
        view = typed.followup.sent[0][1]
        pick = _pressed(actions, _Auth(True))
        pick.data = {"values": ["picture"]}
        view.pick._values = ["picture"]
        await view.pick.callback(pick)
        modal = pick.response.modal
        modal.note._value = "Washed out"
        submit = _pressed(actions, _Auth(True))
        actions.open_ticket = open_ticket
        await modal.on_submit(submit)
        return stranger, typed, unknown, general, offline, view, modal, submit

    stranger, typed, unknown, general, offline, view, modal, submit = asyncio.run(scenario())
    assert stranger.followup.sent == [(ticket_view.NOT_MEMBER, None)]
    assert typed.followup.sent[0][0] == "What's wrong with **Sintel (2010)**?"
    assert [o.value for o in view.pick.options] == ["playback", "picture", "audio", "wrongfilm", "other"], "no missing episodes on a film"
    assert view.pick.options[1].description == "Washed out, wrong colours, or blurry"
    assert unknown.followup.sent == [(ticket_view.PICK_ONE, None)]
    assert general.followup.sent[0][0] == "What's it about?"
    assert [o.value for o in general.followup.sent[0][1].pick.options] == ["access", "other"]
    assert offline.response.sent == ["Tickets need the website running. Ask an admin."]
    assert modal.note.required is False
    person, body = sent[0]
    assert person["discordId"] == "7" and person["user"]["name"] == "Jordan"
    assert body == {"kind": "movie", "id": "45745", "reason": "picture", "note": "Washed out", "source": "discord"}
    assert submit.followup.sent == [(ticket_view.SENT, None)]
