# path: tests/test_request_flow.py
"""The member's side of /request: picking seasons and confirming.

The confirmation said "Request submitted!" before anything was posted, so a
wrong or unwritable admin channel, or a failed save, dropped the request while
the member waited for an approval that could never come. A save that failed
after the card went up left a card whose buttons found no request.

The season picker put every season plus "All Seasons" in one select, which
Discord caps at 25 options, and took max() of the regular seasons without
checking there were any. Either way the member was left on "Fetching season
information..." with no error.

The film/TV and book search boxes submit through one body, and a request's
seasons are worded by one helper from the menu to the admin card.

A request from Discord skipped the website's checks: no per-member limit, no
"you already asked for this one", and a title blocked in Seerr or already on
Plex went to the admins anyway. Members also had no way to list their own
requests in Discord, only on the website.

Without a TMDB key, film and TV search offered a made-up film (TMDB number 1)
instead of saying search needs a key. /requests linked a request made in Seerr
to a message that doesn't exist, and counted requests from before a fixed date
as untracked on every install.
"""
import asyncio
import pathlib
import tempfile
from types import SimpleNamespace

import conftest  # noqa: F401

import discord


# ------------------------------------------------------------------ fakes

class _Response:
    def __init__(self, screen):
        self.screen = screen
        self._done = False

    def is_done(self):
        return self._done

    async def edit_message(self, **changes):
        self._done = True
        await self.screen.show(changes)

    async def send_message(self, content, ephemeral=False):
        self._done = True
        self.screen.notes.append(content)

    async def defer(self, ephemeral=False, thinking=False):
        self._done = True

    async def send_modal(self, modal):
        self._done = True
        self.screen.modal = modal


class _Followup:
    def __init__(self, screen):
        self.screen = screen

    async def send(self, content=None, ephemeral=False, embed=None):
        if content is not None:
            self.screen.notes.append(content)
        if embed is not None:
            self.screen.embed = embed


class _Screen:
    """The member's ephemeral message, which Discord refuses to show an invalid view on."""

    def __init__(self):
        self.contents, self.notes = [], []
        self.view = self.embed = self.modal = None

    async def show(self, changes):
        view = changes.get("view")
        for item in getattr(view, "children", []):
            if isinstance(item, discord.ui.Select) and (len(item.options) > 25 or item.max_values > 25):
                raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"),
                                            "Invalid Form Body: options must be 25 or fewer")
        if "content" in changes:
            self.contents.append(changes["content"])
        if "view" in changes:
            self.view = view
        if "embed" in changes:
            self.embed = changes["embed"]

    @property
    def last(self):
        return self.contents[-1] if self.contents else ""


class _Interaction:
    def __init__(self, bot=None, user_id=7):
        self.client = bot
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>", display_name="Sam")
        self.screen = _Screen()
        self.response = _Response(self.screen)
        self.followup = _Followup(self.screen)

    async def edit_original_response(self, **changes):
        await self.screen.show(changes)


class _Card:
    def __init__(self, mid, embed=None):
        self.id = mid
        self.embed = embed
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _Channel:
    def __init__(self, fail_send=False):
        self.cards = []
        self.fail_send = fail_send

    async def send(self, embed=None, **_):
        if self.fail_send:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        card = _Card(5000 + len(self.cards), embed)
        self.cards.append(card)
        return card


def _run(scenario, channel, *, fail_save=False):
    """Run a scenario with a fresh database, `channel` as the admin channel and no phone alerts."""
    from plugins.media_requests import cog

    async def broken_save(*_, **__):
        raise RuntimeError("database is locked")

    patches = [(cog, "require_admin_channel", lambda bot, config: channel),
               (cog, "notify", SimpleNamespace(alert_admins_soon=lambda *a, **k: None))]
    if fail_save:
        patches.append((cog, "save_request", broken_save))
    saved = [(owner, name, getattr(owner, name)) for owner, name, _ in patches]
    for owner, name, value in patches:
        setattr(owner, name, value)

    async def in_db():
        import database.session as session_module
        session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
        if session_module.engine is not None:
            await session_module.engine.dispose()
        await session_module.init_database(f"sqlite:///{pathlib.Path(tempfile.mkdtemp()) / 'd.db'}")
        try:
            return await scenario()
        finally:
            await session_module.engine.dispose()

    try:
        return asyncio.run(in_db())
    finally:
        for owner, name, value in saved:
            setattr(owner, name, value)


_SERVICES = SimpleNamespace(config=SimpleNamespace(admin_channel_id=1))
_FILM = {"id": 603, "media_type": "movie", "title": "The Film"}


async def _confirm_film():
    from plugins.media_requests.cog import ConfirmationView
    view = ConfirmationView(dict(_FILM), 7, _SERVICES)
    interaction = _Interaction(bot=SimpleNamespace(services=_SERVICES))
    await view.confirm.callback(interaction)
    return interaction.screen


# ------------------------------------------------------------- confirming

def test_a_request_is_called_submitted_only_once_the_admins_have_it():
    channel = _Channel()

    async def scenario():
        from database.request_store import get_request
        screen = await _confirm_film()
        return screen, await get_request(channel.cards[0].id) if channel.cards else None

    screen, record = _run(scenario, channel)
    assert len(channel.cards) == 1 and record is not None
    assert "Sending" in screen.contents[0], screen.contents
    assert "Request submitted" in screen.last


def test_an_unwritable_admin_channel_tells_the_member_it_failed():
    channel = _Channel(fail_send=True)
    screen = _run(_confirm_film, channel)
    assert not any("submitted" in c for c in screen.contents), f"told it was submitted: {screen.contents}"
    assert "Couldn't send your request" in screen.last


def test_a_failed_save_takes_the_card_down_and_says_so():
    channel = _Channel()
    screen = _run(_confirm_film, channel, fail_save=True)
    assert channel.cards and channel.cards[0].deleted, "a card with no stored request was left for the admins"
    assert not any("submitted" in c for c in screen.contents), f"told it was submitted: {screen.contents}"
    assert "Couldn't send your request" in screen.last


def test_a_failed_save_from_the_website_raises_and_leaves_no_card():
    from plugins.media_requests.cog import post_book_request
    channel = _Channel()
    user = SimpleNamespace(id=7, mention="<@7>", display_name="Sam")
    book = {"title": "The Book", "author": "A. Writer", "request_format": "ebook"}

    async def scenario():
        try:
            await post_book_request(None, _SERVICES, user, book)
        except RuntimeError:
            return True
        return False

    assert _run(scenario, channel, fail_save=True) is True
    assert channel.cards[0].deleted


# ------------------------------------------------------------ the season picker

def _show(numbers, status="Ended"):
    return {"id": 456, "media_type": "tv", "name": "The Show", "status": status,
            "seasons": [{"season_number": n, "episode_count": 10} for n in numbers]}


async def _pick_show(details):
    """Choose a show in the search results, with `details` as what TMDB returns for it."""
    from plugins.media_requests.cog import MediaSelectView

    async def tmdb_get(_path):
        if isinstance(details, Exception):
            raise details
        return dict(details)

    services = SimpleNamespace(config=SimpleNamespace(tmdb_api_key="k"), tmdb=SimpleNamespace(get=tmdb_get))
    view = MediaSelectView([{"id": 456, "media_type": "tv", "name": "The Show"}], 7, services)
    view.select_menu._values = ["0"]
    interaction = _Interaction()
    await view.select_menu.callback(interaction)
    return interaction.screen


def test_a_long_running_show_can_be_picked_season_by_season():
    screen = asyncio.run(_pick_show(_show(range(0, 37), status="Returning Series")))
    assert "Fetching" not in screen.last, "stuck on the loading message"
    selects = [c for c in screen.view.children if isinstance(c, discord.ui.Select)]
    offered = sorted(int(o.value) for s in selects for o in s.options)
    assert offered == list(range(1, 37)), offered
    assert all(len(s.options) <= 25 and s.max_values <= len(s.options) for s in selects)
    labels = [getattr(c, "label", "") or "" for c in screen.view.children]
    assert any("All" in label for label in labels), labels
    assert any("S36" in label for label in labels), "Latest + Monitor names the newest season"


def test_seasons_picked_in_different_menus_are_requested_together():
    async def scenario():
        screen = await _pick_show(_show(range(1, 31)))
        view = screen.view
        first, second = [c for c in view.children if isinstance(c, discord.ui.Select)]
        first._values = ["2", "25"]
        await first.callback(_Interaction())
        second._values = ["30"]
        await second.callback(_Interaction())
        return view.selected_seasons

    assert asyncio.run(scenario()) == [2, 25, 30]


def test_a_show_with_no_regular_seasons_says_so():
    for details in (_show([0]), {"id": 456, "media_type": "tv", "name": "The Show", "status": "In Production"}):
        screen = asyncio.run(_pick_show(details))
        assert "Fetching" not in screen.last, "stuck on the loading message"
        assert "no seasons" in screen.last.lower(), screen.last


def test_a_failure_while_showing_the_seasons_is_reported():
    from plugins.media_requests import cog

    def broken(*_, **__):
        raise RuntimeError("boom")

    original = cog.SeasonSelectionView
    cog.SeasonSelectionView = broken
    try:
        screen = asyncio.run(_pick_show(_show([1, 2])))
    finally:
        cog.SeasonSelectionView = original
    assert screen.last.startswith("❌"), screen.contents


def test_a_request_says_why_it_could_not_reach_the_admin_channel():
    from core.discord_lookup import AdminChannelUnavailable, require_admin_channel
    from plugins.media_requests import cog
    assert cog.AdminChannelUnavailable is AdminChannelUnavailable, "callers still catch it from media_requests"
    nowhere = SimpleNamespace(get_channel=lambda cid: None)
    for config, said in ((SimpleNamespace(admin_channel_id=None), "Admin channel not configured"),
                         (SimpleNamespace(admin_channel_id=77), "Admin channel 77 not found")):
        try:
            require_admin_channel(nowhere, config)
        except AdminChannelUnavailable as e:
            assert str(e) == said, str(e)
        else:
            raise AssertionError(f"no error for {config}")
    channel = _Channel()
    assert require_admin_channel(SimpleNamespace(get_channel=lambda cid: channel), SimpleNamespace(admin_channel_id=77)) is channel


# ------------------------------------------------------------ the search box

async def _search(modal_name, found):
    """Submit the film/TV or book search box, with `found` as what the search returns (or raises)."""
    from plugins.media_requests import cog
    asked = []

    async def search(query):
        asked.append(query)
        if isinstance(found, Exception):
            raise found
        return found

    modal = getattr(cog, modal_name)(SimpleNamespace(services=_SERVICES, _search_tmdb=search, _search_open_library=search))
    modal.search_query._value = "  Dune "
    interaction = _Interaction()
    await modal.on_submit(interaction)
    return interaction.screen, asked


def test_both_search_boxes_share_one_submit():
    from plugins.media_requests import cog
    for modal in (cog.TVMovieRequestModal, cog.BookRequestModal):
        assert "on_submit" not in vars(modal), f"{modal.__name__} carries its own copy of the search submit"
    assert cog.TVMovieRequestModal.on_submit is cog.BookRequestModal.on_submit


def test_a_search_shows_what_it_found_or_says_why_not():
    import logging
    from plugins.media_requests import cog

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    log = logging.getLogger(cog.__name__)
    log.addHandler(handler)
    try:
        for modal_name, found, menu, logged in (
                ("TVMovieRequestModal", [{"id": 1, "media_type": "movie", "title": "Dune"}], "MediaSelectView", "Search error"),
                ("BookRequestModal", [{"title": "Dune", "author": "Frank Herbert"}], "BookSelectView", "Book search error")):
            screen, asked = asyncio.run(_search(modal_name, found))
            assert asked == ["Dune"], asked
            assert screen.notes == ["🔍 Searching..."], screen.notes
            assert screen.last == "**I found these titles:**" and type(screen.view).__name__ == menu, (screen.last, screen.view)

            screen, _ = asyncio.run(_search(modal_name, []))
            assert screen.last == "No results found. Please try a different search term or use `/request` again."

            records.clear()
            screen, _ = asyncio.run(_search(modal_name, RuntimeError("tmdb down")))
            assert screen.last == "An error occurred. Please try again later."
            assert [r.getMessage() for r in records] == [f"{logged} after modal submit: tmdb down"], records
    finally:
        log.removeHandler(handler)


# ------------------------------------------------------------ how seasons read

def test_seasons_read_the_same_wherever_they_are_shown():
    from plugins.media_requests import cog
    assert cog.seasons_label("all") == "All Seasons"
    assert cog.seasons_label([3, 1]) == "S1, S3"
    assert cog.seasons_label([3, 1], long=True) == "Season 1, Season 3"


def _seasons_field(card):
    return next(f.value for f in card.embed.fields if f.name == "Seasons")


def test_picked_seasons_read_the_same_from_menu_to_admin_card():
    channel = _Channel()

    async def scenario():
        view = (await _pick_show(_show(range(1, 31)))).view
        first, second = [c for c in view.children if isinstance(c, discord.ui.Select)]
        first._values = ["2"]
        await first.callback(_Interaction())
        second._values = ["30"]
        picked = _Interaction()
        await second.callback(picked)
        confirming = _Interaction()
        await view.submit_button.callback(confirming)
        await confirming.screen.view.confirm.callback(_Interaction(bot=SimpleNamespace(services=_SERVICES)))
        return picked.screen.last, confirming.screen.embed.description

    picked, requesting = _run(scenario, channel)
    assert picked.startswith("**Selected:** S2, S30\n\n"), picked
    assert requesting == "**Requesting:** Season 2, Season 30", requesting
    assert _seasons_field(channel.cards[-1]) == "S2, S30"


def test_all_seasons_and_latest_with_monitor_read_the_same_on_the_admin_card():
    channel = _Channel()

    async def scenario():
        said = []
        for button in ("All Seasons", "Latest (S5) + Monitor"):
            view = (await _pick_show(_show(range(1, 6), status="Returning Series"))).view
            pressed = next(c for c in view.children if getattr(c, "label", None) == button)
            confirming = _Interaction()
            await pressed.callback(confirming)
            await confirming.screen.view.confirm.callback(_Interaction(bot=SimpleNamespace(services=_SERVICES)))
            said.append(confirming.screen.embed.description)
        return said

    said = _run(scenario, channel)
    assert said == ["**Requesting:** All Seasons", "**Selected:** Season 5 + Monitor for new episodes 🔔"], said
    assert [_seasons_field(card) for card in channel.cards] == ["All Seasons", "S5 🔔 (Monitor enabled)"]


# ------------------------------------------- checked like a request from the website

class _Known:
    """What the website knows: this member's requests, and Seerr's word on the title."""

    def __init__(self, mine=(), seerr=None, seasons=(), seerr_down=False):
        self.mine, self.seerr, self.seasons, self.seerr_down = list(mine), seerr or {}, list(seasons), seerr_down
        self.asked_for, self.seerr_asked = [], 0

    async def my_requests(self, user_id, plex_account_id=None):
        self.asked_for.append((user_id, plex_account_id))
        return list(self.mine)

    async def _seerr(self, path, ttl):
        self.seerr_asked += 1
        if self.seerr_down:
            raise RuntimeError("Seerr is down")
        return dict(self.seerr)

    async def tv_seasons(self, tid, detail):
        return list(self.seasons)


def _website(seerr_set_up=True, **known):
    from portal.actions import Actions
    services = SimpleNamespace(config=_SERVICES.config, seerr=SimpleNamespace(configured=seerr_set_up))
    known.setdefault("seerr_down", not seerr_set_up)    # as the real client: asking an unset Seerr fails
    return Actions(None, services, _Known(**known), "")


def _confirm(view, website, channel):
    """Press ✅ Yes on `view` with the website running; what the member was told."""
    async def scenario():
        interaction = _Interaction(bot=SimpleNamespace(services=_SERVICES, portal_actions=website))
        await view.confirm.callback(interaction)
        return interaction.screen.last
    return _run(scenario, channel)


def _film_view():
    from plugins.media_requests.cog import ConfirmationView
    return ConfirmationView(dict(_FILM), 7, _SERVICES)


def _show_view(seasons, monitor=False):
    from plugins.media_requests.cog import ConfirmationView
    return ConfirmationView({"id": 456, "media_type": "tv", "name": "The Show"}, 7, _SERVICES, seasons, monitor=monitor)


def test_a_film_asked_for_already_is_refused_on_discord_as_on_the_website():
    channel = _Channel()
    website = _website(mine=[{"title": {"kind": "movie", "id": "603"}, "stage": "requested"}])
    said = _confirm(_film_view(), website, channel)
    assert said == "❌ You already asked for this one.", said
    assert channel.cards == [], "the admins were asked twice"
    assert website.data.asked_for == [(7, None)]


def test_an_open_show_doesnt_stop_a_film_with_the_same_tmdb_number():
    """TMDB numbers films and shows separately: show 603 is not film 603."""
    channel = _Channel()
    said = _confirm(_film_view(), _website(mine=[{"title": {"kind": "tv", "id": "603"}, "stage": "requested"}]), channel)
    assert "Request submitted" in said and len(channel.cards) == 1, said


def test_a_declined_film_can_be_asked_for_again():
    channel = _Channel()
    said = _confirm(_film_view(), _website(mine=[{"title": {"kind": "movie", "id": "603"}, "stage": "declined"}]), channel)
    assert "Request submitted" in said and len(channel.cards) == 1, said


def test_discord_requests_count_towards_the_websites_limit():
    channel = _Channel()
    website = _website()
    for _ in range(20):
        website.limit("7", "request")      # twenty asked for already, on either side
    said = _confirm(_film_view(), website, channel)
    assert said == "❌ That's a lot at once. Try again later.", said
    assert channel.cards == []


def test_a_film_blocked_in_seerr_or_on_plex_already_is_refused_on_discord():
    for status, words in ((6, "blocked"), (5, "already on Plex")):
        channel = _Channel()
        said = _confirm(_film_view(), _website(seerr={"mediaInfo": {"status": status}}), channel)
        assert said.startswith("❌") and words in said, said
        assert channel.cards == []


def test_a_show_is_refused_only_when_nothing_asked_for_is_still_missing():
    have = [{"n": 1, "status": "available"}, {"n": 2, "status": "requested"}, {"n": 3, "status": "upcoming"}]
    for seasons, monitor, refused in (("all", False, "Every season is already on Plex or requested."),
                                      ([2], False, "Those seasons are already on Plex or requested."),
                                      ([1, 2], False, "Those seasons are already on Plex or requested.")):
        channel = _Channel()
        said = _confirm(_show_view(seasons, monitor), _website(seasons=have if seasons != "all" else have[:2]), channel)
        assert said == f"❌ {refused}", (seasons, said)
        assert channel.cards == []
    # Something still to get: a missing season, or one not aired yet (by hand, with All
    # Seasons or followed with Latest + Monitor alike).
    for seasons, monitor, known in (("all", False, have + [{"n": 4, "status": "none"}]),
                                    ("all", False, have),
                                    ([3], False, have),
                                    ([2, 4], False, have + [{"n": 4, "status": "partial"}]),
                                    ([3], True, have)):
        channel = _Channel()
        said = _confirm(_show_view(seasons, monitor), _website(seasons=known), channel)
        assert "Request submitted" in said and len(channel.cards) == 1, (seasons, said)


def test_a_book_asked_for_already_is_refused_on_discord():
    from plugins.media_requests.cog import BookConfirmationView
    book = {"title": "The Book", "author": "A. Writer", "open_library_key": "/works/OL1W", "request_format": "both"}
    channel = _Channel()
    said = _confirm(BookConfirmationView(dict(book), 7, _SERVICES),
                    _website(mine=[{"title": {"kind": "audiobook", "id": "OL1W"}, "stage": "approved"}]), channel)
    assert said == "❌ You already asked for this one.", said
    assert channel.cards == []


def test_a_discord_request_still_goes_when_seerr_cant_say():
    """Seerr is optional for Discord's /request: without its word, the admins decide."""
    channel = _Channel()
    said = _confirm(_film_view(), _website(seerr_down=True), channel)
    assert "Request submitted" in said and len(channel.cards) == 1, said


def test_without_seerr_set_up_a_discord_request_goes_quietly_after_the_other_checks():
    """No Seerr is a normal setup, not an outage: no Seerr call and no warning."""
    import logging
    from plugins.media_requests import cog
    warned = []
    handler = logging.Handler(logging.WARNING)
    handler.emit = warned.append
    logging.getLogger(cog.__name__).addHandler(handler)
    try:
        channel = _Channel()
        website = _website(seerr_set_up=False)
        said = _confirm(_film_view(), website, channel)
    finally:
        logging.getLogger(cog.__name__).removeHandler(handler)
    assert "Request submitted" in said and len(channel.cards) == 1, said
    assert website.data.seerr_asked == 0 and website.data.asked_for == [(7, None)]
    assert not warned, [r.getMessage() for r in warned]
    said = _confirm(_film_view(), _website(seerr_set_up=False, mine=[{"title": {"kind": "movie", "id": "603"}, "stage": "requested"}]),
                    _Channel())
    assert said == "❌ You already asked for this one.", said


# ------------------------------------------------------------ /my-requests

def _my_requests(bot):
    from plugins.media_requests.cog import MediaRequestsCog
    cog = MediaRequestsCog(bot, _SERVICES)
    interaction = _Interaction(bot=bot)
    asyncio.run(cog.list_my_requests.callback(cog, interaction))
    return interaction.screen


def test_members_can_list_their_own_requests_on_discord():
    rows = [{"slot": 12, "stage": "downloading", "seasons": [1, 2], "progress": {"percent": 40},
             "title": {"kind": "tv", "id": "456", "title": "The Show", "year": "2019"}},
            {"slot": 7, "stage": "available", "seasons": None, "progress": None,
             "title": {"kind": "ebook", "id": "OL1W", "title": "The Book", "year": ""}},
            {"slot": 3, "stage": "requested", "seasons": None, "progress": {"detail": "Older request; its outcome wasn't recorded"},
             "title": {"kind": "movie", "id": "603", "title": "The Film", "year": "1999"}}]
    known = _Known(mine=rows)
    bot = SimpleNamespace(portal_actions=SimpleNamespace(data=known, public_url="https://plexbie.example.com/"))
    screen = _my_requests(bot)
    assert known.asked_for == [(7, None)], "only their own requests, by their Discord id"
    embed = screen.embed
    assert embed is not None, screen.notes
    names = [f.name for f in embed.fields]
    assert names == ["No. 0012 · The Show (2019)", "No. 0007 · The Book", "No. 0003 · The Film (1999)"], names
    values = [f.value for f in embed.fields]
    assert "Downloading" in values[0] and "40%" in values[0] and "S1, S2" in values[0], values[0]
    assert "On Audiobookshelf" in values[1], values[1]
    assert "Requested" in values[2] and "outcome wasn't recorded" in values[2], values[2]
    assert "https://plexbie.example.com/schedule" in (embed.description or ""), embed.description


def test_my_requests_shows_the_newest_ten_and_says_how_many_more():
    rows = [{"slot": n, "stage": "requested", "seasons": None, "progress": None,
             "title": {"kind": "movie", "id": str(n), "title": f"Film {n}", "year": ""}} for n in range(14, 0, -1)]
    screen = _my_requests(SimpleNamespace(portal_actions=SimpleNamespace(data=_Known(mine=rows), public_url="")))
    assert len(screen.embed.fields) == 10 and screen.embed.fields[0].name == "No. 0014 · Film 14"
    assert "10 of 14" in screen.embed.footer.text, screen.embed.footer.text


def test_my_requests_with_nothing_asked_for_or_no_website():
    screen = _my_requests(SimpleNamespace(portal_actions=SimpleNamespace(data=_Known(), public_url="")))
    assert screen.embed is None and "/request" in screen.notes[-1], screen.notes
    screen = _my_requests(SimpleNamespace(portal_actions=None))
    assert screen.embed is None and "website" in screen.notes[-1], screen.notes


# ------------------------------------------------------------ no TMDB key

def test_without_a_tmdb_key_film_and_tv_search_says_so_instead_of_offering_a_made_up_film():
    from plugins.media_requests.cog import MediaRequestsCog, MediaTypeSelectView
    config = SimpleNamespace(admin_channel_id=1, tmdb_api_key=None)
    cog = MediaRequestsCog(SimpleNamespace(), SimpleNamespace(config=config))

    async def press_tv_and_movie():
        interaction = _Interaction()
        await MediaTypeSelectView(cog, 7).tv_movie.callback(interaction)
        return interaction.screen

    assert asyncio.run(cog._search_tmdb("Dune")) == [], "offered a film TMDB never returned"
    screen = asyncio.run(press_tv_and_movie())
    assert screen.modal is None, "the search box opened with nothing to search with"
    assert screen.notes == ["Search needs a TMDB API key. Ask an admin to add one in Plexbie's setup."], screen.notes

    config.tmdb_api_key = "k"
    screen = asyncio.run(press_tv_and_movie())
    assert type(screen.modal).__name__ == "TVMovieRequestModal" and screen.notes == [], screen.notes


# ------------------------------------------------------------ /requests

def _awaiting(saved):
    """What an admin's /requests shows, with `saved` storing the requests first."""
    from plugins.media_requests import cog

    async def admin(interaction):
        return True

    async def scenario():
        await saved()
        command = cog.MediaRequestsCog(SimpleNamespace(), _SERVICES)
        interaction = _Interaction()
        interaction.guild = SimpleNamespace(id=99)
        await command.list_requests.callback(command, interaction)
        return interaction.screen.embed

    allowed, cog.require_admin = cog.require_admin, admin
    try:
        return _run(scenario, _Channel())
    finally:
        cog.require_admin = allowed


def test_requests_links_each_request_to_its_own_card():
    async def saved():
        from database.request_store import save_request, set_fields
        await save_request(5000, user_id=1, media=dict(_FILM), media_type="movie")
        await set_fields(5000, timestamp="2025-01-01T00:00:00+00:00")
        # Made in Seerr, keyed by Seerr's number: one announced in the admin channel, one not.
        for number, name in ((41, "The Show"), (42, "Unannounced")):
            await save_request(number, user_id=1, media={"id": number, "media_type": "tv", "name": name},
                               media_type="tv", extra={"source": "seerr", "overseerr_request_id": number})
        await set_fields(41, admin_card_id=9001)

    embed = _awaiting(saved)
    shown = {f.name: f.value for f in embed.fields}
    assert "https://discord.com/channels/99/1/5000)" in shown["The Film"], shown["The Film"]
    assert "[Open the approval message](https://discord.com/channels/99/1/5000)" in shown["The Film"]
    assert "[Open the announcement](https://discord.com/channels/99/1/9001)" in shown["The Show"], shown["The Show"]
    assert "discord.com" not in shown["Unannounced"], "linked to a message that doesn't exist"
    assert "predate" not in (embed.footer.text or ""), embed.footer.text
