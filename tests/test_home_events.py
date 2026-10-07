"""Events from other Discord servers, and DMs from outside the household, are ignored.

While Public Bot is on, anyone can add Plexbie to a server of their own. Its
commands and admin buttons already answer only in the household's server
(test_home_guild.py); this covers the rest of what reaches it from there: the
invite tracker and watch parties ignore other servers' events, and a DM reaches
the admins only from a member of the household's server or the bot owner.
With GUILD_ID blank no server is home yet, so all of them are ignored and only
the owner's DMs get through.
"""
import asyncio
import logging
import pathlib
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace

import conftest  # noqa: F401
import discord
from helpers import HOME, OTHER, FakeServices

from core.config import Config
from test_home_guild import _Env, _Guild, _Logs, _Response, _config, _plexbie

OWNER = 500
MEMBER = 7
STRANGER = 8


async def _init(path):
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


# --- the invite tracker ---

class _Invite:
    def __init__(self, code, uses, guild=None):
        self.code, self.uses, self.guild = code, uses, guild
        self.inviter, self.max_uses, self.temporary = None, 0, False
        self.created_at, self.expires_at = datetime.now(timezone.utc), None


class _InviteGuild:
    """A server whose invites can be listed; remembers each time they were."""

    def __init__(self, gid, invites=()):
        self.id, self.name, self.invites_now, self.listed = gid, f"Server {gid}", list(invites), 0

    async def invites(self):
        self.listed += 1
        return list(self.invites_now)


class _Joiner:
    def __init__(self, uid, guild):
        self.id, self.guild, self.bot = uid, guild, False

    def __str__(self):
        return f"Joiner({self.id})"


def _tracker(guild_id, guilds):
    from plugins.invite_tracker.cog import InviteTrackerCog

    class Bot:
        def __init__(self):
            self.guilds = list(guilds)

        def get_guild(self, gid):
            return next((g for g in self.guilds if g.id == gid), None)

    config = _config(guild_id=guild_id)
    cog = InviteTrackerCog(Bot(), SimpleNamespace(config=config))
    cog.refreshed = []

    async def refresh(guild):
        cog.refreshed.append(guild.id)
    cog._update_invite_cache = refresh
    return cog


def test_invite_cache_loads_only_the_household_server():
    home, other = _InviteGuild(HOME), _InviteGuild(OTHER)
    cog = _tracker(HOME, [other, home])
    asyncio.run(cog.on_ready())
    assert cog.refreshed == [HOME]


def test_invite_tracker_ignores_a_join_in_another_server():
    other = _InviteGuild(OTHER, [_Invite("abc", 2)])
    cog = _tracker(HOME, [other])
    cog.invite_cache[str(OTHER)] = {"abc": _Invite("abc", 1)}
    with _Logs("plugins.invite_tracker.cog") as logs:
        asyncio.run(cog.on_member_join(_Joiner(STRANGER, other)))
    assert other.listed == 0 and cog.refreshed == []
    assert logs.records == [], [r.getMessage() for r in logs.records]


def test_invite_tracker_ignores_invites_made_or_deleted_in_another_server():
    other = _InviteGuild(OTHER)
    cog = _tracker(HOME, [other])
    cog.invite_cache[str(OTHER)] = {"abc": _Invite("abc", 1)}
    with _Logs("plugins.invite_tracker.cog") as logs:
        asyncio.run(cog.on_invite_create(_Invite("new", 0, guild=other)))
        asyncio.run(cog.on_invite_delete(_Invite("abc", 1, guild=other)))
    assert cog.refreshed == []
    assert "abc" in cog.invite_cache[str(OTHER)]
    assert logs.records == [], [r.getMessage() for r in logs.records]


def test_invite_tracker_still_records_a_join_in_the_household_server():
    from sqlalchemy import select
    from database.session import get_session
    from plugins.invite_tracker.models import InviteUse
    home = _InviteGuild(HOME, [_Invite("abc", 2)])
    cog = _tracker(HOME, [home])
    cog.invite_cache[str(HOME)] = {"abc": _Invite("abc", 1)}

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await cog.on_member_join(_Joiner(MEMBER, home))
        async with get_session() as session:
            return (await session.execute(select(InviteUse))).scalars().all()

    rows = asyncio.run(scenario())
    assert [(r.guild_id, r.invite_code, r.joiner_id) for r in rows] == [(str(HOME), "abc", str(MEMBER))]
    assert cog.invite_cache[str(HOME)]["abc"].uses == 2


def test_invite_tracker_ignores_every_server_while_guild_id_is_blank():
    home = _InviteGuild(HOME, [_Invite("abc", 2)])
    cog = _tracker(None, [home])
    cog.invite_cache[str(HOME)] = {"abc": _Invite("abc", 1)}
    asyncio.run(cog.on_ready())
    asyncio.run(cog.on_member_join(_Joiner(MEMBER, home)))
    asyncio.run(cog.on_invite_create(_Invite("new", 0, guild=home)))
    assert home.listed == 0 and cog.refreshed == []


def test_a_server_adopted_after_start_up_gets_its_invite_cache():
    """The cog's on_ready can run before a blank GUILD_ID is filled in; the
    household's server is cached as soon as it becomes home."""
    home = _InviteGuild(HOME)
    cog = _tracker(None, [home])
    asyncio.run(cog.on_ready())
    assert cog.refreshed == []
    cog.services.config.guild_id = HOME
    asyncio.run(cog.on_home_server(home))
    assert cog.refreshed == [HOME]


def test_plexbie_announces_its_household_server_when_it_adopts_or_joins_it():
    mine = _Guild(HOME, owner_id=OWNER)
    bot = _plexbie(_config(), [mine])
    with _Env():
        asyncio.run(bot._settle_home_guild())
    assert bot.config.guild_id == HOME
    assert bot.dispatched == [("home_server", HOME)]

    late = _plexbie(_config(guild_id=HOME), [])
    late.guilds.append(mine)
    asyncio.run(late.on_guild_join(mine))
    assert late.dispatched == [("home_server", HOME)]

    stranger = _Guild(OTHER, owner_id=77)
    late.guilds.append(stranger)
    asyncio.run(late.on_guild_join(stranger))
    assert late.dispatched == [("home_server", HOME)], "another server is never announced as home"


# --- watch parties ---

class _Voice:
    def __init__(self, channel=None, streaming=False):
        self.channel, self.self_stream = channel, streaming


def _watch_party(guild_id):
    from plugins.watch_party.cog import ActiveWatchParty, WatchPartyCog
    cog = WatchPartyCog.__new__(WatchPartyCog)     # no background loops
    cog.services = SimpleNamespace(config=_config(guild_id=guild_id))
    cog.watch_party_channel_id = 55
    cog.active_party = ActiveWatchParty(session_id=1, voice_channel_id=55, streamer_discord_id=MEMBER,
                                        streamer_plex_username="jordan", media_title="Big Buck Bunny",
                                        started_at=datetime.now(timezone.utc))
    cog.handled = []
    for name in ("_handle_stream_start", "_handle_stream_stop", "_handle_user_joined", "_handle_user_left"):
        async def handled(*args, _name=name):
            cog.handled.append(_name)
        setattr(cog, name, handled)
    return cog


def _voice_member(guild_id):
    return SimpleNamespace(id=MEMBER, name="jordan", guild=SimpleNamespace(id=guild_id))


def test_watch_party_ignores_voice_changes_in_another_server():
    """The streamer stopping a stream somewhere else must not end the household's party."""
    cog = _watch_party(HOME)
    elsewhere = SimpleNamespace(id=66, name="General")
    with _Logs("plugins.watch_party.cog") as logs:
        asyncio.run(cog.on_voice_state_update(_voice_member(OTHER), _Voice(elsewhere, True), _Voice(elsewhere, False)))
        asyncio.run(cog.on_voice_state_update(_voice_member(OTHER), _Voice(None), _Voice(SimpleNamespace(id=55), False)))
    assert cog.handled == []
    assert logs.records == [], [r.getMessage() for r in logs.records]


def test_watch_party_still_follows_the_household_server():
    cog = _watch_party(HOME)
    party = SimpleNamespace(id=55, name="Watch Party")
    asyncio.run(cog.on_voice_state_update(_voice_member(HOME), _Voice(party, True), _Voice(party, False)))
    assert cog.handled == ["_handle_stream_stop"]


def test_watch_party_ignores_every_server_while_guild_id_is_blank():
    cog = _watch_party(None)
    party = SimpleNamespace(id=55, name="Watch Party")
    asyncio.run(cog.on_voice_state_update(_voice_member(HOME), _Voice(party, True), _Voice(party, False)))
    assert cog.handled == []


# --- DMs ---

class _Home:
    """The household's server: who is cached, who Discord knows about, and what it says."""

    def __init__(self, cached=(), fetchable=(), error=None):
        self.id, self.cached, self.fetchable, self.error, self.fetched = HOME, set(cached), set(fetchable), error, []

    def get_member(self, uid):
        return object() if uid in self.cached else None

    async def fetch_member(self, uid):
        self.fetched.append(uid)
        if self.error is not None:
            raise self.error
        if uid in self.fetchable:
            return object()
        raise discord.NotFound(_Response(404), "Unknown Member")


def _dm_bot(home, guild_id=HOME, cached=True):
    """A bot with an admin channel that can make threads, and people who can be DMed.
    cached=False: the household's server isn't in the bot's cache yet, only Discord has it."""
    from portal import inbox
    inbox._not_members.clear()
    out = SimpleNamespace(dms=[], posts=[], alerts=[], guild_fetches=0)

    class Thread:
        id, archived = 77, False

        async def send(self, content=None, **kw):
            out.posts.append(content)

    class Start:
        async def create_thread(self, name, auto_archive_duration=None):
            return Thread()

    class Channel:
        guild = None

        async def send(self, content=None, **kw):
            out.posts.append(content)
            return Start()

    class Actions:
        async def alert_admins_about_dm(self, who, name, text):
            out.alerts.append((who, text))

    class Bot:
        services = FakeServices(Config())
        portal_actions = Actions()

        def get_guild(self, gid):
            return home if (gid == HOME and cached) else None

        async def fetch_guild(self, gid):
            out.guild_fetches += 1
            if home is None or gid != HOME:
                raise discord.HTTPException(_Response(503), "Service Unavailable")
            return home

        def get_channel(self, cid):
            return Thread() if int(cid) == 77 else Channel()

    bot = Bot()
    bot.services.config.admin_channel_id = 9
    bot.services.config.guild_id = guild_id
    bot.services.config.bot_owner_id = OWNER
    bot.out = out
    return bot


class _Dm:
    guild, attachments = None, []

    def __init__(self, uid, text, out):
        if isinstance(text, list):          # attachments only
            text, self.attachments = "", [SimpleNamespace(filename=name) for name in text]
        async def send(_self, content=None, embed=None, view=None):
            out.dms.append(content)
        self.content = text
        self.author = type("A", (), {"id": uid, "name": f"user{uid}", "display_name": f"User {uid}",
                                     "bot": False, "send": send})()


def _deliver(bot, *dms):
    """Send each (author id, text) to on_dm; who Manage → Messages then lists."""
    from core import message_log
    from portal import inbox

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        for uid, text in dms:
            await inbox.on_dm(bot, _Dm(uid, text, bot.out))
        return [p["id"] for p in await message_log.people() if p["received"]]
    return asyncio.run(scenario())


def test_a_dm_from_outside_the_household_server_reaches_nobody():
    from portal import inbox
    inbox._ignored_logged.pop(STRANGER, None)
    home = _Home(cached=[MEMBER])
    bot = _dm_bot(home)
    with _Logs("portal.inbox") as logs:
        heard = _deliver(bot, (STRANGER, "hi, free Nitro: example.com"), (STRANGER, "hello?"))
    assert heard == []
    assert bot.out.dms == [] and bot.out.posts == [] and bot.out.alerts == []
    assert STRANGER in home.fetched, "not cached, so Discord is asked"
    info = [m for m in logs.at(logging.INFO) if f"({STRANGER})" in m]
    assert len(info) == 1, logs.at(logging.INFO)
    assert "Nitro" not in info[0], "what they wrote stays out of the log"
    assert any(f"({STRANGER})" in m for m in logs.at(logging.DEBUG)), "later ones go to DEBUG"


def test_a_dm_from_a_household_member_still_reaches_the_admins():
    bot = _dm_bot(_Home(cached=[MEMBER]))
    heard = _deliver(bot, (MEMBER, "is Dune coming?"))
    assert heard == [f"d{MEMBER}"]
    assert len(bot.out.dms) == 1 and bot.out.alerts == [(f"d{MEMBER}", "is Dune coming?")]
    assert any("is Dune coming?" in (p or "") for p in bot.out.posts)


def test_a_member_missing_from_the_cache_is_looked_up():
    home = _Home(fetchable=[MEMBER])
    bot = _dm_bot(home)
    assert _deliver(bot, (MEMBER, "hello")) == [f"d{MEMBER}"]
    assert home.fetched == [MEMBER]


def test_a_dm_is_refused_when_discord_wont_say_who_is_a_member():
    for error in (discord.Forbidden(_Response(403), "Missing Access"),
                  discord.HTTPException(_Response(500), "Server Error")):
        bot = _dm_bot(_Home(fetchable=[MEMBER], error=error))
        assert _deliver(bot, (MEMBER, "hello")) == [], type(error).__name__
        assert bot.out.posts == [] and bot.out.alerts == []


def test_the_bot_owner_can_always_dm_plexbie():
    bot = _dm_bot(_Home())
    assert _deliver(bot, (OWNER, "testing")) == [f"d{OWNER}"]
    assert bot.out.alerts == [(f"d{OWNER}", "testing")]


def test_with_guild_id_blank_only_the_owner_reaches_the_admins():
    bot = _dm_bot(_Home(cached=[MEMBER]), guild_id=None)
    assert _deliver(bot, (MEMBER, "hello"), (OWNER, "testing")) == [f"d{OWNER}"]
    assert bot.out.alerts == [(f"d{OWNER}", "testing")]


def test_a_stranger_who_keeps_writing_is_not_looked_up_every_time():
    """Discord is asked once; within the recheck window the answer is remembered, so a
    flood of DMs doesn't become a flood of member lookups."""
    from portal import inbox
    home = _Home(cached=[MEMBER])
    bot = _dm_bot(home)
    assert _deliver(bot, *[(STRANGER, f"hello {n}") for n in range(5)]) == []
    assert home.fetched == [STRANGER]
    inbox._not_members[STRANGER] -= inbox.NOT_MEMBER_RECHECK + 1     # the window has passed
    _deliver(bot, (STRANGER, "still there?"))
    assert home.fetched == [STRANGER, STRANGER]


def test_someone_who_joins_the_household_server_is_heard_at_once():
    """A remembered "not a member" never outweighs the member cache."""
    home = _Home()
    bot = _dm_bot(home)
    assert _deliver(bot, (STRANGER, "how do I join?")) == []
    home.cached.add(STRANGER)
    assert _deliver(bot, (STRANGER, "joined!")) == [f"d{STRANGER}"]
    assert home.fetched == [STRANGER]


def test_an_empty_dm_is_not_looked_up():
    """A sticker or an empty message has nothing to pass on, so Discord isn't asked."""
    home = _Home()
    bot = _dm_bot(home)
    assert _deliver(bot, (STRANGER, ""), (STRANGER, "   ")) == []
    assert home.fetched == []
    assert _deliver(bot, (STRANGER, ["photo.png"])) == []
    assert home.fetched == [STRANGER], "a file is something to pass on, so it is checked"


def test_a_members_dm_is_kept_while_the_server_is_not_cached_yet():
    """Start-up, or Discord briefly without the server: Discord is asked directly."""
    home = _Home(fetchable=[MEMBER])
    bot = _dm_bot(home, cached=False)
    assert _deliver(bot, (MEMBER, "hello")) == [f"d{MEMBER}"]
    assert bot.out.guild_fetches == 1 and home.fetched == [MEMBER]


def test_the_log_says_when_the_household_server_cant_be_reached():
    from portal import inbox
    inbox._ignored_logged.pop(MEMBER, None)
    bot = _dm_bot(None)
    with _Logs("portal.inbox") as logs:
        assert _deliver(bot, (MEMBER, "hello")) == []
    line = next(m for m in logs.at(logging.INFO) if f"({MEMBER})" in m)
    assert "can't reach the household's server" in line, line


# --- mentions ---

def test_mentioning_plexbie_is_not_read_as_a_text_command():
    """Plexbie has only slash commands. Were "@Plexbie hi" parsed as a text command, every
    mention in any server or DM would log a CommandNotFound at ERROR."""
    import bot as bot_module
    parsed = []

    class Bot:
        async def process_commands(self, message):
            parsed.append(message)

    for guild in (SimpleNamespace(id=OTHER), SimpleNamespace(id=HOME), None):
        message = SimpleNamespace(content="<@77> hi", guild=guild,
                                  author=SimpleNamespace(id=STRANGER, bot=False))
        with _Logs("bot") as logs:
            asyncio.run(bot_module.Plexbie.on_message(Bot(), message))
        assert parsed == [] and logs.at(logging.ERROR) == []
