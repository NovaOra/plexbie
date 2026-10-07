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


class _Followup:
    def __init__(self, screen):
        self.screen = screen

    async def send(self, content, ephemeral=False):
        self.screen.notes.append(content)


class _Screen:
    """The member's ephemeral message, which Discord refuses to show an invalid view on."""

    def __init__(self):
        self.contents, self.notes = [], []
        self.view = None

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
    def __init__(self, mid):
        self.id = mid
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _Channel:
    def __init__(self, fail_send=False):
        self.cards = []
        self.fail_send = fail_send

    async def send(self, **_):
        if self.fail_send:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        card = _Card(5000 + len(self.cards))
        self.cards.append(card)
        return card


def _run(scenario, channel, *, fail_save=False):
    """Run a scenario with a fresh database, `channel` as the admin channel and no phone alerts."""
    from plugins.media_requests import cog

    async def broken_save(*_, **__):
        raise RuntimeError("database is locked")

    patches = [(cog, "_admin_channel", lambda bot, services: channel),
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
