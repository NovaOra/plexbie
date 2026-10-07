# path: tests/test_plex_invites.py
"""Unaccepted Plex invites: change the address or take one back (core/plex_invites)."""
import asyncio
import pathlib
import tempfile

import conftest  # noqa: F401

from core import plex_invites


class Invite:
    def __init__(self, email, invite_id=7):
        self.email, self.username, self.friendlyName, self.createdAt = email, "", "", None
        self.id, self.friend, self.home, self.server = invite_id, False, False, True


class Account:
    def __init__(self, emails):
        self.invites = [Invite(e) for e in emails]
        self.calls = []

    def pendingInvites(self, includeSent=True, includeReceived=False):
        return list(self.invites)

    def cancelInvite(self, invite):
        self.calls.append(("cancel", invite.email))
        self.invites.remove(invite)

    def inviteFriend(self, user, server, sections, allowSync):
        self.calls.append(("invite", user))


class Server:
    class library:
        @staticmethod
        def sections():
            return ["Movies"]


def _with(account):
    original = plex_invites.owner_account
    plex_invites.owner_account = lambda config: account
    return original


def test_changing_the_email_cancels_the_old_invite_and_sends_a_new_one():
    account = Account(["wrong@proton.me", "someone@else.com"])
    original = _with(account)
    try:
        plex_invites.resend(object(), Server(), "Wrong@Proton.me", "right@gmail.com")
        assert plex_invites.cancel(object(), "someone@else.com") is True
        assert plex_invites.cancel(object(), "nobody@here.com") is False
    finally:
        plex_invites.owner_account = original
    assert account.calls == [("cancel", "wrong@proton.me"), ("invite", "right@gmail.com"), ("cancel", "someone@else.com")]


def test_the_records_follow_the_new_address_and_the_person_is_told():
    from database import session as session_module
    from database.kv_store import kv_get, kv_set
    from plugins.user_invites.cog import INVITES_NAMESPACE, correct_invite_email
    from plugins.user_mgmt.models import PlexUser
    from portal.actions import Actions
    from helpers import FakeServices
    from core.config import Config

    db = pathlib.Path(tempfile.mkdtemp()) / "inv.db"
    told = []

    class Bot:
        def get_user(self, uid):
            return None                     # not cached: fetched instead

        async def fetch_user(self, uid):
            return f"user{uid}"

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            await kv_set(INVITES_NAMESPACE, "555", {"email": "wrong@proton.me", "username": "alt", "status": "approved"})
            async with session_module.get_session() as s:
                s.add(PlexUser(discord_id=555, discord_username="alt", plex_username="wrong", plex_email="wrong@proton.me"))
                await s.commit()
            who = await correct_invite_email("wrong@proton.me", "right@gmail.com")
            record = await kv_get(INVITES_NAMESPACE, "555")
            async with session_module.get_session() as s:
                from sqlalchemy import select
                row = (await s.execute(select(PlexUser))).scalars().first()
                tracked = (row.plex_email, row.plex_username)

            import core.admin_mirror as mirror
            original = mirror.send_user_dm

            async def fake_dm(bot, services, member, context=None, content=None, **kw):
                told.append((member, content))
            mirror.send_user_dm = fake_dm
            try:
                actions = Actions(bot=Bot(), services=FakeServices(Config()), data=None, public_url="")
                await actions._tell_invite_moved(who, "wrong@proton.me", "right@gmail.com")
            finally:
                mirror.send_user_dm = original
            return who, record, tracked
        finally:
            await session_module.engine.dispose()

    who, record, tracked = asyncio.run(go())
    assert who["discord_id"] == 555
    assert record["email"] == "right@gmail.com"
    assert tracked == ("right@gmail.com", "right"), "the guessed Plex name follows the new email"
    assert told and told[0][0] == "user555" and "right@gmail.com" in told[0][1]


def test_an_invite_to_an_address_with_no_plex_account_is_cancelled_by_email():
    """Those have no id: plexapi would ask for /requested/nan and Plex says 404."""
    account = Account([])
    account.invites = [Invite("morgan@example.net", invite_id=float("nan"))]
    queried = []
    account.query = lambda url, method=None: queried.append(url)
    account._session = type("S", (), {"delete": "DELETE"})()
    original = _with(account)
    try:
        assert plex_invites.cancel(object(), "morgan@example.net") is True
    finally:
        plex_invites.owner_account = original
    assert queried and queried[0].endswith("/requested/morgan%40example.net?friend=0&home=0&server=1"), queried
    assert not any(c[0] == "cancel" for c in account.calls), "plexapi's id-based cancel isn't used"


def _db():
    from database import session as session_module
    return session_module, pathlib.Path(tempfile.mkdtemp()) / "match.db"


def test_plexbie_learns_which_plex_account_each_person_is():
    from sqlalchemy import select
    from plugins.user_mgmt.cog import match_account, reconcile_accounts
    from plugins.user_mgmt.models import PlexUser
    session_module, db = _db()
    accounts = [
        {"id": 1, "title": "Riverhost", "username": "Riverhost", "email": "owner@x.me", "owner": True},
        {"id": 975, "title": "river975", "username": "river975", "email": "riverhost@example.com", "owner": False},
        {"id": 42, "title": "Morgan Hale", "username": "morganhale", "email": "morgan@example.com", "owner": False},
        {"id": 77, "title": "bob", "username": "bob", "email": "bob@x.me", "owner": False},
    ]

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            async with session_module.get_session() as s:
                s.add_all([
                    PlexUser(plex_username="Riverhost", discord_id=1),                                     # the owner
                    PlexUser(plex_username="riverhost", plex_email="riverhost@example.com", discord_id=2),   # guessed from email
                    PlexUser(plex_username="cait", plex_user_id=42, discord_id=3),                      # known by id
                    PlexUser(plex_username="wrong", plex_email="gone@proton.me", discord_id=4),         # nothing to match
                    PlexUser(plex_username="bob", discord_id=5),
                    PlexUser(plex_username="bobby", plex_email="bob@x.me", discord_id=6),               # would collide
                ])
                await s.commit()
            changes = await reconcile_accounts(accounts)
            async with session_module.get_session() as s:
                rows = {r.discord_id: (r.plex_username, r.plex_email, r.plex_user_id)
                        for r in (await s.execute(select(PlexUser))).scalars()}
            return changes, rows
        finally:
            await session_module.engine.dispose()

    changes, rows = asyncio.run(go())
    assert rows[1][0] == "Riverhost", "the owner's row is left alone"
    assert rows[2] == ("river975", "riverhost@example.com", 975), "matched by email: real name, id"
    assert rows[3] == ("Morgan Hale", "morgan@example.com", 42), "matched by id"
    assert rows[4][0] == "wrong", "nothing to match on: left for the admin to pick"
    assert rows[6][0] == "bobby", "a rename onto someone else's name is refused"
    assert len([c for c in changes if c.startswith("now tracking")]) == 0, "everyone on Plex was already tracked"
    assert len(changes) == 2


def test_everyone_shared_on_plex_is_tracked_whoever_they_are():
    """Shared outside Discord (a website invite, or in Plex directly): still checked for inactivity."""
    from sqlalchemy import select
    from plugins.user_mgmt.cog import reconcile_accounts
    from plugins.user_mgmt.models import PlexUser
    session_module, db = _db()
    accounts = [
        {"id": 1, "title": "Owner", "username": "Owner", "email": "o@x.me", "owner": True},
        {"id": 5, "title": "grandma", "username": "grandma", "email": "gran@x.me", "owner": False},
        {"id": 6, "title": "Alex", "username": "alexk", "email": "alex@x.me", "owner": False},
    ]

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            async with session_module.get_session() as s:
                s.add(PlexUser(plex_username="Alex", discord_id=9))
                await s.commit()
            first = await reconcile_accounts(accounts)
            again = await reconcile_accounts(accounts)
            async with session_module.get_session() as s:
                rows = sorted((r.plex_username, r.plex_user_id, r.discord_id) for r in (await s.execute(select(PlexUser))).scalars())
            return first, again, rows
        finally:
            await session_module.engine.dispose()

    first, again, rows = asyncio.run(go())
    # Alex's row is linked to Discord, so a same-named account isn't taken as them
    # (a Plex name is the holder's to choose); an admin matches them on People.
    assert rows == [("Alex", None, 9), ("grandma", 5, None)], rows
    assert any("now tracking grandma" in c for c in first)
    assert not any("now tracking" in c for c in again), "running it again adds nobody twice"


def test_an_admin_can_say_which_account_someone_is():
    from sqlalchemy import select
    from plugins.user_mgmt.cog import match_account
    from plugins.user_mgmt.models import PlexUser
    session_module, db = _db()

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            async with session_module.get_session() as s:
                s.add_all([PlexUser(plex_username="riverhost", plex_email="riverhost@example.org", discord_id=2),
                           PlexUser(plex_username="taken", discord_id=3)])
                await s.commit()
            await match_account("riverhost", {"id": 975, "title": "river975", "email": "riverhost@example.com"})
            try:
                await match_account("river975", {"id": 9, "title": "taken", "email": ""})
                clash = None
            except ValueError as e:
                clash = str(e)
            async with session_module.get_session() as s:
                row = (await s.execute(select(PlexUser).where(PlexUser.discord_id == 2))).scalar_one()
                return (row.plex_username, row.plex_email, row.plex_user_id), clash
        finally:
            await session_module.engine.dispose()

    row, clash = asyncio.run(go())
    assert row == ("river975", "riverhost@example.com", 975)
    assert clash and "already someone else" in clash


def test_rename_shows_everywhere_and_tells_tautulli_without_resetting_it():
    from sqlalchemy import select
    from plugins.user_mgmt.cog import rename_person
    from plugins.user_mgmt.models import PlexUser
    session_module, db = _db()
    calls = []

    class Taut:
        configured = True

        async def call(self, cmd, **params):
            calls.append((cmd, params))
            return {"keep_history": 0, "allow_guest": 1, "custom_thumb": "x.png"} if cmd == "get_user" else None

    class S:
        tautulli = Taut()

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            async with session_module.get_session() as s:
                s.add_all([PlexUser(plex_username="pat74", plex_user_id=74), PlexUser(plex_username="taken", display_name="Mom")])
                await s.commit()
            note = await rename_person(S(), "pat74", "  Pat   Alt ")
            try:
                await rename_person(S(), "pat74", "mom")
                clash = None
            except ValueError as e:
                clash = str(e)
            async with session_module.get_session() as s:
                named = (await s.execute(select(PlexUser).where(PlexUser.plex_user_id == 74))).scalar_one().display_name
            await rename_person(S(), "pat74", "pat74")
            async with session_module.get_session() as s:
                cleared = (await s.execute(select(PlexUser).where(PlexUser.plex_user_id == 74))).scalar_one().display_name
            return note, clash, named, cleared
        finally:
            await session_module.engine.dispose()

    note, clash, named, cleared = asyncio.run(go())
    assert named == "Pat Alt" and cleared is None
    assert clash and "already called" in clash
    assert "Tautulli shows" in note
    edit = next(p for c, p in calls if c == "edit_user")
    assert edit == {"user_id": 74, "friendly_name": "Pat Alt", "custom_thumb": "x.png", "keep_history": 0, "allow_guest": 1}


def test_a_member_is_recognised_by_account_after_a_plex_username_change():
    from plugins.user_mgmt.models import PlexUser
    from portal.auth import Auth
    session_module, db = _db()

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            async with session_module.get_session() as s:
                s.add(PlexUser(plex_username="river975", plex_user_id=975, discord_id=55))
                await s.commit()
            auth = object.__new__(Auth)
            return await auth._plex_row(plex_name="brand-new-name", plex_id="975")
        finally:
            await session_module.engine.dispose()

    row = asyncio.run(go())
    assert row is not None and row.discord_id == 55


def test_renaming_your_plex_account_to_someone_elses_row_gets_you_nothing():
    """A member renames their plex.tv username to a pending Discord joiner's row name."""
    from sqlalchemy import select
    from plugins.user_mgmt.cog import reconcile_accounts
    from plugins.user_mgmt.models import PlexUser
    from portal.auth import Auth
    session_module, db = _db()
    accounts = [
        {"id": 1, "title": "Owner", "username": "Owner", "email": "o@x.me", "owner": True},
        {"id": 50, "title": "jane.smith", "username": "jane.smith", "email": "mallory@x.me", "owner": False},
        {"id": 60, "title": "kid", "username": "kid", "email": "", "owner": False},
    ]

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            async with session_module.get_session() as s:
                s.add_all([PlexUser(plex_username="jane.smith", plex_email="jane.smith@example.com", discord_id=10),
                           PlexUser(plex_username="kid")])            # nobody attached: a name may fill it in
                await s.commit()
            await reconcile_accounts(accounts)
            async with session_module.get_session() as s:
                rows = {r.plex_username: (r.plex_user_id, r.discord_id) for r in (await s.execute(select(PlexUser))).scalars()}
            row = await object.__new__(Auth)._plex_row(plex_name="jane.smith", plex_id="50")
            return rows, row
        finally:
            await session_module.engine.dispose()

    rows, signed_in_as = asyncio.run(go())
    assert rows["jane.smith"] == (None, 10), "Jane's row keeps no stranger's account"
    assert rows["kid"] == (60, None)
    assert signed_in_as is None, "Mallory signs in as nobody's row, so with no Discord link or admin"


def test_two_rows_on_one_account_are_trusted_for_neither():
    """An older database can already hold two rows on one account: the index waits, sign-in trusts neither."""
    import re
    import sqlite3
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    from sqlalchemy.schema import CreateTable
    from plugins.user_mgmt.models import PlexUser
    from portal.auth import Auth
    session_module, db = _db()
    ddl = str(CreateTable(PlexUser.__table__).compile(dialect=sqlite_dialect.dialect()))
    ddl = re.sub(r",\s*UNIQUE \(plex_user_id\)", "", ddl)
    assert "UNIQUE (plex_user_id)" not in ddl
    con = sqlite3.connect(db)
    con.execute(ddl)
    for name, did in (("a", 1), ("b", 2)):
        con.execute("INSERT INTO plex_users (discord_id, plex_username, plex_user_id, days_inactive, warning_sent, "
                    "is_top_watcher, never_remove, created_at, updated_at) VALUES (?, ?, 7, 0, 0, 0, 0, "
                    "'2026-01-01', '2026-01-01')", (did, name))
    con.commit()
    con.close()

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            return await object.__new__(Auth)._plex_row(plex_id="7")
        finally:
            await session_module.engine.dispose()

    assert asyncio.run(go()) is None


def test_the_auto_linker_never_moves_someone_elses_link():
    """Asking to join with an existing member's email doesn't take over their row."""
    from sqlalchemy import select
    from plugins.user_mgmt.cog import UserMgmtCog
    from plugins.user_mgmt.models import PlexUser
    session_module, db = _db()

    class Bot:
        def get_guild(self, _):
            return None

        def get_channel(self, _):
            return None

    class Config:
        guild_id = 1
        admin_channel_id = None

    class Services:
        config = Config()

    cog = object.__new__(UserMgmtCog)
    cog.bot, cog.services = Bot(), Services()

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            async with session_module.get_session() as s:
                s.add_all([PlexUser(plex_username="victim", plex_user_id=70, discord_id=700),
                           PlexUser(plex_username="newbie", plex_email="new@x.me", discord_id=800)])
                await s.commit()
            stolen = await cog._link_invite(999, {"user_id": 70, "username": "victim"}, "victim@x.me")
            joined = await cog._link_invite(800, {"user_id": 80, "username": "newbie80"}, "new@x.me")
            async with session_module.get_session() as s:
                rows = {r.discord_id: (r.plex_username, r.plex_user_id) for r in (await s.execute(select(PlexUser))).scalars()}
            return stolen, joined, rows
        finally:
            await session_module.engine.dispose()

    stolen, joined, rows = asyncio.run(go())
    assert stolen == ("conflict", "victim") and rows[700] == ("victim", 70) and 999 not in rows
    assert joined == ("linked", "newbie80") and rows[800] == ("newbie80", 80)


def test_only_an_invite_sent_to_an_email_address_can_be_moved():
    """Invites to a Plex username list with no email; moving one of those would
    cancel whichever email-less invite plex.tv lists first."""
    from portal.actions import Actions
    from helpers import FakeServices
    from core.config import Config

    actions = Actions(bot=None, services=FakeServices(Config()), data=None, public_url="")
    actions.services.plex_server = object()
    admin = {"user": {"id": "1", "name": "Sam"}, "admin": True}
    resent = []
    original = plex_invites.resend
    def resend(config, server, old, new):
        resent.append((old, new))
        raise RuntimeError("stopped here")

    plex_invites.resend = resend
    try:
        answers = [asyncio.run(actions.plex_invite_change(admin, body)) for body in (
            {"email": "", "new": "right@gmail.com"},
            {"email": "   ", "new": "right@gmail.com"},
            {"email": "not-an-address", "new": "right@gmail.com"},
            {"email": "a" * 250 + "@x.me", "new": "right@gmail.com"},
            {"email": "wrong@proton.me", "new": "a" * 250 + "@gmail.com"},
        )]
        assert [a["ok"] for a in answers] == [False] * 5, answers
        assert resent == [], "nothing was cancelled or sent on plex.tv"
        # The longest address there can be (254 characters) still goes through.
        longest = "a" * 249 + "@x.me"
        asyncio.run(actions.plex_invite_change(admin, {"email": longest, "new": "right@gmail.com"}))
        asyncio.run(actions.plex_invite_change(admin, {"email": "wrong@proton.me", "new": longest}))
    finally:
        plex_invites.resend = original
    assert resent == [(longest, "right@gmail.com"), ("wrong@proton.me", longest)]


def test_fixing_an_empty_address_changes_no_records():
    from sqlalchemy import select
    from database.kv_store import kv_get_all, kv_set
    from plugins.user_invites.cog import INVITES_NAMESPACE, WEB_JOINS_NAMESPACE, correct_invite_email
    from plugins.user_mgmt.models import PlexUser
    session_module, db = _db()

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            await kv_set(INVITES_NAMESPACE, "555", {"username": "alt", "status": "approved"})
            await kv_set(WEB_JOINS_NAMESPACE, "w1", {"email": "", "plex_name": "kid"})
            async with session_module.get_session() as s:
                s.add(PlexUser(discord_id=555, plex_username="kid", plex_email=""))
                await s.commit()
            who = await correct_invite_email("", "right@gmail.com")
            records = [await kv_get_all(INVITES_NAMESPACE), await kv_get_all(WEB_JOINS_NAMESPACE)]
            async with session_module.get_session() as s:
                row = (await s.execute(select(PlexUser))).scalars().first()
            return who, records, row.plex_email
        finally:
            await session_module.engine.dispose()

    who, records, tracked = asyncio.run(go())
    assert who == {"discord_id": None, "name": None}
    assert records == [{"555": {"username": "alt", "status": "approved"}}, {"w1": {"email": "", "plex_name": "kid"}}]
    assert tracked == ""
