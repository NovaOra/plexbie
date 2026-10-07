# path: tests/test_ticket_delivery.py
"""An admin's reply on a ticket says whether it reached the member.

Replying, solving with a last message and opening a ticket "and tell them" all
used to answer "has been told" whatever happened: a member with closed Discord
DMs, or with no alerts and no email, heard nothing and nobody knew. These tests
pin the replacement: a Discord DM first, a phone alert or email when the DM
doesn't arrive (never both when it does), and an honest answer with "told". When
it reached nobody, the ticket's timeline says so for whoever opens it next.
"""
import asyncio
import os
import pathlib
import tempfile

import conftest  # noqa: F401
from helpers import FakeServices
from test_app_push import TOKEN, _Expo
from test_portal import ADMIN, _Actions, _all_requests_data, _init

from core import admin_mirror, notify
from core.config import Config

SAM_ON_DISCORD = {"user": {"name": "Sam"}, "discordId": "7", "plexAccountId": "55", "plexName": "Sam"}
SAM_WITHOUT_DISCORD = {"user": {"name": "Sam"}, "discordId": None, "plexAccountId": "55", "plexName": "Sam"}
SAM_ONLY_ON_DISCORD = {"user": {"name": "Sam"}, "discordId": "7", "plexAccountId": None, "plexName": None}


def _bot(dms, *, closed):
    class Channel:
        async def send(self, content=None, embed=None, **kw):
            return None

    class Bot:
        def get_channel(self, cid):
            return Channel()

        def get_guild(self, gid):
            return None

        def get_user(self, uid):
            return None

        async def fetch_user(self, uid):
            class U:
                id, name, display_name = uid, "Sam", "Sam"

                async def send(self, content=None, embed=None, view=None):
                    if closed:
                        raise RuntimeError("Cannot send messages to this user")
                    dms.append(content or embed.description)
                    return type("Sent", (), {"id": 555, "channel": type("C", (), {"id": 444})()})()
            return U()
    return Bot()


def _run(scenario, *, closed=False, fallback="none", bot=True):
    """Runs scenario(actions) with Sam's DMs open or closed and notify_member answering
    `fallback` (None: the real one). Returns (its result, the DMs that arrived, the
    alerts/emails tried)."""
    dms, alerts = [], []
    actions = _Actions(FakeServices(Config()))
    actions.bot = _bot(dms, closed=closed) if bot else None
    actions.config.admin_channel_id = 9
    actions.data = _all_requests_data()

    async def record(services, **kw):
        alerts.append(kw)
        return fallback

    async def body():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "t.db")
        real = notify.notify_member
        if fallback is not None:
            notify.notify_member = record
        try:
            out = await scenario(actions)
            # A DM's app copy goes in the background: let it finish before counting.
            await asyncio.gather(*admin_mirror._app_tasks)
            return out
        finally:
            notify.notify_member = real
    return asyncio.run(body()), dms, alerts


async def _ticket(person=SAM_ON_DISCORD, **kw):
    from portal import help as helpdesk
    return await helpdesk.create(request_key="301", slot=301, title="Searching Forever", kind="movie", seasons=None,
                                 user=person, reason="Stuck downloading", note="0% all morning", status_now="Searching", **kw)


async def _thread(hid):
    from portal import help as helpdesk
    return [h for h in await helpdesk.all_help() if h["id"] == hid][0]["thread"]


# ------------------------------------------------------------- a reply
def test_a_reply_that_arrives_as_a_dm_says_so_and_sends_nothing_else():
    async def scenario(actions):
        h = await _ticket()
        return await actions.ticket_comment(ADMIN, h["id"], {"kind": "reply", "text": "Is it the 4K one?"})

    out, dms, alerts = _run(scenario)
    assert out == {"ok": True, "told": True, "message": "Sent to Sam (Discord DM)."}
    assert len(dms) == 1 and alerts == [], "a DM that arrived is never sent again another way"


def test_a_reply_to_closed_dms_falls_back_to_a_phone_alert():
    async def scenario(actions):
        h = await _ticket()
        return await actions.ticket_comment(ADMIN, h["id"], {"kind": "reply", "text": "Is it the 4K one?"})

    out, dms, alerts = _run(scenario, closed=True, fallback="push")
    assert out == {"ok": True, "told": True, "message": "Sent to Sam (phone alert)."}
    assert dms == [] and len(alerts) == 1
    sent = alerts[0]
    assert sent["title"] == "About your request: Searching Forever" and sent["url"] == "/schedule"
    assert sent["body"] == "Message from Pat: Is it the 4K one?" and sent["plex_account_id"] == "55"
    assert sent["discord_id"] == "7", "alerts turned on while signed in with Discord count too"


def test_a_reply_that_reaches_nobody_says_so_and_goes_on_the_ticket():
    async def scenario(actions):
        h = await _ticket()
        out = await actions.ticket_comment(ADMIN, h["id"], {"kind": "reply", "text": "Is it the 4K one?"})
        return out, await _thread(h["id"])

    (out, thread), dms, alerts = _run(scenario, closed=True, fallback="none")
    assert out["ok"] is True and out["told"] is False
    assert out["message"] == ("Added to the ticket, but it didn't reach Sam: Plexbie couldn't DM them on Discord, "
                              "and no phone alert or email reached them either. Tell them another way.")
    assert dms == [] and len(alerts) == 1, "tried once each way, nothing sent twice"
    assert [e["kind"] for e in thread] == ["member", "reply", "action"]
    assert thread[-1]["by"] == "Plexbie" and "didn't reach Sam" in thread[-1]["text"]


def test_a_reply_to_someone_without_discord_says_how_it_went():
    async def scenario(actions):
        h = await _ticket(SAM_WITHOUT_DISCORD)
        return await actions.ticket_comment(ADMIN, h["id"], {"kind": "reply", "text": "On it"})

    out, dms, alerts = _run(scenario, fallback="email")
    assert out == {"ok": True, "told": True, "message": "Sent to Sam (email)."}
    nobody, _, _ = _run(scenario, fallback="none")
    assert nobody["told"] is False and nobody["message"] == (
        "Added to the ticket, but it didn't reach Sam: no phone alert or email reached them. Tell them another way.")


# ------------------------------------------- the Plexbie app on their phone
def _with_app(scenario, *, closed, person, plex_account_id, discord_id):
    """Runs a reply with the real alerts: APP_PUSH on, Sam's phone registered with the
    app, Expo stood in for. Returns (the answer, the DMs that arrived, what Expo got)."""
    expo = _Expo(lambda m: {"status": "ok", "id": "t"})
    saved = os.environ.get("APP_PUSH")
    os.environ["APP_PUSH"] = "expo"

    async def with_phone(actions):
        await notify.register_app(TOKEN, "android", plex_account_id=plex_account_id, plex_name=None, discord_id=discord_id)
        return await scenario(actions, person)
    try:
        out, dms, _ = _run(with_phone, closed=closed, fallback=None)
    finally:
        expo.close()
        os.environ.pop("APP_PUSH", None)
        if saved is not None:
            os.environ["APP_PUSH"] = saved
    return out, dms, expo.sent


async def _reply(actions, person):
    h = await _ticket(person)
    return await actions.ticket_comment(ADMIN, h["id"], {"kind": "reply", "text": "Is it the 4K one?"})


def test_closed_dms_and_the_app_get_one_phone_alert_not_two():
    out, dms, pushed = _with_app(_reply, closed=True, person=SAM_ON_DISCORD, plex_account_id="55", discord_id="7")
    assert out == {"ok": True, "told": True, "message": "Sent to Sam (phone alert)."}
    assert dms == [] and len(pushed) == 1, "the DM's app copy and the fallback are one alert, not two"
    assert pushed[0]["title"] == "About your request: Searching Forever"


def test_alerts_turned_on_with_discord_alone_are_found():
    out, dms, pushed = _with_app(_reply, closed=True, person=SAM_ONLY_ON_DISCORD, plex_account_id=None, discord_id="7")
    assert out == {"ok": True, "told": True, "message": "Sent to Sam (phone alert)."}
    assert dms == [] and len(pushed) == 1


def test_a_dm_that_arrives_still_reaches_the_app_once():
    out, dms, pushed = _with_app(_reply, closed=False, person=SAM_ON_DISCORD, plex_account_id="55", discord_id="7")
    assert out == {"ok": True, "told": True, "message": "Sent to Sam (Discord DM)."}
    assert len(dms) == 1 and len(pushed) == 1, "the DM and its usual app copy, nothing more"


# ------------------------------------------------------------- solving
def test_solving_with_closed_dms_falls_back_to_email():
    async def scenario(actions):
        h = await _ticket()
        return await actions.help_resolve(ADMIN, h["id"], {"reply": "On Plex now"})

    out, dms, alerts = _run(scenario, closed=True, fallback="email")
    assert out == {"ok": True, "told": True, "message": "Resolved, and Sam has been told (email)."}
    assert dms == [] and len(alerts) == 1


def test_solving_with_a_dm_that_arrives():
    async def scenario(actions):
        h = await _ticket()
        return await actions.ticket_status(ADMIN, h["id"], {"status": "resolved", "message": "On Plex now"})

    out, dms, alerts = _run(scenario)
    assert out == {"ok": True, "told": True, "message": "Resolved, and Sam has been told (Discord DM)."}
    assert len(dms) == 1 and alerts == []


def test_solving_when_it_reaches_nobody_is_still_solved_and_says_so():
    async def scenario(actions):
        h = await _ticket()
        out = await actions.ticket_status(ADMIN, h["id"], {"status": "resolved", "message": "On Plex now"})
        from portal import help as helpdesk
        return out, [x for x in await helpdesk.all_help() if x["id"] == h["id"]][0]

    (out, h), dms, alerts = _run(scenario, closed=True, fallback="none")
    assert out["ok"] is True and out["told"] is False and h["status"] == "resolved"
    assert out["message"] == ("Resolved, but it didn't reach Sam: Plexbie couldn't DM them on Discord, "
                              "and no phone alert or email reached them either. Tell them another way.")
    assert [e["kind"] for e in h["thread"]] == ["member", "reply", "status", "action"]
    assert h["thread"][-1]["by"] == "Plexbie" and "didn't reach Sam" in h["thread"][-1]["text"]
    assert dms == [] and len(alerts) == 1


def test_without_discord_connected_it_still_tries_alerts_and_email():
    async def scenario(actions):
        h = await _ticket()
        return await actions.help_resolve(ADMIN, h["id"], {})

    out, _, alerts = _run(scenario, bot=False, fallback="push")
    assert out == {"ok": True, "told": True, "message": "Resolved, and Sam has been told (phone alert)."} and len(alerts) == 1


# ------------------------------------------- opening a ticket and telling them
def test_opening_a_ticket_and_telling_them_says_how_it_arrived():
    from database.request_store import mark_resolved, save_request

    async def scenario(actions):
        await save_request(201, user_id=7, media={"id": 1, "media_type": "movie", "title": "Searching Forever"})
        await mark_resolved(201, "approved", "Sam")
        return await actions.admin_ticket(ADMIN, "201", {"note": "Indexer was down", "tell": True})

    out, dms, alerts = _run(scenario)
    assert out["ok"] is True and out["told"] is True and len(dms) == 1 and alerts == []
    assert out["message"].startswith("Ticket opened, and ") and "has been told (Discord DM). It's on Manage → Tickets." in out["message"]


def test_opening_a_ticket_and_telling_them_when_it_reaches_nobody():
    from database.request_store import mark_resolved, save_request

    async def scenario(actions):
        await save_request(201, user_id=7, media={"id": 1, "media_type": "movie", "title": "Searching Forever"})
        await mark_resolved(201, "approved", "Sam")
        out = await actions.admin_ticket(ADMIN, "201", {"note": "Indexer was down", "tell": True})
        return out, await _thread(out["help"]["id"])

    (out, thread), dms, alerts = _run(scenario, closed=True, fallback="none")
    assert out["ok"] is True and out["told"] is False and out["help"]["id"]
    assert out["message"].startswith("Ticket opened, but it didn't reach ") and out["message"].endswith(
        "Tell them another way. It's on Manage → Tickets.")
    assert [e["kind"] for e in thread] == ["note", "reply", "action"] and thread[-1]["by"] == "Plexbie"
    assert dms == [] and len(alerts) == 1


# ----------------------------------------------- nothing meant to be told
def test_quiet_tickets_notes_and_waiting_have_no_told():
    from database.request_store import mark_resolved, save_request

    async def scenario(actions):
        await save_request(201, user_id=7, media={"id": 1, "media_type": "movie", "title": "Searching Forever"})
        await mark_resolved(201, "approved", "Sam")
        quiet = await actions.admin_ticket(ADMIN, "201", {"note": "Indexer was down"})
        note = await actions.ticket_comment(ADMIN, quiet["help"]["id"], {"kind": "note", "text": "Trying another"})
        waiting = await actions.ticket_status(ADMIN, quiet["help"]["id"], {"status": "waiting"})
        await actions.ticket_status(ADMIN, quiet["help"]["id"], {"status": "open"})
        solved = await actions.help_resolve(ADMIN, quiet["help"]["id"], {"reply": "Fixed"})
        return quiet, note, waiting, solved, await _thread(quiet["help"]["id"])

    (quiet, note, waiting, solved, thread), dms, alerts = _run(scenario, closed=True, fallback="none")
    assert all("told" not in out for out in (quiet, note, waiting, solved))
    assert solved == {"ok": True, "message": "Resolved."}
    assert dms == [] and alerts == [] and "action" not in [e["kind"] for e in thread]
