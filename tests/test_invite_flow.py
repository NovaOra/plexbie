# path: tests/test_invite_flow.py
"""Plex invite approval: persistence and ordering.

Regression coverage for:
  * PlexInviteApprovalView was the only persistent view never registered with
    add_view, and its buttons carried no custom_id - so after any restart
    (including every deploy) clicking Approve returned "This interaction failed"
    and the pending request became silently unactionable.
  * _send_to_admin recorded the admin message id against the saved request, but
    ran BEFORE _save_request created it, so the id was always dropped. Verified
    against the live database: 3 entries, 0 with a message_id.
"""
import inspect
import re

import discord

import conftest  # noqa: F401

from plugins.user_invites.cog import (
    INVITE_MESSAGES_NAMESPACE, INVITES_NAMESPACE, PlexInviteApprovalView,
    UserInvitesCog,
)


def _source(obj) -> str:
    """Source of a function, unwrapping discord.py wrapper objects.

    app_commands.Command stores the function on .callback and tasks.Loop on
    .coro; inspect.getsource rejects the wrappers themselves.
    """
    target = getattr(obj, "callback", None) or getattr(obj, "coro", None) or obj
    return inspect.getsource(target)


def _code(obj) -> str:
    """Source with comment lines removed.

    Ordering assertions below search for literal strings, and a comment that
    explains the bug naturally quotes the very text being searched for. Stripping
    comments keeps those assertions about the code rather than the prose.
    """
    return "\n".join(
        line for line in _source(obj).splitlines()
        if not line.strip().startswith("#")
    )


# --- persistent view ---

def test_view_is_constructible_with_no_arguments():
    """add_view() registers an argument-less instance at startup."""
    view = PlexInviteApprovalView()
    assert view.user_id is None and view.email is None


def test_every_button_has_a_custom_id():
    """add_view raises ValueError on a persistent view whose items lack one."""
    view = PlexInviteApprovalView()
    for child in view.children:
        assert getattr(child, "custom_id", None), f"{child.label} has no custom_id"


def test_custom_ids_are_distinct_and_stable():
    view = PlexInviteApprovalView()
    ids = [c.custom_id for c in view.children]
    assert len(set(ids)) == len(ids)
    assert set(ids) == {"plex_invite_approve", "plex_invite_deny"}


def test_view_is_persistent():
    assert PlexInviteApprovalView().timeout is None


def test_discord_accepts_it_as_persistent():
    """The real check discord.py performs when registering a persistent view."""
    view = PlexInviteApprovalView()
    assert view.is_persistent(), (
        "is_persistent() is False, so bot.add_view() would raise ValueError"
    )


def test_cog_load_registers_the_view():
    """cog_load, not setup(): the plugin loader never calls setup()."""
    source = _source(UserInvitesCog.cog_load)
    assert "add_view(PlexInviteApprovalView())" in source, (
        "without this, pending approval messages stop working after a restart"
    )


def test_both_callbacks_recover_state_before_acting():
    for name in ("approve", "deny"):
        source = _source(getattr(PlexInviteApprovalView, name))
        assert "_ensure_loaded" in source, f"{name} does not recover view state"


# --- message id persistence ---

def test_request_is_saved_before_admins_are_notified():
    """The ordering bug: the id was recorded against a record that did not exist."""
    source = _code(UserInvitesCog.join_plex)
    save_at = source.find("_save_request(")
    notify_at = source.find("_send_to_admin(")
    assert save_at != -1 and notify_at != -1
    assert save_at < notify_at, (
        "_send_to_admin records the message id onto the saved request, so the "
        "request must be saved first"
    )


def test_message_record_is_written_unconditionally():
    # The posting moved into post_join_request so the website shares it with
    # /join-plex; the cog's _send_to_admin must still go through it.
    import plugins.user_invites.cog as invites

    assert "post_join_request(" in _code(UserInvitesCog._send_to_admin)
    source = _code(invites.post_join_request)
    assert f"kv_set({INVITE_MESSAGES_NAMESPACE}" in source.replace(
        "INVITE_MESSAGES_NAMESPACE", INVITE_MESSAGES_NAMESPACE
    ) or "INVITE_MESSAGES_NAMESPACE" in source
    # It must not be gated on an existing record, which is what silently failed.
    assert "await kv_set(INVITE_MESSAGES_NAMESPACE, str(message.id)" in source


def test_message_ids_use_a_separate_namespace():
    """user_mgmt iterates plex_invites and reads every key as a Discord user id.

    Putting message ids there would make auto_link_users try to link them as users.
    """
    assert INVITE_MESSAGES_NAMESPACE != INVITES_NAMESPACE

    import plugins.user_mgmt.cog as user_mgmt

    source = _source(user_mgmt.UserMgmtCog.auto_link_users)
    assert "int(discord_id_str)" in source, (
        "if this stops treating keys as user ids, revisit the namespace split"
    )


# --- duplicate flow guard ---

def test_cog_tracks_in_progress_flows():
    cog = object.__new__(UserInvitesCog)
    UserInvitesCog.__init__(cog, bot=None, services=None)
    assert isinstance(cog._in_progress, set)


def test_join_plex_guards_and_always_releases():
    source = _code(UserInvitesCog.join_plex)
    assert "in self._in_progress" in source, "no guard against a concurrent flow"
    assert "finally:" in source and "_in_progress.discard" in source, (
        "the guard must be released in a finally, or a user could never retry"
    )


def test_dm_is_sent_before_the_user_is_told_to_check_dms():
    source = _code(UserInvitesCog.join_plex)
    dm_at = source.find("join-plex email collection prompt")
    told_at = source.find("Check your DMs!")
    assert dm_at != -1 and told_at != -1
    assert dm_at < told_at, (
        "telling the user to check DMs before sending one leaves them waiting for "
        "a message that may never arrive"
    )


def test_blocked_dm_tells_the_user():
    source = _code(UserInvitesCog.join_plex)
    forbidden = source.split("except discord.Forbidden:")[1]
    assert "followup.send" in forbidden, (
        "a blocked DM was only logged, so the user got no feedback at all"
    )



# --- persistent views must register from a hook the loader actually calls ---

def test_plugin_loader_never_calls_setup():
    """The premise of the test below, asserted rather than assumed.

    core.plugin_manager imports the module with fromlist=["setup"] but then
    instantiates the cog class and calls add_cog itself - it never invokes
    setup(). Anything placed there is dead code in this bot.
    """
    from core.plugin_manager import PluginManager

    source = _source(PluginManager.load_plugin).replace('fromlist=["setup"]', "")
    assert "setup(" not in source
    assert "cog_class(self.bot" in source


def test_no_persistent_view_is_registered_only_in_setup():
    """Registering in setup() means the buttons silently die on every restart.

    This is how PlexInviteApprovalView, AdminApprovalView and
    BookAdminApprovalView all ended up unregistered.
    """
    import pathlib

    root = pathlib.Path(conftest.PROJECT_ROOT)
    offenders = []
    for path in sorted(root.glob("plugins/*/cog.py")):
        in_setup = False
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if line.startswith("async def setup"):
                in_setup = True
            elif line and not line[0].isspace() and not line.startswith("async def setup"):
                in_setup = False
            if in_setup and "add_view(" in line and not line.strip().startswith("#"):
                offenders.append(f"{path.relative_to(root)}:{number}")
    assert offenders == [], (
        "move these to cog_load or __init__, which add_cog actually triggers: "
        + str(offenders)
    )


def test_every_persistent_view_is_registered_somewhere_reachable():
    """Each timeout=None view must be registered from __init__ or cog_load."""
    import pathlib
    import re

    root = pathlib.Path(conftest.PROJECT_ROOT)
    missing = []
    for path in sorted(root.glob("plugins/*/cog.py")):
        text = path.read_text()
        if "timeout=None" not in text:
            continue
        registered = re.findall(r"add_view\(", text)
        if not registered:
            missing.append(path.relative_to(root).as_posix())
    assert missing == [], f"persistent views never registered in: {missing}"


def test_approving_still_finishes_when_discord_refuses_the_role():
    """A fresh server's roles sit beside Plexbie's own, so giving Plex Member failed
    with Missing Permissions - after the Plex invite had gone out - and the whole
    approval stopped: not marked approved, not tracked, the website showed an error."""
    import asyncio
    import discord
    from core.config import Config
    from helpers import FakeServices
    view = PlexInviteApprovalView(user_id=42, email="pat@example.com", services=FakeServices(Config()))
    view.services.config.plex_member_role_id = 7
    done = {}

    async def invite():
        return True, "pat"

    async def track(member, name, plex_id=None):
        done["tracked"] = name

    async def status(s):
        done["status"] = s
    view._send_plex_invite, view._add_to_user_tracking, view._set_status = invite, track, status

    class Resp:
        status, reason = 403, "Forbidden"

    class Member:
        name = display_name = "Pat"

        async def add_roles(self, *a, **k):
            raise discord.Forbidden(Resp(), "Missing Permissions")

    class Role:
        name = "Plex Member"

    class Guild:
        me = None

        def get_member(self, uid):
            return Member()

        def get_role(self, rid):
            return Role()

    class Bot:
        def get_user(self, uid):
            return None

    result = asyncio.run(view._approve_core(Bot(), Guild()))
    assert result["ok"] and done == {"tracked": "pat", "status": "approved"}
    assert "Server Settings > Roles" in result["warning"] and "Plex Member" in result["warning"]


def test_roles_beside_or_above_the_bots_own_are_out_of_reach():
    from core.role_order import out_of_reach
    roles = {"1": {"name": "Plex Member", "position": 1}, "2": {"name": "New on Plex", "position": 3},
             "3": {"name": "Plexbie Admin", "position": 5}}
    assert out_of_reach([1], roles, ["1", "2", "3"]) == ["Plex Member", "New on Plex", "Plexbie Admin"]
    assert out_of_reach([4], roles, ["1", "2", "3", "missing"]) == ["Plexbie Admin"]
    assert out_of_reach([9], roles, ["1", "2", "3"]) == []


def test_plex_tv_tells_an_instant_share_from_a_waiting_invite():
    from plugins.user_invites.cog import share_is_active

    class Share:
        def __init__(self, pending):
            self.pending = pending

    class User:
        def __init__(self, email, pending):
            self.email, self.servers = email, [Share(pending)]

    class Invite:
        def __init__(self, email):
            self.email = email

    class Account:
        def __init__(self, invites, users):
            self._i, self._u = invites, users

        def pendingInvites(self, **kw):
            return self._i

        def users(self):
            return self._u

    back = Account([], [User("Back@Example.com", False)])
    new = Account([Invite("new@example.com")], [])
    waiting = Account([], [User("wait@example.com", True)])
    assert share_is_active(back, "back@example.com")
    assert not share_is_active(new, "new@example.com")
    assert not share_is_active(waiting, "wait@example.com")
    assert not share_is_active(back, "someone@else.com")


def test_someone_shared_straight_away_isnt_told_to_check_their_email():
    """A person removed and re-added got "check your email for the invitation",
    but plex.tv had simply switched their share back on: no email came."""
    import asyncio
    from core.config import Config
    from helpers import FakeServices
    from plugins.user_invites import cog as module
    sent = []

    async def fake_dm(bot, services, user, context=None, embed=None, content=None):
        sent.append(embed)

    for already_on in (True, False):
        view = PlexInviteApprovalView(user_id=42, email="pat@example.com", services=FakeServices(Config()))

        async def invite(v=view, on=already_on):
            v.already_on = on
            return True, "pat"

        async def nothing(*a, **k):
            return None
        view._send_plex_invite, view._add_to_user_tracking, view._set_status = invite, nothing, nothing

        class Bot:
            def get_user(self, uid):
                return object()
        saved = module.send_user_dm
        module.send_user_dm = fake_dm
        try:
            result = asyncio.run(view._approve_core(Bot(), None))
        finally:
            module.send_user_dm = saved
        if already_on:
            assert "no invitation to accept" in result["summary"]
            assert "Check your email" not in sent[-1].description and "open Plex" in sent[-1].description
        else:
            assert "invitation sent" in result["summary"] and "Check your email" in sent[-1].description


def test_a_join_approval_never_relinks_someone_elses_record():
    """Asking to join as <member-name>@anything (or with a member's email) and
    being approved linked the asker's Discord account to that member's record."""
    import asyncio
    import pathlib
    import tempfile
    from core.config import Config
    from helpers import FakeServices
    import database.session as session_module
    from database.session import get_session
    from plugins.user_mgmt.models import PlexUser
    from sqlalchemy import select

    class Member:
        def __init__(self, i, n):
            self.id, self.display_name = i, n

        def __str__(self):
            return self.display_name

    async def scenario():
        session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
        if session_module.engine is not None:
            await session_module.engine.dispose()
        await session_module.init_database(f"sqlite:///{pathlib.Path(tempfile.mkdtemp()) / 'p.db'}")
        async with get_session() as s:
            s.add(PlexUser(plex_username="jane", discord_id=111, plex_user_id=501, plex_email="jane@real.com"))
        view = PlexInviteApprovalView(user_id=999, email="jane@attacker.com", services=FakeServices(Config()))
        note = await view._add_to_user_tracking(Member(999, "Mallory"), "jane")
        stolen_by_email = PlexInviteApprovalView(user_id=999, email="jane@real.com", services=FakeServices(Config()))
        note2 = await stolen_by_email._add_to_user_tracking(Member(999, "Mallory"), "jane", 501)
        async with get_session() as s:
            jane = (await s.execute(select(PlexUser).where(PlexUser.plex_user_id == 501))).scalar_one()
            mallory = (await s.execute(select(PlexUser).where(PlexUser.discord_id == 999))).scalars().all()
        return note, note2, jane.discord_id, [(m.plex_username, m.plex_user_id) for m in mallory]
    note, note2, jane_discord, mallory = asyncio.run(scenario())
    assert jane_discord == 111, "Jane's record keeps Jane's Discord link"
    assert mallory == [("jane@attacker.com", None)], "the asker gets their own record, under their email"
    assert note is None and "already linked to another Discord member" in note2
