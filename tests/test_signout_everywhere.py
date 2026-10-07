# path: tests/test_signout_everywhere.py
"""Manage → Health → "Sign out every other session" (portal/auth.py, portal/mobile.py).

A sign-in someone else made in an admin's name (a Plex Allow pressed on the wrong
page) must be endable without changing WEB_SESSION_SECRET and restarting, and the
phone app's sign-ins, which that secret never covered, must end too. One press by an
admin ends every app sign-in but the presser's own and every website cookie made
before it; the presser stays signed in. Until somebody presses it, nothing changes
for anyone.
"""
import asyncio
import base64
import hashlib
import pathlib
import secrets
import tempfile
import time

import aiohttp
import conftest  # noqa: F401
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from helpers import FakeServices

from core.config import Config
from portal.actions import Actions
from portal.app import build_app
from portal.auth import CONFIRM_COOKIE, CONFIRM_SECONDS, SESSION_COOKIE, Auth, sign, unsign
from portal.cache import TTLCache

SECRET = "s" * 40
PATH = "/api/admin/sign-out-others"
ADMINS = {1}          # Discord ids holding the admin role; everyone here is a member


async def _init(path):
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


def _auth():
    cfg = Config()
    cfg.web_session_secret = SECRET
    auth = Auth(None, FakeServices(cfg), TTLCache())

    async def roles(uid):
        return {"member": True, "admin": uid in ADMINS, "in_guild": True}
    auth._discord_roles = roles
    return auth


def _client(auth):
    services = FakeServices(auth.config)
    app = build_app(services, who=auth.who, readonly=False, dist=None, image_cache=tempfile.mkdtemp(),
                    auth=auth, actions=Actions(None, services, None, ""))
    # Every request carries exactly the cookie or token the test gives it.
    return TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar())


def _cookie(uid: int, sid: str) -> str:
    """A website cookie as one made before this change was: no generation in it."""
    return sign(SECRET, {"via": "discord", "id": str(uid), "name": f"Person {uid}", "sid": sid,
                         "typ": SESSION_COOKIE, "exp": int(time.time()) + 3600})


def _web(cookie: str) -> dict:
    return {"Cookie": f"{SESSION_COOKIE}={cookie}", "X-Plexbie": "1"}


def _app(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Plexbie": "1"}


def _flow() -> tuple:
    """The app's secret and the sign-in it starts with it."""
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, {"challenge": challenge, "state": secrets.token_urlsafe(16), "redirect": "com.plexbie.app:/auth"}


async def _app_sign_in(auth, uid: int) -> str:
    """An app sign-in made the way the app makes one: a code, traded with its secret."""
    verifier, flow = _flow()
    code = auth.mobile.mint_code(flow, "discord", {"id": str(uid), "name": f"Person {uid}"})
    return (await auth.mobile.exchange(code, verifier, "Phone"))["token"]


async def _who(client, headers) -> object:
    r = await client.get("/api/session", headers=headers)
    if r.status != 200:
        return r.status
    body = await r.json()
    return body["user"]["id"] if body else None


def _run(scenario):
    async def go():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "s.db")
        auth = _auth()
        client = _client(auth)
        await client.start_server()
        try:
            return await scenario(auth, client)
        finally:
            await client.close()
    return asyncio.run(go())


def test_a_member_cant_sign_everyone_out():
    async def scenario(auth, client):
        admin, member = _cookie(1, "ada-1"), _cookie(2, "bo-1")
        phone = await _app_sign_in(auth, 1)
        r = await client.post(PATH, headers=_web(member))
        return r.status, await _who(client, _web(admin)), await _who(client, _app(phone)), len(auth.mobile._sessions)
    status, admin, phone, kept = _run(scenario)
    assert status == 403
    assert admin == "1" and phone == "1" and kept == 1, "nobody was signed out"


def test_old_cookies_stop_working_and_the_presser_stays_signed_in():
    async def scenario(auth, client):
        mine, other_admin, member = _cookie(1, "ada-1"), _cookie(1, "ada-2"), _cookie(2, "bo-1")
        phones = [await _app_sign_in(auth, 1), await _app_sign_in(auth, 2)]
        r = await client.post(PATH, headers=_web(mine))
        out = {"status": r.status, "body": await r.json()}
        fresh = r.cookies[SESSION_COOKIE].value
        out["fresh"] = unsign(SECRET, fresh)
        out["old"] = [await _who(client, _web(c)) for c in (mine, other_admin, member)]
        out["phones"] = [await _who(client, _app(t)) for t in phones]
        out["me"] = await _who(client, _web(fresh))
        # A sign-in after the press gets a cookie that works.
        made = web.Response()
        auth._set(made, SESSION_COOKIE, {"via": "discord", "id": "2", "name": "Person 2", "sid": "bo-2"}, 3600)
        out["new sign-in"] = await _who(client, _web(made.cookies[SESSION_COOKIE].value))
        # And it holds after a restart: the press is stored, not only remembered.
        restarted = _client(_auth())
        await restarted.start_server()
        try:
            out["restart old"] = await _who(restarted, _web(other_admin))
            out["restart me"] = await _who(restarted, _web(fresh))
            out["restart phone"] = await _who(restarted, _app(phones[1]))
        finally:
            await restarted.close()
        return out
    out = _run(scenario)
    assert out["status"] == 200 and out["body"]["ok"] is True
    assert out["body"]["ended"] == 2, "both app sign-ins ended (the presser is on the website)"
    assert out["old"] == [None, None, None], "every cookie made before the press is refused, the presser's old one too"
    assert out["phones"] == [401, 401]
    assert out["me"] == "1", "the presser's new cookie works"
    assert out["fresh"]["sid"] == "ada-1" and out["fresh"]["exp"] <= int(time.time()) + 3600, \
        "the same sign-in, no longer than it had left"
    assert out["new sign-in"] == "2"
    assert out["restart old"] is None and out["restart me"] == "1" and out["restart phone"] == 401


def test_from_the_app_only_the_callers_own_app_sign_in_is_kept():
    async def scenario(auth, client):
        cookie = _cookie(1, "ada-1")
        mine, others = await _app_sign_in(auth, 1), [await _app_sign_in(auth, 1), await _app_sign_in(auth, 2)]
        r = await client.post(PATH, headers=_app(mine))
        return (r.status, await r.json(), SESSION_COOKIE in r.cookies, await _who(client, _app(mine)),
                [await _who(client, _app(t)) for t in others], await _who(client, _web(cookie)))
    status, body, set_cookie, me, others, website = _run(scenario)
    assert status == 200 and body["ended"] == 2
    assert not set_cookie, "the app has no cookie to renew"
    assert me == "1" and others == [401, 401]
    assert website is None, "website sign-ins end too"


def test_cookies_made_before_any_press_keep_working():
    """Old cookies carry no generation and count as the first one, which is where the
    stored value starts: installing this signs nobody out."""
    async def scenario(auth, client):
        old = await _who(client, _web(_cookie(2, "bo-1")))
        made = web.Response()
        auth._set(made, SESSION_COOKIE, {"via": "discord", "id": "2", "name": "Person 2", "sid": "bo-2"}, 3600)
        new = await _who(client, _web(made.cookies[SESSION_COOKIE].value))
        restarted = _client(_auth())
        await restarted.start_server()
        try:
            after_restart = await _who(restarted, _web(_cookie(2, "bo-1")))
        finally:
            await restarted.close()
        return old, new, after_restart
    assert _run(scenario) == ("2", "2", "2")


def test_a_cross_site_press_is_refused():
    async def scenario(auth, client):
        cookie = _cookie(1, "ada-1")
        r = await client.post(PATH, headers={"Cookie": f"{SESSION_COOKIE}={cookie}"})
        return r.status, await _who(client, _web(cookie))
    assert _run(scenario) == (403, "1")


BROWSER = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "k" * 40, "auth": "a" * 16}}


def test_alerts_turned_on_elsewhere_stop_too():
    """Alerts belong to an account, not to a sign-in, so those that whoever else signed in
    as an admin turned on (a browser, or a phone through the website) can't be told from
    the admin's own: they all go, except the phones of the app sign-in that pressed."""
    from core import notify

    async def scenario(auth, client):
        mine = await _app_sign_in(auth, 1)
        await notify.subscribe(BROWSER, plex_account_id=None, plex_name=None, discord_id="1")
        await notify.register_app("ExponentPushToken[strangers-phone]", "android", plex_account_id=None,
                                  plex_name=None, discord_id="1")
        await notify.register_app("ExponentPushToken[admins-phone-1]", "ios", plex_account_id=None,
                                  plex_name=None, discord_id="1", session=hashlib.sha256(mine.encode()).hexdigest())
        r = await client.post(PATH, headers=_app(mine))
        browsers = await notify.kv_get_all(notify.SUBS_NAMESPACE)
        phones = [rec["token"] for rec in (await notify.kv_get_all(notify.APP_NAMESPACE)).values()]
        sent = await notify.push_to_admins(discord_ids={"1"}, plex_account_ids=set(), title="t", body="b",
                                           url="/", tag="t")
        return r.status, browsers, phones, sent
    status, browsers, phones, sent = _run(scenario)
    assert status == 200
    assert browsers == {} and sent == 0, "no browser gets the admins' alerts any more"
    assert phones == ["ExponentPushToken[admins-phone-1]"], "only the presser's own phone keeps them"


def test_an_app_sign_in_waiting_for_its_last_tap_cant_be_finished_after_a_press():
    """Someone who got an admin to press Allow holds the "Sign in to the app as …?" page
    for five minutes, and a code from it for one: neither may outlast the press."""
    async def scenario(auth, client):
        verifier, flow = _flow()
        made = web.Response()
        auth._set(made, CONFIRM_COOKIE, {"mobile": flow, "person": {"via": "discord", "id": "1", "name": "Person 1"}},
                  CONFIRM_SECONDS)
        waiting = {"Cookie": f"{CONFIRM_COOKIE}={made.cookies[CONFIRM_COOKIE].value}"}
        before = await client.post("/auth/mobile/confirm", data={"answer": "yes"}, headers=waiting,
                                   allow_redirects=False)
        code = before.headers["Location"].split("code=")[1].split("&")[0]
        r = await client.post(PATH, headers=_web(_cookie(1, "ada-1")))
        page = await client.get("/auth/mobile/confirm", headers=waiting)
        after = await client.post("/auth/mobile/confirm", data={"answer": "yes"}, headers=waiting,
                                  allow_redirects=False)
        traded = await auth.mobile.exchange(code, verifier, "Phone")
        return before.status, r.status, page.status, after.status, after.headers.get("Location"), traded
    before, pressed, page, after, location, traded = _run(scenario)
    assert before == 302 and pressed == 200
    assert (page, after, location) == (410, 410, None), "the waiting page has expired: no new code"
    assert traded is None, "a code made before the press can't be traded after it"
