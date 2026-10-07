# path: tests/test_invite_links.py
"""Invite links, from the link to a Plex share (portal/invites.py, portal/auth.py,
plugins/user_invites/cog.py join_with_invite_link).

An invite link puts someone on the household Plex with no approval step, so every
check on the way must hold: a link that stopped working after the page loaded, an
email lock, a guessed or over-long code, a share Plex refused. The person's one-time
Plex sign-in is used to accept the share for them and must never be kept: not in
the database, not in a cookie, not in the log. And whatever goes wrong with the
invite, the sign-in still finishes and Plexbie still signs itself out of their
Plex account.
"""
import asyncio
import json
import logging
import pathlib
import tempfile

import conftest  # noqa: F401
from aiohttp.test_utils import TestClient, TestServer
from helpers import FakeServices

from core.config import Config
from portal.app import build_app
from portal.auth import INVITE_COOKIE, INVITE_TRIES, Auth, unsign
from portal.cache import TTLCache
from portal.invites import NAMESPACE, Invites, status

GUEST_TOKEN = "GUEST-ONE-TIME-PLEX-TOKEN-7f3a9c"
SECRET = "s" * 40


async def _init(folder):
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = folder / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{folder / 'p.db'}")


def _scenario(body):
    async def run():
        folder = pathlib.Path(tempfile.mkdtemp())
        await _init(folder)
        return await body(Invites(), folder)
    return asyncio.run(run())


async def _key(inv, **kw):
    token, _ = await inv.create(**{"label": "Mom", "email": None, "days": 7, "actor": "Admin", **kw})
    return token, (await inv.find(token))[0]


class _Joins:
    """A join() for redeem that records each call."""

    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    async def __call__(self, rec):
        self.calls.append(rec.get("label"))
        return self.ok


ME = {"id": 77, "username": "mom", "email": "mom@example.com"}


# ------------------------------------------------------------ redeem's re-checks
def test_a_link_revoked_after_the_page_loaded_shares_nothing():
    async def body(inv, _):
        _, key = await _key(inv)
        assert (await inv.revoke(key, "Admin"))["ok"]
        join = _Joins()
        return await inv.redeem(key, ME, join), join.calls
    assert _scenario(body) == ("invalid", [])


def test_a_link_that_expired_after_the_page_loaded_shares_nothing():
    from database.kv_store import kv_get, kv_set

    async def body(inv, _):
        _, key = await _key(inv)
        rec = await kv_get(NAMESPACE, key)
        rec["expires_at"] = "2020-01-01T00:00:00+00:00"
        await kv_set(NAMESPACE, key, rec)
        join = _Joins()
        return await inv.redeem(key, ME, join), join.calls
    assert _scenario(body) == ("invalid", [])


def test_an_unreadable_expiry_counts_as_expired():
    from database.kv_store import kv_get, kv_set
    for broken in ({"expires_at": "next tuesday"}, {"expires_at": None}, {"expires_at": 12}, {}):
        assert status(broken) == "expired", broken

    async def body(inv, _):
        _, key = await _key(inv)
        rec = await kv_get(NAMESPACE, key)
        rec["expires_at"] = "soon"
        await kv_set(NAMESPACE, key, rec)
        join = _Joins()
        return await inv.redeem(key, ME, join), join.calls
    assert _scenario(body) == ("invalid", [])


def test_the_email_lock_ignores_case_and_spaces_and_needs_an_email():
    async def body(inv, _):
        token, key = await _key(inv, email="mom@example.com")
        join = _Joins()
        none = await inv.redeem(key, {**ME, "email": None}, join)
        blank = await inv.redeem(key, {**ME, "email": "  "}, join)
        unspent = await inv.find(token) is not None
        calls = list(join.calls)
        loose = await inv.redeem(key, {**ME, "email": " MOM@Example.com "}, join)
        return none, blank, unspent, calls, loose, join.calls
    assert _scenario(body) == ("email", "email", True, [], "ok", ["Mom"])


def test_an_email_locked_invite_waits_until_plex_says_the_email_is_confirmed():
    """Anyone can open a Plex account with an address that isn't theirs, so a
    forwarded email-locked link would work for whoever registered it first."""
    async def body(inv, _):
        token, key = await _key(inv, email="mom@example.com")
        join = _Joins()
        waiting = await inv.redeem(key, {**ME, "confirmed": False}, join)
        unspent, calls = await inv.find(token) is not None, list(join.calls)
        confirmed = await inv.redeem(key, {**ME, "confirmed": True}, join)
        # plex.tv not saying either way changes nothing, so a change on its side
        # can't stop every locked invite.
        _, quiet_key = await _key(inv, email="mom@example.com")
        quiet = await inv.redeem(quiet_key, ME, join)
        # An invite with no lock never depended on the email.
        _, open_key = await _key(inv)
        unlocked = await inv.redeem(open_key, {**ME, "confirmed": False}, join)
        return waiting, unspent, calls, confirmed, quiet, unlocked
    assert _scenario(body) == ("unconfirmed", True, [], "ok", "ok", "ok")


def test_a_share_that_worked_counts_even_if_marking_the_invite_used_fails():
    from portal import invites as module

    async def body(inv, _):
        _, key = await _key(inv)
        saved = module.kv_set

        async def broken(*a, **k):
            raise RuntimeError("database is locked")
        module.kv_set = broken
        try:
            return await inv.redeem(key, ME, _Joins())
        finally:
            module.kv_set = saved
    assert _scenario(body) == "ok", "they were shared with; telling them it failed sends them round again"


# --------------------------------------------------------- the link and the limit
def _auth(services=None):
    if services is None:
        cfg = Config()
        cfg.web_session_secret = SECRET
        services = FakeServices(cfg)
    auth = Auth(None, services, TTLCache())
    auth.invites = Invites()
    auth.forgotten = []

    async def forget(token, client):
        auth.forgotten.append(token)
    auth._forget_device = forget

    async def share():
        return {"ids": {"42"}, "owner": "1"}
    auth._plex_share_info = share
    return auth


def _site(auth):
    app = build_app(FakeServices(auth.config), who=auth.who, readonly=False, dist=None,
                    image_cache=tempfile.mkdtemp(), auth=auth)
    return TestClient(TestServer(app))


def _web(body):
    async def run():
        folder = pathlib.Path(tempfile.mkdtemp())
        await _init(folder)
        auth = _auth()
        client = _site(auth)
        await client.start_server()
        try:
            return await body(auth, client, folder)
        finally:
            await client.close()
    return asyncio.run(run())


def test_opening_an_invite_link_keeps_the_code_out_of_the_address_bar_and_history():
    async def body(auth, client, _):
        token, _ = await auth.invites.create(label="Sam", email="sam@example.com", days=7, actor="Omar")
        opened = await client.get(f"/invite/{token}", allow_redirects=False)
        info = await (await client.get("/api/invite")).json()
        bad = await client.get("/invite/" + "x" * 32, allow_redirects=False)
        after_bad = await (await client.get("/api/invite")).json()
        return token, opened, info, bad, after_bad

    token, opened, info, bad, after_bad = _web(body)
    assert opened.status == 302 and opened.headers["Location"] == "/invite"
    assert opened.headers["Referrer-Policy"] == "no-referrer" and opened.headers["Cache-Control"] == "no-store"
    held = opened.cookies[INVITE_COOKIE].value
    assert token not in held and unsign(SECRET, held)["typ"] == INVITE_COOKIE, "the cookie holds the fingerprint, not the code"
    assert info["valid"] and info["label"] == "Sam" and info["inviter"] == "Omar" and info["emailLocked"]
    assert "sam@example.com" not in json.dumps(info), "the page never shows whose email it's locked to"
    assert bad.headers["Location"] == "/invite" and bad.cookies[INVITE_COOKIE].value == ""
    assert after_bad == {"valid": False}


def test_after_too_many_tries_from_one_address_even_a_good_code_is_refused():
    async def body(auth, client, _):
        token, _ = await auth.invites.create(label="Sam", email=None, days=7, actor="Omar")
        h = {"X-Plexbie": "1"}
        first = await (await client.post("/api/invite/check", headers=h, json={"token": token})).json()
        for n in range(INVITE_TRIES[0] - 1):
            await client.post("/api/invite/check", headers=h, json={"token": f"{n:032d}"})
        late = await (await client.post("/api/invite/check", headers=h, json={"token": token})).json()
        opened = await client.get(f"/invite/{token}", allow_redirects=False)
        return first, late, opened.cookies[INVITE_COOKIE].value
    first, late, cookie = _web(body)
    assert first["valid"] is True
    assert late == {"valid": False} and cookie == "", "guessing stops at the limit, right or wrong"


def test_an_over_long_invite_code_is_cut_before_it_is_looked_up():
    async def body(auth, client, _):
        token, _ = await auth.invites.create(label="Sam", email=None, days=7, actor="Omar")
        asked = []
        find = auth.invites.find

        async def recording(code):
            asked.append(code)
            return await find(code)
        auth.invites.find = recording
        h = {"X-Plexbie": "1"}
        long = await (await client.post("/api/invite/check", headers=h, json={"token": token + "A" * 5000})).json()
        good = await (await client.post("/api/invite/check", headers=h, json={"token": token})).json()
        return long, good, [len(a) for a in asked]
    long, good, lengths = _web(body)
    assert long == {"valid": False} and good["valid"]
    assert lengths == [128, 32]


# ------------------------------------------------------------ sharing with Plex
class _Plex:
    """plex.tv and the Plex server, faked where join_with_invite_link reaches them."""

    def __init__(self, invite_fails=False, accept_fails=False):
        self.invite_fails, self.accept_fails = invite_fails, accept_fails
        self.invited, self.guests, self.accepted, self.posted = [], [], [], []
        plex = self

        class Owner:
            username = "omar"

            def inviteFriend(self, **kw):
                if plex.invite_fails:
                    raise RuntimeError("(400) bad_request")
                plex.invited.append(kw)

        class Guest:
            def __init__(self, token=None):
                plex.guests.append(token)

            def acceptInvite(self, owner):
                if plex.accept_fails:
                    raise RuntimeError("(404) not_found")
                plex.accepted.append(owner)

        class Library:
            def sections(self):
                return ["Films", "TV"]

        class Server:
            library = Library()

        class Channel:
            async def send(self, text):
                plex.posted.append(text)

        self.owner, self.guest, self.server, self.channel = Owner(), Guest, Server(), Channel()

    def __enter__(self):
        import plexapi.myplex
        from plugins.user_invites import cog
        self._saved = (cog.owner_account, cog.find_admin_channel, plexapi.myplex.MyPlexAccount)
        cog.owner_account = lambda cfg: self.owner
        cog.find_admin_channel = lambda bot, cfg: self.channel
        plexapi.myplex.MyPlexAccount = self.guest
        return self

    def __exit__(self, *exc):
        import plexapi.myplex
        from plugins.user_invites import cog
        cog.owner_account, cog.find_admin_channel, plexapi.myplex.MyPlexAccount = self._saved


class _Log(logging.Handler):
    """Every log record, at every level, while it's installed."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(message)s"))
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))

    def __enter__(self):
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self)
        return self

    def __exit__(self, *exc):
        root = logging.getLogger()
        root.removeHandler(self)
        root.setLevel(self._level)


def _services():
    cfg = Config()
    cfg.web_session_secret = SECRET
    cfg.plex_token = "OWNER-TOKEN"
    return FakeServices(cfg)


async def _join(services, **kw):
    from plugins.user_invites.cog import join_with_invite_link
    return await join_with_invite_link(None, services, **{
        "plex_name": "sam", "plex_account_id": "77", "email": "sam@example.com", "user_token": GUEST_TOKEN,
        "label": "Sam", "created_by": "Omar", **kw})


async def _tracked():
    from sqlalchemy import select
    from database.session import get_session
    from database.kv_store import kv_get_all
    from plugins.user_invites.cog import WEB_JOINS_NAMESPACE
    from plugins.user_mgmt.models import PlexUser
    async with get_session() as s:
        rows = [(r.plex_username, r.plex_user_id, r.plex_email) for r in (await s.execute(select(PlexUser))).scalars()]
    return rows, await kv_get_all(WEB_JOINS_NAMESPACE)


def _stored(folder) -> bytes:
    """Everything the database holds on disk."""
    return b"".join(p.read_bytes() for p in folder.iterdir() if p.is_file())


def test_an_invite_link_shares_plex_and_accepts_with_their_own_sign_in():
    async def body(_, folder):
        services = _services()
        with _Plex() as plex, _Log() as log:
            services.plex_server = plex.server
            result = await _join(services)
        return result, plex, await _tracked(), log.lines, _stored(folder)
    result, plex, (rows, joins), log, stored = _scenario(body)
    assert result == {"ok": True, "accepted": True}
    assert [(i["user"], i["sections"], i["allowSync"]) for i in plex.invited] == [("sam", ["Films", "TV"], False)]
    assert plex.guests == [GUEST_TOKEN] and plex.accepted == ["omar"], "accepted for them, from the owner who shared"
    assert rows == [("sam", 77, "sam@example.com")], "tracked for inactivity, by Plex account id"
    assert joins["77"]["via"] == "invite link" and joins["77"]["invited_by"] == "Omar"
    assert len(plex.posted) == 1 and "**sam** joined Plex" in plex.posted[0] and "still need" not in plex.posted[0]
    assert GUEST_TOKEN.encode() not in stored, "the one-time Plex token is never stored"
    assert not [line for line in log if GUEST_TOKEN in line], "the one-time Plex token is never logged"


def test_when_accepting_for_them_fails_they_are_still_shared_and_plex_emails_them():
    async def body(_, folder):
        services = _services()
        with _Plex(accept_fails=True) as plex, _Log() as log:
            services.plex_server = plex.server
            result = await _join(services)
        return result, plex, await _tracked(), log.lines, _stored(folder)
    result, plex, (rows, joins), log, stored = _scenario(body)
    assert result == {"ok": True, "accepted": False}
    assert len(plex.invited) == 1 and rows and "77" in joins
    assert "still need to accept" in plex.posted[0]
    assert GUEST_TOKEN.encode() not in stored and not [line for line in log if GUEST_TOKEN in line]


def test_when_plex_refuses_the_share_nothing_is_recorded():
    async def body(_, folder):
        services = _services()
        with _Plex(invite_fails=True) as plex, _Log() as log:
            services.plex_server = plex.server
            result = await _join(services)
        return result, plex, await _tracked(), log.lines
    result, plex, (rows, joins), log = _scenario(body)
    assert result == {"ok": False, "accepted": False}
    assert plex.guests == [] and rows == [] and joins == {} and plex.posted == []
    assert not [line for line in log if GUEST_TOKEN in line]


def test_without_a_way_to_reach_plex_an_invite_link_shares_nothing():
    async def body(_, __):
        services = _services()
        services.plex_server = None
        with _Plex() as plex:
            result = await _join(services)
        return result, plex.invited, await _tracked()
    assert _scenario(body) == ({"ok": False, "accepted": False}, [], ([], {}))


def test_a_share_that_worked_counts_even_if_its_record_cant_be_saved():
    from plugins.user_invites import cog

    async def body(_, __):
        services = _services()
        saved = cog.kv_set

        async def broken(*a, **k):
            raise RuntimeError("database is locked")
        cog.kv_set = broken
        try:
            with _Plex() as plex:
                services.plex_server = plex.server
                result = await _join(services)
        finally:
            cog.kv_set = saved
        return result, len(plex.invited), len(plex.posted)
    assert _scenario(body) == ({"ok": True, "accepted": True}, 1, 1)


# ------------------------------------------------------- Auth._use_invite's answers
def test_someone_already_on_plex_keeps_the_invite_for_whoever_it_was_for():
    from plugins.user_invites import cog

    async def body(inv, _):
        auth = _auth(_services())
        auth.invites = inv
        token, key = await _key(inv)
        calls = []
        saved = cog.join_with_invite_link

        async def join(*a, **k):
            calls.append(k)
            return {"ok": True}
        cog.join_with_invite_link = join
        try:
            outcome = await auth._use_invite(key, {"id": 42, "username": "pat"}, GUEST_TOKEN)
        finally:
            cog.join_with_invite_link = saved
        return outcome, calls, await inv.find(token) is not None
    assert _scenario(body) == ("already", [], True)


def test_only_a_share_that_worked_spends_the_invite():
    from plugins.user_invites import cog

    async def body(inv, _):
        auth = _auth(_services())
        auth.invites = inv
        token, key = await _key(inv)
        saved, out = cog.join_with_invite_link, []
        for result in ({}, {"ok": False, "accepted": False}, {"ok": True, "accepted": False}):
            async def join(*a, r=result, **k):
                assert k["user_token"] == GUEST_TOKEN and k["plex_name"] == "sam" and k["plex_account_id"] == "77"
                return r
            cog.join_with_invite_link = join
            try:
                out.append((await auth._use_invite(key, {"id": 77, "username": " sam "}, GUEST_TOKEN),
                            await inv.find(token) is not None))
            finally:
                cog.join_with_invite_link = saved
        return out
    assert _scenario(body) == [("failed", True), ("failed", True), ("ok", False)]


def test_an_invite_step_that_breaks_answers_failed_instead_of_raising():
    from plugins.user_invites import cog

    async def body(inv, _):
        auth = _auth(_services())
        auth.invites = inv
        token, key = await _key(inv)
        saved = cog.join_with_invite_link

        async def join(*a, **k):
            raise RuntimeError("something inside broke")
        cog.join_with_invite_link = join
        try:
            with _Log() as log:
                outcome = await auth._use_invite(key, {"id": 77, "username": "sam"}, GUEST_TOKEN)
        finally:
            cog.join_with_invite_link = saved
        return outcome, await inv.find(token) is not None, log.lines
    outcome, unspent, log = _scenario(body)
    assert outcome == "failed" and unspent
    assert not [line for line in log if GUEST_TOKEN in line]


# ------------------------------------------------------------- on the website
def _sign_in_with_invite(me, *, lock=None, join_raises=False, revoked=False):
    """Open an invite link, sign in with Plex from it, and see where the site lands."""
    from plugins.user_invites import cog

    async def run():
        folder = pathlib.Path(tempfile.mkdtemp())
        await _init(folder)
        services = _services()
        auth = _auth(services)

        async def fetch(method, url, **kw):
            if "/pins/" in url:
                return {"code": "ABCD1234", "authToken": GUEST_TOKEN}
            return me
        auth._fetch_json = fetch
        client = _site(auth)
        await client.start_server()
        saved = cog.join_with_invite_link

        async def broken(*a, **k):
            raise RuntimeError("something inside broke")
        if join_raises:
            cog.join_with_invite_link = broken
        try:
            with _Plex() as plex, _Log() as log:
                services.plex_server = plex.server
                token, _ = await auth.invites.create(label="Sam", email=lock, days=7, actor="Omar")
                await client.get(f"/invite/{token}", allow_redirects=False)
                await client.get("/auth/plex/go?invite=1")
                await client.post("/auth/plex/pin", headers={"X-Plexbie": "1"},
                                  json={"id": 7, "code": "ABCD1234", "next": "/", "invite": True})
                if revoked:
                    assert (await auth.invites.revoke((await auth.invites.find(token))[0], "Omar"))["ok"]
                done = await client.get("/auth/plex/callback", allow_redirects=False)
                await asyncio.sleep(0)
                jar = {c.key: c.value for c in client.session.cookie_jar}
                unspent = await auth.invites.find(token) is not None
            return (done.status, done.headers.get("Location"), jar, unspent, auth.forgotten, plex,
                    log.lines, _stored(folder))
        finally:
            cog.join_with_invite_link = saved
            await client.close()
    return asyncio.run(run())


SAM = {"id": 77, "username": "sam", "email": "sam@example.com", "confirmed": True}


def _no_token_kept(jar, log, stored):
    assert GUEST_TOKEN.encode() not in stored, "the one-time Plex token is never stored"
    assert not [line for line in log if GUEST_TOKEN in line], "the one-time Plex token is never logged"
    assert GUEST_TOKEN not in json.dumps([unsign(SECRET, v) for v in jar.values()]), "nor put in a cookie"


def test_a_good_invite_signs_in_lands_on_ok_and_forgets_the_invite_cookie():
    status_, location, jar, unspent, forgotten, plex, log, stored = _sign_in_with_invite(SAM)
    assert status_ == 302 and location == "/?invite=ok"
    assert "plexbie_session" in jar and INVITE_COOKIE not in jar
    assert not unspent and len(plex.invited) == 1 and plex.accepted == ["omar"]
    assert forgotten == [GUEST_TOKEN], "Plexbie signs itself out of their Plex account"
    _no_token_kept(jar, log, stored)


def test_a_locked_invite_opened_with_another_account_keeps_the_cookie_for_a_retry():
    status_, location, jar, unspent, forgotten, plex, log, stored = _sign_in_with_invite(
        {**SAM, "email": "someone@else.com"}, lock="sam@example.com")
    assert location == "/?invite=email" and INVITE_COOKIE in jar and "plexbie_session" in jar
    assert unspent and plex.invited == [] and forgotten == [GUEST_TOKEN]
    _no_token_kept(jar, log, stored)


def test_a_locked_invite_with_an_unconfirmed_plex_email_waits_with_the_cookie_kept():
    status_, location, jar, unspent, forgotten, plex, log, stored = _sign_in_with_invite(
        {**SAM, "confirmed": False}, lock="sam@example.com")
    assert location == "/?invite=unconfirmed" and INVITE_COOKIE in jar
    assert unspent and plex.invited == [] and forgotten == [GUEST_TOKEN]
    _no_token_kept(jar, log, stored)


def test_someone_already_on_plex_lands_on_already_and_the_invite_cookie_goes():
    status_, location, jar, unspent, forgotten, plex, log, stored = _sign_in_with_invite({**SAM, "id": 42})
    assert status_ == 302 and location == "/?invite=already"
    assert "plexbie_session" in jar and INVITE_COOKIE not in jar
    assert unspent and plex.invited == [] and forgotten == [GUEST_TOKEN]
    _no_token_kept(jar, log, stored)


def test_a_link_revoked_before_plex_answers_lands_on_invalid_and_the_invite_cookie_goes():
    status_, location, jar, unspent, forgotten, plex, log, stored = _sign_in_with_invite(SAM, revoked=True)
    assert status_ == 302 and location == "/?invite=invalid"
    assert "plexbie_session" in jar and INVITE_COOKIE not in jar
    assert not unspent and plex.invited == [] and forgotten == [GUEST_TOKEN]
    _no_token_kept(jar, log, stored)


def test_a_broken_invite_step_still_signs_in_and_forgets_the_plex_device():
    """It raised straight out of the sign-in: a bare 500, no session, and their
    one-time Plex sign-in left authorised on their plex.tv account."""
    status_, location, jar, unspent, forgotten, plex, log, stored = _sign_in_with_invite(SAM, join_raises=True)
    assert status_ == 302 and location == "/?invite=failed"
    assert "plexbie_session" in jar and INVITE_COOKIE in jar, "signed in, and the invite is kept for a retry"
    assert unspent and forgotten == [GUEST_TOKEN]
    _no_token_kept(jar, log, stored)
