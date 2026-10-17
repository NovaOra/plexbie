# path: tests/test_plexbie_tickets.py
"""Tickets Plexbie opens itself, on a member's request: an import that won't go through, a
search that found nothing, a download Seerr says failed.

The ticket is the member's (filed under them, on their list), but its first message is
Plexbie's words, not theirs: these pin that it's stored and shown as Plexbie's, to the
member and to the admins, and that tickets from before that was kept (Plexbie's words
filed as the member's, with the reason in front) read the same way.
"""
import asyncio
import pathlib
import tempfile

import conftest  # noqa: F401
from test_portal import _init

from portal import help as helpdesk
from portal.admin import _ticket_row

NOTE = "Sintel finished downloading, but Radarr won’t import it by itself. An admin will check it."


class _Bot:
    """No website running, no admin channel: open_help only files the ticket."""
    services = None

    def get_channel(self, cid):
        return None


def _db():
    asyncio.run(_init(pathlib.Path(tempfile.mkdtemp()) / "t.db"))


def _opened_by_plexbie(**kw) -> dict:
    from core import season_search
    from database.request_store import save_request

    async def body():
        await save_request(301, user_id=7, media={"id": 45745, "media_type": "movie", "title": "Sintel"},
                           extra={"requester_name": "Jordan"})
        return await season_search.open_help(_Bot(), 301, seasons=None, reason="blocked", note=NOTE,
                                             status_now="Downloaded, import blocked", **kw)
    return asyncio.run(body())


def test_a_ticket_plexbie_opens_is_the_members_with_plexbies_words_first():
    _db()
    h = _opened_by_plexbie()
    assert h["by_plexbie"] is True
    assert h["who"] == "Jordan" and h["discord_id"] == 7      # still theirs: on their list, answers reach them
    first = h["thread"][0]
    assert first["kind"] == "member" and first["by"] == "Plexbie" and first["plexbie"] is True
    assert first["text"] == NOTE                                # no reason in front: that's the ticket's heading
    assert helpdesk.owns_ticket(h, {"discordId": "7"})


def test_the_member_sees_it_as_plexbies_not_their_own():
    _db()
    h = _opened_by_plexbie()
    view = helpdesk.member_view(h)
    assert view["byPlexbie"] is True
    assert view["thread"][0]["by"] == "Plexbie" and view["thread"][0]["plexbie"] is True
    row = helpdesk.member_row(h["id"], h)
    assert row["byPlexbie"] is True and row["thread"][0]["by"] == "Plexbie"


def test_what_the_member_writes_after_it_is_still_theirs():
    _db()
    h = _opened_by_plexbie()

    async def answer():
        return await helpdesk.add(h["id"], "member", h["who"], "It's the extended cut, if that helps.")
    view = helpdesk.member_view(asyncio.run(answer()))
    assert [e["by"] for e in view["thread"]] == ["Plexbie", "You"]
    assert "plexbie" not in view["thread"][1]


def test_the_admins_see_plexbie_opened_it_for_the_member():
    _db()
    h = _opened_by_plexbie()
    row = _ticket_row(h["id"], h)
    assert row["byPlexbie"] is True and row["who"] == "Jordan" and row["openedBy"] is None
    assert row["last"]["by"] == "Plexbie"


def test_a_members_own_ticket_and_an_admins_are_not_plexbies():
    member = {"who": "Jordan", "reason": helpdesk.REASONS["stuck"], "status_then": "Downloading, 40%",
              "thread": [{"id": "a", "at": "2026-10-01T00:00:00+00:00", "by": "Jordan", "kind": "member",
                          "text": "Stuck downloading. It's been at 40% all day."}]}
    admin = {"who": "Jordan", "reason": "Opened by an admin", "opened_by": "Sam", "status_then": "Nothing found",
             "thread": [{"id": "b", "at": "2026-10-01T00:00:00+00:00", "by": "Sam", "kind": "note", "text": "Checking."}]}
    for h in (member, admin):
        assert helpdesk.by_plexbie(h) is False and _ticket_row("x", h)["byPlexbie"] is False
    assert helpdesk.member_view(member)["thread"][0]["by"] == "You"
    assert helpdesk.shown_thread(member) == member["thread"]


def test_one_stored_before_it_was_kept_reads_as_plexbies_too():
    """Plexbie's words filed as the member's, the reason in front, and no by_plexbie."""
    reason = helpdesk.PLEXBIE_REASONS["blocked"]
    old = {"who": "Jordan", "reason": reason, "status_then": "Downloaded, import blocked", "status": "open",
           "thread": [{"id": "a", "at": "2026-10-01T00:00:00+00:00", "by": "Jordan", "kind": "member",
                       "text": f"{reason}. {NOTE}"}]}
    assert helpdesk.by_plexbie(old)
    first = helpdesk.shown_thread(old)[0]
    assert first["by"] == "Plexbie" and first["plexbie"] is True and first["text"] == NOTE
    assert helpdesk.member_view(old)["thread"][0]["by"] == "Plexbie"
    assert _ticket_row("x", old)["byPlexbie"] is True
    assert old["thread"][0]["by"] == "Jordan"      # shown so, not rewritten


def test_older_ones_are_known_by_what_only_plexbie_puts_on_a_ticket():
    seen = {"Nothing found", "On Plex, as another film", "Adding to Plex", "Approved, not in Sonarr",
            "Approved, not in Radarr", "Failed in Seerr", "Downloaded, import blocked"}
    for saw in seen:
        assert helpdesk.by_plexbie({"reason": helpdesk.REASONS["stuck"], "status_then": saw}), saw
    assert helpdesk.by_plexbie({"reason": helpdesk.REASONS["notfound"], "offer": "name", "status_then": "Searching"})
    assert helpdesk.by_plexbie({"reason": helpdesk.PLEXBIE_REASONS["notreached"], "status_then": "Approved"})
    assert not helpdesk.by_plexbie({"reason": helpdesk.REASONS["notfound"], "status_then": "Searching, Nothing found yet"})


def test_a_download_seerr_says_failed_opens_one_as_plexbie():
    from types import SimpleNamespace

    from webhooks.seerr_handler import SeerrEvents
    _db()
    handler = SeerrEvents.__new__(SeerrEvents)
    handler.bot = SimpleNamespace()
    asyncio.run(handler._failed(302, {"user_id": 7, "requester_name": "Jordan",
                                      "media": {"title": "Sintel", "media_type": "movie"}}, "No space left."))
    h = asyncio.run(helpdesk.open_for({"302"}))["302"]
    assert h["by_plexbie"] is True and h["who"] == "Jordan"
    assert h["thread"][0]["by"] == "Plexbie" and h["thread"][0]["text"].startswith("Seerr reported the download failed.")
