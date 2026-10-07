# path: tests/test_request_decisions.py
"""One decision per request, wherever it is made.

The Discord Approve/Decline buttons, the website and Seerr all decide the same
stored request. The buttons used to skip the per-request lock and never looked
at the stored status, so a click during (or after) a website decision ran the
approval a second time, or turned an approved request into a declined one.

Also: a book approval that sent nothing to SABnzbd was recorded as approved and
closed, so it could never be retried; a failed edit of the admin card skipped
recording the decision, so the next click submitted the request again; and the
Seerr arrival check ran as a task nothing held on to.

The film/TV and book cards decline through one shared body (each keeping the
button ids already-posted cards carry), and both season follow-ups build the
same "Can't be found" callback, so a fix made to one reaches the other.

The website's own approve and decline are pinned end to end too: a request made
in Seerr is decided there without waiting on the lock it already holds, a refusal
or a failed approval leaves the request open with nobody told, and a show with
no TheTVDB entry is approved for a hand download instead of going to Seerr.
"""
import asyncio
import logging
import pathlib
import tempfile
from types import SimpleNamespace

import conftest  # noqa: F401

import discord

MID = 4242


# ------------------------------------------------------------------ fakes

class _Message:
    def __init__(self, fail_edit: bool = False):
        self.id = MID
        self.embeds = [discord.Embed(title="Request")]
        self.edits = []
        self.fail_edit = fail_edit

    async def edit(self, **changes):
        if self.fail_edit:
            raise RuntimeError("card gone")
        self.edits.append(changes)


class _Response:
    def __init__(self, message):
        self.message = message
        self._done = False
        self.sent = []

    def is_done(self):
        return self._done

    async def defer(self, **_):
        self._done = True

    async def edit_message(self, **changes):
        self._done = True
        await self.message.edit(**changes)

    async def send_message(self, content, ephemeral=False):
        self._done = True
        self.sent.append(content)


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content, ephemeral=False):
        self.sent.append(content)


class _User:
    mention = "<@7>"

    def __str__(self):
        return "Discord admin"


class _Interaction:
    def __init__(self, bot, message):
        self.client = bot
        self.message = message
        self.user = _User()
        self.response = _Response(message)
        self.followup = _Followup()

    @property
    def said(self):
        return self.response.sent + self.followup.sent


class _Channel:
    def __init__(self, card):
        self.card = card

    async def fetch_message(self, _):
        return self.card


class _World:
    """A database, a bot, and the outside world (downloads, Seerr, DMs) as counters."""

    def __init__(self, card=None, download=lambda fmt: True, seerr=None):
        from plugins.media_requests import cog

        self.cog = cog
        self.card = card or _Message()
        self.services = SimpleNamespace(config=SimpleNamespace(admin_channel_id=1), seerr=seerr,
                                        sonarr=SimpleNamespace(configured=False),
                                        radarr=SimpleNamespace(configured=False))
        self.bot = SimpleNamespace(services=self.services)
        self.downloads, self.fulfils, self.dms = [], [], []
        self.gate = None
        world = self

        async def submit(view):
            world.downloads.append(view.book.get("request_format"))
            return download(view.book.get("request_format"))

        async def fulfil(view):
            world.fulfils.append(view._title())
            if world.gate is not None:
                await world.gate.wait()
            return {"success": True, "followup_message": "Request approved and submitted to Seerr!",
                    "admin_note": "", "user_message": "on its way"}

        async def track(view):
            return None

        async def dm(bot, services, user_id, **kw):
            world.dms.append(kw.get("content"))
            return None

        self._patches = [
            (cog.BookAdminApprovalView, "_submit_to_download", submit),
            (cog.AdminApprovalView, "_fulfill_request", fulfil),
            (cog.AdminApprovalView, "_register_with_tracking", track),
            (cog, "dm_user_id", dm),
            (cog, "require_admin_channel", lambda bot, config: _Channel(world.card)),
        ]

    def run(self, scenario):
        saved = [(owner, name, getattr(owner, name)) for owner, name, _ in self._patches]
        for owner, name, value in self._patches:
            setattr(owner, name, value)
        try:
            return asyncio.run(self._in_db(scenario))
        finally:
            for owner, name, value in saved:
                setattr(owner, name, value)

    async def _in_db(self, scenario):
        import database.session as session_module
        session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
        if session_module.engine is not None:
            await session_module.engine.dispose()
        await session_module.init_database(f"sqlite:///{pathlib.Path(tempfile.mkdtemp()) / 'd.db'}")
        try:
            return await scenario()
        finally:
            await session_module.engine.dispose()

    async def save_film(self):
        from database.request_store import save_request
        await save_request(MID, user_id=1, media={"id": 603, "media_type": "movie", "title": "The Film"})

    async def save_book(self, fmt="ebook"):
        from database.request_store import save_request
        await save_request(MID, user_id=1, media={"title": "The Book", "author": "A. Writer", "request_format": fmt,
                                                  "open_library_key": "/works/OL1W"}, media_type=fmt)

    async def save_seerr_show(self):
        """A show requested in Seerr itself, mirrored here as Seerr's request 41."""
        from database.request_store import save_request
        await save_request(MID, user_id=1, media={"id": 456, "media_type": "tv", "name": "The Show"}, seasons=[2],
                           media_type="tv", extra={"source": "seerr", "overseerr_request_id": 41})

    async def press(self, view_type, custom_id, message=None):
        """Click a button the way discord.py does: a failure goes to the view's on_error."""
        view = view_type()
        item = next(c for c in view.children if getattr(c, "custom_id", None) == custom_id)
        interaction = _Interaction(self.bot, message or self.card)
        try:
            await item.callback(interaction)
        except Exception as e:
            await view.on_error(interaction, e, item)
        return interaction

    async def website(self, approve):
        return await self.cog.decide_request(self.bot, self.services, MID, approve, "web admin")

    @staticmethod
    async def status():
        from database.request_store import get_request
        return (await get_request(MID))["status"]


def _buttons_gone(message):
    return bool(message.edits) and message.edits[-1].get("view", "kept") is None


# ------------------------------------------- a decision already made elsewhere

def test_discord_approve_after_a_website_decline_does_nothing():
    w = _World()

    async def scenario():
        await w.save_film()
        await w.website(False)
        clicked = await w.press(w.cog.AdminApprovalView, "approve_request", _Message())
        return clicked, await w.status()

    clicked, status = w.run(scenario)
    assert w.fulfils == [], "the film was sent to Seerr after it had been declined"
    assert status == "declined"
    assert "Already declined." in clicked.said


def test_discord_decline_after_a_website_approval_keeps_it_approved():
    w = _World()

    async def scenario():
        await w.save_film()
        await w.website(True)
        card = _Message()
        clicked = await w.press(w.cog.AdminApprovalView, "decline_request", card)
        return clicked, card, await w.status()

    clicked, card, status = w.run(scenario)
    assert status == "approved", "a late Decline flipped an approved request"
    assert len(w.dms) == 1, f"the requester was told twice: {w.dms}"
    assert "Already approved." in clicked.said
    assert _buttons_gone(card), "the stale buttons stay on the card"


def test_discord_book_approve_after_a_website_decline_downloads_nothing():
    w = _World()

    async def scenario():
        await w.save_book()
        await w.website(False)
        card = _Message()
        clicked = await w.press(w.cog.BookAdminApprovalView, "approve_book_request", card)
        return clicked, card, await w.status()

    clicked, card, status = w.run(scenario)
    assert w.downloads == [], "an NZB was queued for a declined book"
    assert status == "declined"
    assert "Already declined." in clicked.said
    assert _buttons_gone(card)


def test_discord_book_decline_after_a_website_approval_keeps_it_approved():
    w = _World()

    async def scenario():
        await w.save_book()
        await w.website(True)
        clicked = await w.press(w.cog.BookAdminApprovalView, "decline_book_request", _Message())
        return clicked, await w.status()

    clicked, status = w.run(scenario)
    assert status == "approved"
    assert len(w.dms) == 1, f"the requester was told twice: {w.dms}"
    assert "Already approved." in clicked.said


def test_a_click_during_a_website_approval_waits_for_it_and_does_not_act_again():
    w = _World()

    async def scenario():
        await w.save_film()
        w.gate = asyncio.Event()
        web = asyncio.create_task(w.website(True))
        while not w.fulfils:
            await asyncio.sleep(0)
        click = asyncio.create_task(w.press(w.cog.AdminApprovalView, "approve_request", _Message()))
        for _ in range(20):
            await asyncio.sleep(0)
        w.gate.set()
        result, clicked = await web, await click
        return result, clicked, await w.status()

    result, clicked, status = w.run(scenario)
    assert result["ok"] is True
    assert w.fulfils == ["The Film"], f"submitted to Seerr {len(w.fulfils)} times"
    assert len(w.dms) == 1
    assert status == "approved"
    assert "Already approved." in clicked.said
    assert w.cog._DECISION_LOCKS == {}, "a finished decision leaves its lock behind"


# ------------------------------------------- a book approval that sent nothing

def test_a_book_with_nothing_found_stays_open_from_discord():
    w = _World(download=lambda fmt: False)

    async def scenario():
        await w.save_book()
        card = _Message()
        clicked = await w.press(w.cog.BookAdminApprovalView, "approve_book_request", card)
        return clicked, card, await w.status()

    clicked, card, status = w.run(scenario)
    assert status == "pending", "nothing was sent, yet the request was recorded as approved"
    assert w.dms == [], "the requester was told it was approved"
    view = card.edits[-1].get("view")
    assert view is not None and not any(c.disabled for c in view.children), "the admin can't try again"
    assert any("Nothing found on the indexers" in s for s in clicked.said), clicked.said


def test_a_book_with_nothing_found_stays_open_from_the_website():
    w = _World(download=lambda fmt: False)

    async def scenario():
        await w.save_book()
        return await w.website(True), await w.status()

    result, status = w.run(scenario)
    assert result["ok"] is False and "Nothing found on the indexers" in result["message"]
    assert status == "pending"
    assert w.card.edits == [], "the card was closed"
    assert w.dms == []


def test_a_book_wanted_in_both_formats_with_one_found_says_so():
    w = _World(download=lambda fmt: fmt == "ebook")

    async def scenario():
        await w.save_book("both")
        clicked = await w.press(w.cog.BookAdminApprovalView, "approve_book_request", _Message())
        return clicked, await w.status()

    clicked, status = w.run(scenario)
    assert status == "approved"
    assert any("some formats not available" in s for s in clicked.said), clicked.said


# ------------------------------------------- a card that can't be edited

def test_a_failed_card_edit_after_a_discord_approval_still_records_it():
    w = _World()

    async def scenario():
        await w.save_film()
        await w.press(w.cog.AdminApprovalView, "approve_request", _Message(fail_edit=True))
        return await w.status()

    assert w.run(scenario) == "approved", "the next click would submit it to Seerr again"
    assert w.fulfils == ["The Film"] and len(w.dms) == 1


def test_a_failed_card_edit_after_a_discord_book_approval_still_records_it():
    w = _World()

    async def scenario():
        await w.save_book()
        card = _Message()
        view = w.cog.BookAdminApprovalView()
        item = next(c for c in view.children if c.custom_id == "approve_book_request")
        interaction = _Interaction(w.bot, card)

        async def respond_then_fail(**changes):      # the first edit lands, the closing one doesn't
            interaction.response._done = True
            card.fail_edit = True
        interaction.response.edit_message = respond_then_fail
        try:
            await item.callback(interaction)
        except Exception as e:
            await view.on_error(interaction, e, item)
        return await w.status()

    assert w.run(scenario) == "approved", "the next click would queue a second download"
    assert w.downloads == ["ebook"] and len(w.dms) == 1


def test_a_failed_card_edit_after_a_website_approval_still_records_it():
    w = _World(card=_Message(fail_edit=True))

    async def scenario():
        await w.save_film()
        return await w.website(True), await w.status()

    result, status = w.run(scenario)
    assert result["ok"] is True
    assert status == "approved"
    assert len(w.dms) == 1


# ------------------------------------------- Seerr's arrival check

def test_the_arrival_check_after_media_available_is_held_and_its_failure_logged():
    from webhooks import seerr_handler as module

    release = asyncio.Event()

    class Arrivals:
        async def check_recently_added(self):
            await release.wait()
            raise RuntimeError("plex unreachable")

    bot = SimpleNamespace(get_cog=lambda name: Arrivals() if name == "NewMediaAddedCog" else None)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger(module.__name__)
    level = log.level
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    async def scenario():
        await module.SeerrEvents(bot).dispatch("MEDIA_AVAILABLE", {"notification_type": "MEDIA_AVAILABLE"})
        held = [t for v in vars(module).values() if isinstance(v, set) for t in v if isinstance(t, asyncio.Task)]
        release.set()
        for _ in range(5):
            await asyncio.sleep(0)
        still = [t for v in vars(module).values() if isinstance(v, set) for t in v if isinstance(t, asyncio.Task)]
        return held, still

    try:
        held, still = asyncio.run(scenario())
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    assert len(held) == 1, "nothing holds the running arrival check"
    assert still == [], "a finished check is never let go"
    assert any("plex unreachable" in r.getMessage() for r in records), "the failure went unreported"


# ------------------------------------------- one Decline for both cards

def _decline(save, view_name, custom_id):
    """Decline a pending request from its Discord card, noting each pass through the shared body."""
    w = _World()
    base, calls = w.cog._RequestApprovalBase, []
    shared = getattr(base, "_decline", None)

    async def spy(self, interaction, what):
        calls.append(what)
        await shared(self, interaction, what)
    if shared is not None:
        w._patches.append((base, "_decline", spy))

    async def scenario():
        await getattr(w, save)()
        card = _Message()
        clicked = await w.press(getattr(w.cog, view_name), custom_id, card)
        return clicked, card, await w.status()

    clicked, card, status = w.run(scenario)
    return w, calls, clicked, card, status


def test_a_discord_decline_closes_either_card_and_tells_the_requester_once():
    for save, view_name, custom_id, said in (
            ("save_film", "AdminApprovalView", "decline_request", "your request for **The Film** was declined"),
            ("save_book", "BookAdminApprovalView", "decline_book_request", "your book request for **The Book** was declined")):
        w, _, clicked, card, status = _decline(save, view_name, custom_id)
        assert status == "declined", view_name
        assert _buttons_gone(card), view_name
        assert any(f.name == "❌ Declined by" for f in card.edits[-1]["embed"].fields), view_name
        assert len(w.dms) == 1 and said in w.dms[0], w.dms
        assert clicked.said == [], clicked.said


def test_both_cards_decline_through_one_shared_body():
    from plugins.media_requests import cog
    assert hasattr(cog._RequestApprovalBase, "_decline"), \
        "the film/TV and book cards each carry their own copy of the Decline body"
    for save, view_name, custom_id, what in (("save_film", "AdminApprovalView", "decline_request", "media"),
                                             ("save_book", "BookAdminApprovalView", "decline_book_request", "book")):
        _, calls, _, _, status = _decline(save, view_name, custom_id)
        assert calls == [what], (view_name, calls)
        assert status == "declined"


def test_the_website_and_discord_name_a_decided_book_the_same_way():
    import inspect
    from plugins.media_requests import cog

    # One rule for a book's title, so the requester hears the same name
    # whichever side made the decision.
    assert "book.get('title', 'Unknown')" not in inspect.getsource(cog.decide_request)
    assert inspect.getsource(cog.BookAdminApprovalView).count("book.get('title', 'Unknown')") == 1, \
        "the book card spells out its title rule outside _title()"


def test_the_card_buttons_keep_the_ids_already_posted_cards_carry():
    from plugins.media_requests import cog

    async def ids():
        return ({c.custom_id for c in cog.AdminApprovalView().children},
                {c.custom_id for c in cog.BookAdminApprovalView().children})

    film, book = asyncio.run(ids())
    assert film == {"approve_request", "decline_request"}, film
    assert book == {"approve_book_request", "decline_book_request"}, book


# ------------------------------------------- seasons a search found nothing for

def test_a_season_search_that_finds_nothing_opens_a_help_request_from_either_follow_up():
    import inspect
    from core import season_search
    from plugins.media_requests import cog

    assert inspect.getsource(cog.AdminApprovalView).count("open_not_found_help(") == 1, \
        "the not-found callback is built separately for each follow-up"

    opened, handed = [], {}

    async def open_help(bot, key, seasons):
        opened.append((bot, key, seasons))

    def start(services, series_id, missing, title, on_nothing):
        handed["already in Sonarr"] = on_nothing

    def follow_up_new_show(services, tmdb_id, seasons, title, on_nothing, on_missing):
        handed["new to Sonarr"] = on_nothing

    class Sonarr:
        async def get(self, path):
            return {"seasons": [{"seasonNumber": 2, "monitored": True}]}

    async def submitted():
        return True

    async def nothing_to_restore():
        return None

    bot = SimpleNamespace()
    patches = [(season_search, "open_not_found_help", open_help), (season_search, "start", start),
               (season_search, "follow_up_new_show", follow_up_new_show)]
    saved = [(owner, name, getattr(owner, name)) for owner, name, _ in patches]
    for owner, name, value in patches:
        setattr(owner, name, value)

    async def scenario():
        view = cog.AdminApprovalView({"id": 456, "media_type": "tv", "name": "The Show"}, 1,
                                     SimpleNamespace(sonarr=Sonarr()), seasons=[2])
        view._bot, view._message_id = bot, MID
        view._submit_to_seerr = submitted
        view._restore_existing_request_monitoring = nothing_to_restore
        aired = [{"seasonNumber": 2, "hasFile": False, "airDateUtc": "2000-01-01T00:00:00Z"}]
        await view._monitor_and_search_seasons({"id": 7, "seasons": []}, [2], aired)
        await view._fulfill_request()
        for on_nothing in handed.values():
            await on_nothing([2])
        unsaved = cog.AdminApprovalView({"id": 456, "media_type": "tv", "name": "The Show"}, 1,
                                        SimpleNamespace(sonarr=Sonarr()), seasons=[2])
        handed.clear()
        await unsaved._monitor_and_search_seasons({"id": 7, "seasons": []}, [2], aired)
        await handed["already in Sonarr"]([2])     # no bot to open it with: nothing happens

    try:
        asyncio.run(scenario())
    finally:
        for owner, name, value in saved:
            setattr(owner, name, value)
    assert opened == [(bot, MID, [2]), (bot, MID, [2])], opened


# ------------------------------------------- the website's own decisions

class _Seerr:
    """Seerr as a decision sees it: the paths posted to, the show details it gives
    (an exception: it can't be asked) and, with refuse, an error for every post."""
    configured = True

    def __init__(self, show=None, refuse=None):
        self.show, self.refuse, self.posts = show, refuse, []

    async def get(self, path, **params):
        if isinstance(self.show, Exception):
            raise self.show
        return self.show

    async def post(self, path, body, raw=False):
        self.posts.append(path)
        if self.refuse is not None:
            raise self.refuse
        return (201, '{"id": 9}') if raw else {}


def test_a_website_decision_on_a_seerr_request_is_made_in_seerr_and_recorded_once():
    from database.request_store import get_request
    for approve, path, said in ((True, "request/41/approve", "Good news"), (False, "request/41/decline", "Sorry")):
        seerr = _Seerr()
        w = _World(seerr=seerr)

        async def scenario():
            await w.save_seerr_show()
            # It holds the request's lock already: waiting on it again would hang forever.
            result = await asyncio.wait_for(w.website(approve), 5)
            return result, await get_request(MID)

        result, record = w.run(scenario)
        assert result == {"ok": True, "message": f"{'Approved' if approve else 'Declined'} The Show in Seerr."}, result
        assert seerr.posts == [path], seerr.posts
        assert record["status"] == ("approved" if approve else "declined")
        assert record["resolved_by"] == "web admin"
        assert len(w.dms) == 1 and said in w.dms[0], w.dms
        assert w.fulfils == [], "a request made in Seerr was submitted to Seerr a second time"
        assert w.cog._DECISION_LOCKS == {}


def test_a_seerr_request_that_seerr_refuses_stays_pending():
    from core.clients import ServiceError
    w = _World(seerr=_Seerr(refuse=ServiceError("Seerr answered HTTP 500", 500)))

    async def scenario():
        await w.save_seerr_show()
        return await w.website(True), await w.status()

    result, status = w.run(scenario)
    assert result["ok"] is False and "Seerr didn't take that" in result["message"], result
    assert status == "pending"
    assert w.dms == [], "the requester heard about a decision Seerr never took"


def test_a_failed_approval_from_the_website_stays_pending_and_tells_nobody():
    w = _World()

    async def refused(view):
        w.fulfils.append(view._title())
        return {"success": False, "mode": "failed",
                "followup_message": "Failed to submit to Seerr or restore monitoring. Please try manually."}
    w._patches.append((w.cog.AdminApprovalView, "_fulfill_request", refused))

    async def scenario():
        await w.save_film()
        return await w.website(True), await w.status()

    result, status = w.run(scenario)
    assert result == {"ok": False, "message": "Failed to submit to Seerr or restore monitoring. Please try manually."}
    assert status == "pending", "a failed approval was recorded, so it can't be tried again"
    assert w.dms == [], "the requester was given good news for a failed approval"
    assert w.card.edits == [], "the card was closed"


def test_a_website_decline_records_who_and_tells_the_requester():
    from database.request_store import get_request
    w = _World()

    async def scenario():
        await w.save_film()
        return await w.website(False), await get_request(MID)

    result, record = w.run(scenario)
    assert result == {"ok": True, "message": "Declined The Film."}
    assert record["status"] == "declined" and record["resolved_by"] == "web admin"
    assert w.fulfils == []
    assert w.dms == ["❌ Sorry, your request for **The Film** was declined."], w.dms
    assert _buttons_gone(w.card)
    assert any(f.name == "❌ Declined by" and "(on the website)" in f.value for f in w.card.edits[-1]["embed"].fields)


def test_an_approve_and_a_decline_at_once_give_one_decision():
    for first in (True, False):
        w = _World()

        async def scenario():
            await w.save_film()
            results = await asyncio.gather(w.website(first), w.website(not first))
            return results, await w.status()

        results, status = w.run(scenario)
        won = [r for r in results if r["ok"]]
        assert len(won) == 1, results
        assert status == ("approved" if first else "declined")
        assert [r["message"] for r in results if not r["ok"]] == [f"Already {status}."], results
        assert w.fulfils == (["The Film"] if first else []), w.fulfils
        assert len(w.dms) == 1, f"the requester was told {len(w.dms)} times: {w.dms}"


def test_a_decline_made_in_seerr_is_recorded_once():
    from database.request_store import get_request
    from webhooks import seerr_handler as module
    seerr = _Seerr()
    w = _World(seerr=seerr)
    event = {"notification_type": "MEDIA_DECLINED", "subject": "The Show (2020)", "message": "",
             "media": {"media_type": "tv", "tmdbId": "456", "status": "PENDING"},
             "request": {"request_id": "41", "requestedBy_username": "Robin"}, "extra": []}

    async def scenario():
        await w.save_seerr_show()
        events = module.SeerrEvents(w.bot)
        await events.dispatch("MEDIA_DECLINED", event)
        await events.dispatch("MEDIA_DECLINED", event)     # Seerr sends it again: nothing more happens
        return await get_request(MID)

    record = w.run(scenario)
    assert record["status"] == "declined" and record["resolved_by"] == "Seerr"
    assert w.dms == ["❌ Sorry, your request for **The Show** was declined."], w.dms
    assert seerr.posts == [], "Seerr's own decision was sent back to it"


# ------------------------------------------- a show with no TheTVDB entry

def _approve_show(tmdb_id, seerr):
    """Approve a saved show through the real _fulfill_request, against a Seerr stub.
    Returns the result, the stored request and the shows followed up in Sonarr."""
    from core import season_search
    from database.request_store import get_request, save_request
    w = _World(seerr=seerr)
    w._patches = [p for p in w._patches if p[1] != "_fulfill_request"]
    followed = []
    w._patches.append((season_search, "follow_up_new_show", lambda services, **kw: followed.append(kw["tmdb_id"])))

    async def scenario():
        await save_request(MID, user_id=1, media={"id": tmdb_id, "media_type": "tv", "name": "Faraway Downs"},
                           seasons="all", media_type="tv")
        view = w.cog.AdminApprovalView()
        assert await view._load_from_saved(MID, w.bot)
        return await view._fulfill_request(), await get_request(MID)

    return (*w.run(scenario), followed)


def test_a_show_with_no_tvdb_entry_is_approved_for_a_hand_download_not_sent_to_seerr():
    from core.clients import ServiceError
    from plugins.media_requests.cog import NO_TVDB_NOTE
    seerr = _Seerr(show={"name": "Faraway Downs", "externalIds": {"tvdbId": None}},
                   refuse=ServiceError("Seerr would drop this request", 500))
    result, record, followed = _approve_show(204999, seerr)
    assert seerr.posts == [], "sent to Seerr, which quietly deletes a request Sonarr can't take"
    assert result["success"] is True and result["mode"] == "manual", result
    assert result["admin_note"] == NO_TVDB_NOTE
    assert "by hand" in result["user_message"]
    assert record["no_tvdb"] is True, "Manage → All requests can't flag it for a hand download"
    assert followed == [], "Sonarr was asked to search for a show it can't have"


def test_a_show_seerr_can_pass_on_or_cant_check_is_sent_to_seerr_once():
    for tmdb_id, show in ((1396, {"name": "Breaking Bad", "externalIds": {"tvdbId": 81189}}),
                          (1397, RuntimeError("Seerr unreachable"))):
        seerr = _Seerr(show=show)
        result, record, followed = _approve_show(tmdb_id, seerr)
        assert seerr.posts == ["request"], (tmdb_id, seerr.posts)
        assert result["success"] is True and result["mode"] == "seerr", result
        assert not record.get("no_tvdb"), tmdb_id
        assert record["overseerr_request_id"] == 9, "Seerr's number for it wasn't kept"
        assert followed == [tmdb_id], followed
