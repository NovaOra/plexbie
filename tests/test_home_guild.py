# path: tests/test_home_guild.py
"""Plexbie answers only in its household's own Discord server (GUILD_ID).

Anyone with the bot's ID can add a Public Bot to a server they own, where they
are Administrator. Commands used to be synced globally when GUILD_ID was blank,
and both admin checks accepted Administrator in whatever server the click came
from, so a stranger's server could run /remove-user or turn on media cleanup.

Covered here: the admin checks and the command tree refuse other servers; no
command is ever registered globally; a blank GUILD_ID is only filled in when
the server is provably the household's; setup can't finish without a server;
Manage → Health says which server Plexbie answers in and whether Public Bot is on.
"""
import ast
import asyncio
import logging
import os
import pathlib
import tempfile
from types import SimpleNamespace

import conftest  # noqa: F401
import discord
from discord import app_commands
from helpers import (
    HOME, OTHER, FakeInteraction, FakeMember, FakeRole, FakeUser, NoIdsConfig, PermConfig,
)

from core import permissions
from core.permissions import AdminActionView, AdminOnlyView, is_bot_admin, require_admin

ROOT = pathlib.Path(conftest.PROJECT_ROOT)
OWNER = 500


class _Logs:
    """Collect what the named loggers say for the duration of a block."""

    def __init__(self, *names, level=logging.DEBUG):
        self.names, self.level, self.records = names, level, []

    def __enter__(self):
        self.handler = logging.Handler()
        self.handler.emit = self.records.append
        self.saved = []
        for name in self.names:
            log = logging.getLogger(name)
            self.saved.append((log, log.level))
            log.addHandler(self.handler)
            log.setLevel(self.level)
        return self

    def __exit__(self, *exc):
        for log, level in self.saved:
            log.removeHandler(self.handler)
            log.setLevel(level)

    def at(self, level):
        return [r.getMessage() for r in self.records if r.levelno == level]


class _Response:
    """What an HTTP error from Discord carries, for building discord.py's exceptions."""

    def __init__(self, status, reason="Nope"):
        self.status, self.reason = status, reason


# --- the admin check counts only the household's server ---

def test_admin_from_another_server_is_refused():
    admin = FakeMember(1, administrator=True)
    assert is_bot_admin(FakeInteraction(admin, PermConfig, guild_id=OTHER)) is False
    assert is_bot_admin(FakeInteraction(admin, PermConfig)) is True


def test_admin_role_from_another_server_is_refused():
    member = FakeMember(2, roles=[FakeRole(PermConfig.admin_role_id)])
    assert is_bot_admin(FakeInteraction(member, PermConfig, guild_id=OTHER)) is False


def test_no_home_server_means_only_the_owner_is_admin():
    class Unbound(PermConfig):
        guild_id = None

    admin = FakeMember(3, administrator=True)
    assert is_bot_admin(FakeInteraction(admin, Unbound, guild_id=OTHER)) is False
    assert is_bot_admin(FakeInteraction(FakeMember(PermConfig.bot_owner_id), Unbound, guild_id=OTHER)) is True


def test_owner_passes_in_another_server_and_in_dms():
    owner = PermConfig.bot_owner_id
    assert is_bot_admin(FakeInteraction(FakeMember(owner), PermConfig, guild_id=OTHER)) is True
    assert is_bot_admin(FakeInteraction(FakeUser(owner), PermConfig, guild_id=None)) is True


def test_admin_only_views_refuse_another_servers_admin():
    class Approve(AdminActionView):
        pass

    for view in (AdminOnlyView(), Approve()):
        interaction = FakeInteraction(FakeMember(4, administrator=True), PermConfig, guild_id=OTHER)
        assert asyncio.run(view.interaction_check(interaction)) is False, type(view).__name__
        assert len(interaction.refusals) == 1


def test_require_admin_refuses_another_server_and_logs_the_server():
    interaction = FakeInteraction(FakeMember(5, administrator=True), PermConfig, guild_id=OTHER)
    with _Logs("core.permissions") as logs:
        assert asyncio.run(require_admin(interaction)) is False
    assert any(str(OTHER) in m for m in logs.at(logging.WARNING)), logs.at(logging.WARNING)


def test_admin_buttons_say_the_server_isnt_set_rather_than_no_permission():
    """With GUILD_ID blank the household's own admins are refused too: tell them why."""
    class Unbound(PermConfig):
        guild_id = None

    unbound = FakeInteraction(FakeMember(6, administrator=True), Unbound)
    unbound.client._home_settled = True
    assert asyncio.run(require_admin(unbound)) is False
    assert unbound.refusals == [permissions.UNBOUND_MESSAGE]
    assert "admin buttons" in permissions.UNBOUND_MESSAGE

    starting = FakeInteraction(FakeMember(6, administrator=True), Unbound)
    starting.client._home_settled = False
    assert asyncio.run(AdminOnlyView().interaction_check(starting)) is False
    assert starting.refusals == [permissions.STARTING_MESSAGE]

    plain = FakeInteraction(FakeMember(7), PermConfig)
    assert asyncio.run(require_admin(plain)) is False
    assert plain.refusals == [permissions.DENIED_MESSAGE], "with a home server it's still a plain refusal"


# --- the command tree: one check in front of every command ---

def _tree_bot(config):
    """A real discord.py bot with Plexbie's tree and one admin-only command."""
    from discord.ext import commands
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none(), tree_cls=permissions.HomeGuildTree)
    bot.services = SimpleNamespace(config=config)
    ran = []

    @app_commands.command(name="remove-user", description="Remove someone from Plex")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_user(interaction: discord.Interaction):
        ran.append(interaction.user.id)

    bot.tree.add_command(remove_user)
    return bot, ran


def _dispatched(bot, member, guild_id):
    interaction = FakeInteraction(member, None, guild_id=guild_id, data={"name": "remove-user", "type": 1})
    interaction.client = bot
    interaction._state, interaction.guild = None, None
    interaction.permissions = discord.Permissions(administrator=True)
    return interaction


def test_tree_refuses_a_command_from_another_server():
    """Through discord.py's own dispatch: the server check comes before the
    command's has_permissions check, which a stranger's Administrator passes."""
    bot, ran = _tree_bot(PermConfig)
    interaction = _dispatched(bot, FakeMember(6, administrator=True), OTHER)
    asyncio.run(bot.tree._call(interaction))
    assert ran == [], "the command ran for an admin of somebody else's server"
    assert interaction.response.sent == [permissions.FOREIGN_MESSAGE]
    assert interaction.command_failed is True

    at_home = _dispatched(bot, FakeMember(6, administrator=True), HOME)
    asyncio.run(bot.tree._call(at_home))
    assert ran == [6] and at_home.refusals == []


def test_bot_uses_the_home_guild_tree():
    source = (ROOT / "bot.py").read_text()
    init = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    call = next(n for n in ast.walk(init) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "__init__")
    tree_cls = next((k.value for k in call.keywords if k.arg == "tree_cls"), None)
    assert isinstance(tree_cls, ast.Name) and tree_cls.id == "HomeGuildTree", (
        "Plexbie must be built with tree_cls=HomeGuildTree, or no command is bound to the household's server"
    )


def test_tree_answers_foreign_autocomplete_with_nothing():
    interaction = FakeInteraction(FakeMember(7), PermConfig, guild_id=OTHER,
                                  type=discord.InteractionType.autocomplete)
    assert asyncio.run(permissions.home_guild_check(interaction)) is False
    assert interaction.response.choices == [[]]
    assert interaction.refusals == []


def test_tree_lets_the_home_server_and_the_owner_through():
    check = permissions.home_guild_check
    assert asyncio.run(check(FakeInteraction(FakeMember(8), PermConfig))) is True
    owner = PermConfig.bot_owner_id
    assert asyncio.run(check(FakeInteraction(FakeMember(owner), PermConfig, guild_id=OTHER))) is True
    assert asyncio.run(check(FakeInteraction(FakeUser(owner), PermConfig, guild_id=None))) is True
    stranger_dm = FakeInteraction(FakeUser(9), PermConfig, guild_id=None)
    assert asyncio.run(check(stranger_dm)) is False


def test_tree_refuses_everyone_but_the_owner_while_unbound():
    class Unbound(PermConfig):
        guild_id = None

    starting = FakeInteraction(FakeMember(10, administrator=True), Unbound, guild_id=OTHER)
    starting.client._home_settled = False
    assert asyncio.run(permissions.home_guild_check(starting)) is False
    assert starting.refusals == [permissions.STARTING_MESSAGE]

    unbound = FakeInteraction(FakeMember(10, administrator=True), Unbound, guild_id=OTHER)
    unbound.client._home_settled = True
    assert asyncio.run(permissions.home_guild_check(unbound)) is False
    assert unbound.refusals == [permissions.UNBOUND_MESSAGE]

    owner = FakeInteraction(FakeMember(PermConfig.bot_owner_id), Unbound, guild_id=OTHER)
    assert asyncio.run(permissions.home_guild_check(owner)) is True


def test_foreign_refusals_dont_flood_the_log():
    noisy = 5150
    with _Logs("core.permissions") as logs:
        for _ in range(2):
            asyncio.run(permissions.home_guild_check(FakeInteraction(FakeMember(11), PermConfig, guild_id=noisy)))
    warned = [m for m in logs.at(logging.WARNING) if str(noisy) in m]
    quiet = [m for m in logs.at(logging.DEBUG) if str(noisy) in m]
    assert len(warned) == 1 and len(quiet) == 1, (warned, quiet)
    assert "/test" in warned[0] and str(HOME) in warned[0]


def test_nothing_dispatches_interactions_around_the_tree():
    """An on_interaction listener would see commands before the tree's check does."""
    offenders = []
    for path in [ROOT / "bot.py", *(p for d in ("core", "plugins", "portal") for p in (ROOT / d).rglob("*.py"))]:
        if "on_interaction" in path.read_text():
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


# --- admin commands take the same admins as the admin buttons ---

def _admin_commands():
    """Every admin slash command, with arguments that get it past its signature."""
    from plugins.media_requests.cog import MediaRequestsCog
    from plugins.status.cog import StatusCog
    from plugins.user_mgmt.cog import UserMgmtCog
    from plugins.watch_party.cog import WatchPartyCog

    channel = SimpleNamespace(sent=[], mention="#general", name="general")

    async def send(message, **kwargs):
        channel.sent.append(message)
    channel.send = send
    return channel, [
        (MediaRequestsCog.list_requests, ()),
        (UserMgmtCog.remove_user, ("someone",)),
        (UserMgmtCog.list_tracked_users, ()),
        (UserMgmtCog.list_plex_users, (None,)),
        (UserMgmtCog.manage_links, ()),
        (WatchPartyCog.watchparty_active, ()),
        (StatusCog.say_command, (channel, "hello")),
    ]


def test_admin_commands_refuse_before_doing_anything():
    """One check for every admin command: refused here, not by Discord's own
    Administrator check, which turns away ADMIN_ROLE_ID and the bot owner."""
    channel, commands = _admin_commands()
    for command, args in commands:
        assert not any("has_permissions" in check.__qualname__ for check in command.checks), (
            f"/{command.name} asks Discord for Administrator, refusing ADMIN_ROLE_ID and the bot owner"
        )
        for member, guild_id in ((FakeMember(12), HOME), (FakeMember(13, administrator=True), OTHER)):
            interaction = FakeInteraction(member, PermConfig, guild_id=guild_id)
            asyncio.run(command.callback(SimpleNamespace(), interaction, *args))
            assert interaction.refusals == [permissions.DENIED_MESSAGE], (command.name, interaction.refusals)
    assert channel.sent == [], "/say posted for someone who isn't an admin"


def test_owner_and_admin_role_run_admin_commands_without_administrator():
    """Through discord.py's own dispatch, like a real click."""
    from discord.ext import commands
    from plugins.watch_party.cog import WatchPartyCog

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none(), tree_cls=permissions.HomeGuildTree)
    bot.services = SimpleNamespace(config=PermConfig)
    cog = WatchPartyCog.__new__(WatchPartyCog)  # not __init__: it needs the full config
    cog.active_party = None
    bot.tree.add_command(cog.watchparty_active)

    def run(member, guild_id=HOME, administrator=False):
        interaction = FakeInteraction(member, None, guild_id=guild_id,
                                      data={"name": "watchparty-active", "type": 1})
        interaction.client = bot
        interaction._state, interaction.guild = None, None
        interaction.permissions = discord.Permissions(administrator=administrator)
        asyncio.run(bot.tree._call(interaction))
        return interaction

    ran = ["No active watch party."]
    assert run(FakeMember(PermConfig.bot_owner_id)).followup.sent == ran, "the bot owner was refused"
    assert run(FakeMember(14, roles=[FakeRole(PermConfig.admin_role_id)])).followup.sent == ran, (
        "an ADMIN_ROLE_ID holder was refused"
    )
    assert run(FakeMember(15, administrator=True), administrator=True).followup.sent == ran

    foreign = run(FakeMember(16, administrator=True), guild_id=OTHER, administrator=True)
    assert foreign.refusals == [permissions.FOREIGN_MESSAGE]
    member = run(FakeMember(17))
    assert member.refusals == [permissions.DENIED_MESSAGE]


# --- which server is home when GUILD_ID is blank ---

class _Guild:
    def __init__(self, gid, name="Server", channels=(), roles=(), members=(), owner_id=1):
        self.id, self.name, self.owner_id = gid, name, owner_id
        self.channels, self.roles, self.members = set(channels), set(roles), set(members)
        self.fetched, self.left, self.me = [], 0, None

    def get_channel(self, cid):
        return object() if cid in self.channels else None

    def get_role(self, rid):
        return object() if rid in self.roles else None

    def get_member(self, uid):
        return None     # not cached: fetch_member decides

    async def fetch_member(self, uid):
        self.fetched.append(uid)
        if uid in self.members:
            return object()
        raise discord.NotFound(_Response(404), "Unknown Member")

    async def leave(self):
        self.left += 1


def _app(owner=OWNER, team_owner=None, public=False):
    return SimpleNamespace(owner=SimpleNamespace(id=owner), bot_public=public,
                           team=SimpleNamespace(owner_id=team_owner) if team_owner else None)


class _PickBot:
    def __init__(self, guilds, app=None, error=None):
        self.guilds, self._app, self._error, self.asked = list(guilds), app or _app(), error, 0

    async def application_info(self):
        self.asked += 1
        if self._error:
            raise self._error
        return self._app


def _ids(**kw):
    keys = ("admin_channel_id", "updates_channel_id", "stats_channel_id", "watch_party_channel_id",
            "admin_role_id", "plex_member_role_id", "arrivals_role_id", "bot_owner_id", "guild_id")
    return SimpleNamespace(**{k: kw.get(k) for k in keys})


def _pick(bot, config):
    from core.discord_lookup import pick_home_guild
    return asyncio.run(pick_home_guild(bot, config))


def test_no_server_picks_nothing():
    assert _pick(_PickBot([]), _ids()) == (None, "none")


def test_the_server_holding_your_channels_is_picked_without_asking_who_owns_the_bot():
    g1, g2 = _Guild(1, channels=[11]), _Guild(2)
    bot = _PickBot([g2, g1])
    assert _pick(bot, _ids(admin_channel_id=11)) == (g1, "settings")
    assert bot.asked == 0


def test_settings_in_two_servers_pick_neither():
    g1, g2 = _Guild(1, channels=[11]), _Guild(2, roles=[22])
    assert _pick(_PickBot([g1, g2]), _ids(admin_channel_id=11, admin_role_id=22)) == (None, "conflict")


def test_settings_for_a_server_plexbie_isnt_in_block_a_strangers_server():
    stranger = _Guild(2, members=[OWNER])
    assert _pick(_PickBot([stranger]), _ids(admin_channel_id=11)) == (None, "elsewhere")


def test_two_servers_and_no_settings_pick_neither():
    assert _pick(_PickBot([_Guild(1, owner_id=OWNER), _Guild(2)]), _ids()) == (None, "several")


def test_the_only_server_is_picked_when_the_bots_owner_owns_it():
    g = _Guild(1, owner_id=OWNER)
    assert _pick(_PickBot([g]), _ids()) == (g, "owner")


def test_the_only_server_is_not_picked_when_the_bots_owner_isnt_there():
    """A blank install the household hasn't invited yet must not adopt the first
    stranger's server that adds it."""
    g = _Guild(1, members=[77], owner_id=77)
    assert _pick(_PickBot([g]), _ids()) == (None, "not_owner")


def test_the_only_server_is_not_picked_when_the_bots_owner_merely_belongs_to_it():
    """Anyone who runs a server the bot's owner is in could add Plexbie there."""
    g = _Guild(1, members=[OWNER], owner_id=77)
    assert _pick(_PickBot([g]), _ids()) == (None, "not_owner")


def test_no_answer_about_the_bots_owner_picks_nothing():
    g = _Guild(1, owner_id=OWNER)
    error = discord.HTTPException(_Response(500), "Discord is down")
    assert _pick(_PickBot([g], error=error), _ids()) == (None, "owner_unknown")


def test_every_reason_for_no_server_says_admin_buttons_are_off_too():
    from core.discord_lookup import problem_text
    for reason in ("none", "several", "elsewhere", "conflict", "not_owner", "owner_unknown"):
        assert "admin buttons are off" in problem_text(reason, [_Guild(1), _Guild(2)]), reason


def test_a_teams_owner_and_bot_owner_id_count_as_the_owner():
    g = _Guild(1, owner_id=OWNER)
    assert _pick(_PickBot([g], app=_app(owner=1, team_owner=OWNER)), _ids()) == (g, "owner")
    assert _pick(_PickBot([g], app=_app(owner=1)), _ids(bot_owner_id=OWNER)) == (g, "owner")


# --- syncing: the household's server only, never the global scope ---

def test_bot_never_syncs_globally():
    """A tree.sync() without guild= registers every command in every server Plexbie
    is in, and clear_commands(guild=None) empties the set later syncs copy from."""
    offenders = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "web/")):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            receiver = ast.unparse(node.func.value)
            keywords = {k.arg: k.value for k in node.keywords}
            if node.func.attr == "sync" and "tree" in receiver and "guild" not in keywords:
                offenders.append(f"{rel}:{node.lineno} {ast.unparse(node)}")
            if node.func.attr == "clear_commands" and isinstance(keywords.get("guild"), ast.Constant) \
                    and keywords["guild"].value is None:
                offenders.append(f"{rel}:{node.lineno} {ast.unparse(node)}")
    assert offenders == [], "\n  ".join(["global command registration:"] + offenders)


class _Tree:
    """Keeps the in-memory command set the way discord.py does: copy_global_to
    copies from it, so emptying it would make later syncs push nothing."""

    def __init__(self, stale=0, forbid=0):
        self.globals = dict.fromkeys(("request", "join-plex", "remove-user"))
        self.guilds, self.calls, self.stale, self.forbid = {}, [], stale, forbid

    def copy_global_to(self, *, guild):
        self.calls.append(("copy", guild.id))
        self.guilds[guild.id] = dict(self.globals)

    async def sync(self, *, guild=None):
        self.calls.append(("sync", getattr(guild, "id", None), len(self.guilds.get(getattr(guild, "id", None), {}))))
        if self.forbid:
            self.forbid -= 1
            raise discord.Forbidden(_Response(403), "Missing Access")
        return list(self.guilds[guild.id])

    async def fetch_commands(self, *, guild=None):
        return ["old"] * self.stale


class _Http:
    def __init__(self, tree):
        self.tree, self.upserts = tree, []

    async def bulk_upsert_global_commands(self, application_id, payload):
        self.upserts.append((application_id, payload))
        self.tree.stale = 0
        return []


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kw):
        self.sent.append(content)


def _config(**kw):
    from core.config import Config
    cfg = Config()
    for key in ("guild_id", "bot_owner_id", "admin_channel_id", "updates_channel_id", "stats_channel_id",
                "watch_party_channel_id", "admin_role_id", "plex_member_role_id", "arrivals_role_id"):
        setattr(cfg, key, None)
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


def _plexbie(config, guilds=(), tree=None, app=None):
    """Plexbie's own start-up methods on a bot with no Discord connection."""
    import bot as bot_module
    P = bot_module.Plexbie

    class Bot:
        _sync_commands, _sync_to_home, _clear_stale_globals = P._sync_commands, P._sync_to_home, P._clear_stale_globals
        _settle_home_guild, _adopt = P._settle_home_guild, P._adopt
        on_guild_join, on_guild_remove = P.on_guild_join, P.on_guild_remove

        def __init__(self):
            self.config, self.services = config, SimpleNamespace(config=config)
            self.guilds, self.tree = list(guilds), tree or _Tree()
            self.http, self.application_id = _Http(self.tree), 77
            self.application = app or _app()
            self._home_settled, self._home_problem, self._told_foreign, self._synced = False, None, set(), None
            self._foreign_notices = []
            self.channels, self.role_checks, self.dispatched = {}, 0, []

        def dispatch(self, event, *args):
            self.dispatched.append((event, *(getattr(a, "id", a) for a in args)))

        def get_guild(self, gid):
            return next((g for g in self.guilds if g.id == gid), None)

        def get_channel(self, cid):
            return self.channels.get(cid)

        async def application_info(self):
            return self.application

        def _check_role_order(self):
            self.role_checks += 1

    return Bot()


def _syncs(bot):
    return [c for c in bot.tree.calls if c[0] == "sync"]


def test_bound_home_syncs_there_and_clears_stale_globals():
    bot = _plexbie(_config(guild_id=HOME), [_Guild(HOME)], tree=_Tree(stale=2))
    with _Logs("bot") as logs:
        asyncio.run(bot._sync_commands())
    assert bot.tree.calls == [("copy", HOME), ("sync", HOME, 3)]
    assert bot.http.upserts == [(77, [])], "stale global commands are removed over HTTP"
    assert len(bot.tree.globals) == 3, "the in-memory command set must survive the clean-up"
    assert any(f"3 synced to guild {HOME}" in m for m in logs.at(logging.INFO))
    assert any("removing 2 stale global command" in m for m in logs.at(logging.WARNING))


def test_guild_sync_forbidden_does_not_stop_start_up():
    bot = _plexbie(_config(guild_id=HOME), [], tree=_Tree(stale=1, forbid=1))
    with _Logs("bot") as logs:
        asyncio.run(bot._sync_commands())
    assert any("won't let Plexbie add its commands" in m for m in logs.at(logging.ERROR)), logs.records
    assert bot.http.upserts == [(77, [])]


def test_joining_the_home_server_late_syncs_the_full_command_set():
    """GUILD_ID set first, Plexbie added afterwards: the commands appear on join,
    all of them, even though stale global ones were cleared in between."""
    home = _Guild(HOME)
    bot = _plexbie(_config(guild_id=HOME), [], tree=_Tree(stale=2, forbid=1))
    asyncio.run(bot._sync_commands())
    bot.guilds.append(home)
    asyncio.run(bot.on_guild_join(home))
    assert _syncs(bot) == [("sync", HOME, 3), ("sync", HOME, 3)]
    assert bot.role_checks == 1


def test_a_blank_guild_id_never_registers_commands_globally():
    bot = _plexbie(_config(), [_Guild(1, owner_id=OWNER), _Guild(2)], tree=_Tree(stale=4))
    with _Logs("bot") as logs:
        asyncio.run(bot._sync_commands())
        asyncio.run(bot._settle_home_guild())
    assert _syncs(bot) == []
    assert bot.http.upserts == [(77, [])]
    assert bot.config.guild_id is None
    assert any("Commands are OFF" in m and "2 servers" in m for m in logs.at(logging.ERROR)), logs.at(logging.ERROR)
    assert "2 servers" in bot._home_problem and bot._home_settled is True


# --- adopting the household's server ---

class _Env:
    """config/.env in a temporary folder, and GUILD_ID put back afterwards."""

    def __enter__(self):
        import bot as bot_module
        self.module, self.old = bot_module, bot_module.ENV_FILE
        self.path = pathlib.Path(tempfile.mkdtemp()) / ".env"
        self.path.write_text("DISCORD_BOT_TOKEN=t\nGUILD_ID=\n")
        bot_module.ENV_FILE = self.path
        self.saved = os.environ.pop("GUILD_ID", None)
        return self

    def __exit__(self, *exc):
        self.module.ENV_FILE = self.old
        os.environ.pop("GUILD_ID", None)
        if self.saved is not None:
            os.environ["GUILD_ID"] = self.saved


def test_adopting_the_only_server_pins_and_saves_it():
    g = _Guild(HOME, name="The Den", owner_id=OWNER)
    bot = _plexbie(_config(), [g])
    with _Env() as env, _Logs("bot") as logs:
        asyncio.run(bot._settle_home_guild())
        assert os.environ.get("GUILD_ID") == str(HOME)
        text = env.path.read_text()
    assert bot.config.guild_id == HOME
    assert f"GUILD_ID={HOME}" in text and "DISCORD_BOT_TOKEN=t" in text
    assert any(f"saved GUILD_ID={HOME}" in m and "The Den" in m for m in logs.at(logging.WARNING))
    assert _syncs(bot) == [("sync", HOME, 3)]
    assert bot._home_problem is None


def test_saving_guild_id_failing_still_pins_it_for_this_run():
    from portal import setup

    def broken(path, values):
        raise OSError("read-only file system")
    g = _Guild(HOME, owner_id=OWNER)
    bot = _plexbie(_config(), [g])
    real = setup.write_env
    setup.write_env = broken
    try:
        with _Env(), _Logs("bot") as logs:
            asyncio.run(bot._settle_home_guild())
    finally:
        setup.write_env = real
    assert bot.config.guild_id == HOME
    assert any("Couldn't save GUILD_ID" in m and f"GUILD_ID={HOME}" in m for m in logs.at(logging.WARNING))


def test_adoption_drops_a_role_that_is_everyone():
    """The @everyone role's ID is the server's: as the admin role it makes everyone an admin."""
    g = _Guild(HOME, roles=[HOME])
    bot = _plexbie(_config(admin_role_id=HOME), [g])
    with _Env(), _Logs("core.config") as logs:
        asyncio.run(bot._settle_home_guild())
    assert bot.config.guild_id == HOME and bot.config.admin_role_id is None
    assert any("ADMIN_ROLE_ID" in m for m in logs.at(logging.ERROR))


def test_settle_runs_once():
    bot = _plexbie(_config(), [_Guild(HOME, owner_id=OWNER)])
    with _Env():
        asyncio.run(bot._settle_home_guild())
        asyncio.run(bot._settle_home_guild())
    assert len(_syncs(bot)) == 1


def test_public_bot_is_warned_about_at_start():
    bot = _plexbie(_config(guild_id=HOME), [_Guild(HOME)], app=_app(public=True))
    with _Logs("bot") as logs:
        asyncio.run(bot._settle_home_guild())
    assert any("Public Bot" in m and "Install Link" in m for m in logs.at(logging.WARNING))


# --- being added to, or removed from, servers ---

def test_a_foreign_join_is_reported_once_and_never_left():
    from core import notify
    alerts = []
    real = notify.alert_admins_soon
    notify.alert_admins_soon = lambda bot, config, **kw: alerts.append(kw)
    stranger = _Guild(OTHER, name="@everyone **x**", owner_id=31337)
    bot = _plexbie(_config(guild_id=HOME, admin_channel_id=31), [_Guild(HOME, name="The Den"), stranger])
    bot.channels[31] = admin = _Channel()
    try:
        with _Logs("bot") as logs:
            asyncio.run(bot.on_guild_join(stranger))
            asyncio.run(bot.on_guild_join(stranger))
    finally:
        notify.alert_admins_soon = real
    assert stranger.left == 0, "Plexbie never leaves a server by itself: the household may be moving there"
    assert len(admin.sent) == 1, admin.sent
    post = admin.sent[0]
    assert str(OTHER) in post and f"GUILD_ID={OTHER}" in post and "31337" in post
    assert "`@\u200beveryone **x**`" in post, post
    assert len(alerts) == 1 and alerts[0]["url"] == "/manage?tab=health"
    assert "everyone" not in alerts[0]["body"] and str(OTHER) in alerts[0]["body"]
    assert len([m for m in logs.at(logging.WARNING) if str(OTHER) in m]) == 1


def test_foreign_joins_cant_fill_the_admin_channel():
    """Server names are their owners' text (a link, say): shown as code, and only a
    few such posts an hour, however many servers someone adds Plexbie to."""
    import bot as bot_module
    from core import notify
    alerts = []
    real = notify.alert_admins_soon
    notify.alert_admins_soon = lambda bot, config, **kw: alerts.append(kw)
    strangers = [_Guild(OTHER + n, name=f"https://phish.example/{n} `x`") for n in range(6)]
    bot = _plexbie(_config(guild_id=HOME, admin_channel_id=31), [_Guild(HOME), *strangers])
    bot.channels[31] = admin = _Channel()
    try:
        with _Logs("bot") as logs:
            for g in strangers:
                asyncio.run(bot.on_guild_join(g))
    finally:
        notify.alert_admins_soon = real
    limit = bot_module.FOREIGN_NOTICES_PER_HOUR
    assert len(admin.sent) == limit and len(alerts) == limit, (admin.sent, alerts)
    assert "`https://phish.example/0 'x'`" in admin.sent[0], admin.sent[0]
    assert all("phish" not in a["body"] for a in alerts)
    assert len([m for m in logs.at(logging.WARNING) if "another Discord server" in m]) == 6, "the log has them all"


def test_unbound_join_of_a_strangers_server_does_not_adopt():
    """Even one the bot's owner is in: whoever runs it could have added Plexbie."""
    bot = _plexbie(_config(), [])
    with _Env():
        asyncio.run(bot._settle_home_guild())
        stranger = _Guild(OTHER, members=[OWNER], owner_id=77)
        bot.guilds.append(stranger)
        asyncio.run(bot.on_guild_join(stranger))
    assert bot.config.guild_id is None and _syncs(bot) == []
    assert "doesn't own" in bot._home_problem


def test_unbound_join_of_the_owners_only_server_adopts():
    bot = _plexbie(_config(), [])
    with _Env():
        asyncio.run(bot._settle_home_guild())
        mine = _Guild(HOME, owner_id=OWNER)
        bot.guilds.append(mine)
        asyncio.run(bot.on_guild_join(mine))
    assert bot.config.guild_id == HOME and _syncs(bot) == [("sync", HOME, 3)]
    assert bot._home_problem is None


def test_removed_from_home_logs_an_error():
    home = _Guild(HOME, name="The Den")
    bot = _plexbie(_config(guild_id=HOME), [home])
    with _Logs("bot") as logs:
        asyncio.run(bot.on_guild_remove(home))
    assert any("removed from its household's server" in m and str(HOME) in m for m in logs.at(logging.ERROR))


# --- setup asks for the server ---

def test_finish_refuses_without_a_server():
    from test_setup import _env, _scenario

    async def steps(c, s):
        r = await (await c.post("/setup/api/finish", json={"values": {
            "DISCORD_BOT_TOKEN": "t", "PLEX_URL": "http://p:32400", "PLEX_TOKEN": "x"}})).json()
        return r, s.done.is_set()

    answer, done = _scenario(_env(""), steps)
    assert answer == {"ok": False, "missing": ["GUILD_ID"]}
    assert done is False


def test_finish_refuses_a_server_id_that_isnt_a_number():
    from test_setup import _env, _scenario

    async def steps(c, s):
        return await (await c.post("/setup/api/finish", json={"values": {
            "DISCORD_BOT_TOKEN": "t", "GUILD_ID": "my server", "PLEX_URL": "http://p:32400", "PLEX_TOKEN": "x"}})).json()

    assert _scenario(_env(""), steps) == {"ok": False, "missing": ["GUILD_ID"]}


def test_state_lists_guild_id_as_missing():
    from test_setup import _env, _scenario

    async def steps(c, s):
        return (await (await c.get("/setup/api/state")).json())["missing"]

    assert "GUILD_ID" in _scenario(_env(""), steps)


def test_start_up_gate_still_does_not_need_guild_id():
    """Running installs with GUILD_ID blank keep starting (and pick their server
    themselves, or say why not) instead of stopping at the setup page."""
    from portal import setup
    keys = setup.REQUIRED + ("GUILD_ID",)
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        os.environ.update(DISCORD_BOT_TOKEN="t", PLEX_URL="http://p:32400", PLEX_TOKEN="x")
        assert not setup.needs_setup(pathlib.Path(tempfile.mkdtemp()))
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_discord_check_reports_public_bot():
    from test_setup import _env, _scenario

    def run(public):
        async def fake_json(method, url, **kw):
            if url.endswith("/oauth2/applications/@me"):
                return {"id": "77", "name": "Plexbie", "flags": 1 << 15, "bot_public": public,
                        "owner": {"id": str(OWNER)}}
            return []

        async def steps(c, s):
            s._json = fake_json
            return await (await c.post("/setup/api/discord", json={"DISCORD_BOT_TOKEN": "t.o.k"})).json()
        return _scenario(_env(""), steps)

    assert run(True)["publicBot"] is True
    assert run(False)["publicBot"] is False


def test_setup_page_has_no_not_picked_server():
    from portal import setup
    page = setup.PAGE.read_text()
    assert "not picked" not in page
    assert '["Discord server", f("GUILD_ID") ? "✓" : "needed", f("GUILD_ID") ? "ok" : "need"]' in page
    assert "GUILD_ID:" in page.split("const names = {", 1)[1].split("}", 1)[0]
    assert "Install Link" in page and "Public Bot" in page


# --- Manage → Health ---

class _Answer:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.body


class _DiscordApi:
    def __init__(self, body):
        self.body, self.asked = body, []

    def get(self, url, **kw):
        self.asked.append(url)
        return _Answer(200, self.body)


class _HealthBot:
    def __init__(self, guilds, synced=3):
        self.guilds, self._synced = list(guilds), synced

    def is_ready(self):
        return True

    def get_guild(self, gid):
        return next((g for g in self.guilds if g.id == gid), None)


def _health(guild_id=HOME, guilds=(), body=None, sign_in=False, bot=True, synced=3):
    from core.config import Config
    from helpers import FakeServices
    from portal import inbox
    from portal.admin import Admin
    from portal.data import Data
    services = FakeServices(Config())
    cfg = services.config
    cfg.guild_id, cfg.plex_url, cfg.plex_token = guild_id, None, None
    cfg.discord_bot_token = "BOT"
    if sign_in:
        cfg.discord_client_id, cfg.discord_client_secret = "1", "secret"
        cfg.discord_callback_url = "https://home.example.com/auth/discord/callback"
    else:
        cfg.discord_client_id = cfg.discord_client_secret = None
    services.http_session = _DiscordApi(body or {})
    real = inbox.threads_ok
    inbox.threads_ok = lambda bot: None
    try:
        checks = asyncio.run(Admin(Data(services), bot=_HealthBot(guilds, synced) if bot else None).health())
    finally:
        inbox.threads_ok = real
    return {c["name"]: c for c in checks}, services.http_session.asked


def test_health_has_a_discord_server_item():
    home, other = _Guild(HOME, name="The Den"), _Guild(OTHER, name="Stranger Things Fans")

    checks, _ = _health(guilds=[home])
    assert checks["Discord server"]["ok"] is True and "Other Discord servers" not in checks

    checks, _ = _health(guilds=[home], synced=None)
    item = checks["Discord server"]
    assert item["ok"] is False and "applications.commands" in item["detail"], "in the server but without its commands"

    checks, _ = _health(guild_id=None, guilds=[home])
    assert checks["Discord server"]["ok"] is False and "GUILD_ID" in checks["Discord server"]["detail"]

    checks, _ = _health(guilds=[other])
    assert checks["Discord server"]["ok"] is False and str(HOME) in checks["Discord server"]["detail"]

    checks, _ = _health(guilds=[home, other])
    extra = checks["Other Discord servers"]
    assert extra["ok"] is False
    assert "Stranger Things Fans" in extra["detail"] and str(OTHER) in extra["detail"] and "Public Bot" in extra["detail"]


def test_health_flags_public_bot():
    on, asked = _health(body={"bot_public": True, "redirect_uris": ["https://home.example.com/auth/discord/callback"]},
                        sign_in=True, bot=False)
    item = on["Discord Public Bot"]
    assert item["ok"] is False and "Installation" in item["detail"] and "Public Bot" in item["detail"]
    assert on["Discord sign-in"]["ok"] is True
    assert asked == ["https://discord.com/api/v10/applications/@me"], "one request serves both checks"

    off, _ = _health(body={"bot_public": False}, bot=False)
    assert off["Discord Public Bot"]["ok"] is True, "the bot token alone is enough to check"

    unknown, _ = _health(body={}, bot=False)
    assert "Discord Public Bot" not in unknown


# --- the docs say how it works ---

def test_guild_id_is_documented_as_required_and_never_global():
    readme = (ROOT / "README.md").read_text()
    env = (ROOT / "config" / ".env.example").read_text()
    block = env.split("GUILD_ID=", 1)[1].split("\n\n", 1)[0]
    assert "registered globally" not in readme and "register globally" not in readme
    assert "Public Bot" in readme and "Install Link" in readme
    assert "Public Bot" in block
    assert "blank to register" not in block and "globally instead" not in block
