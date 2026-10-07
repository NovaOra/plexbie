# path: tests/test_signin_alerts.py
"""Telling admins about their own new sign-ins (portal/auth.py).

A sign-in can be made in someone's name without them noticing (a Plex Allow pressed
on the wrong page, a Discord account taken over). So a new website or app session for
an admin is told to that admin by Discord DM (when they have Discord) and to every
admin through the alerts, once, with when, how and roughly from what, and never a
token, a cookie or an address. A member's sign-in tells nobody, and nothing about
telling may break the sign-in itself.
"""
import asyncio
import base64
import hashlib
import json
import pathlib
import secrets
import tempfile
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
import conftest  # noqa: F401
from aiohttp.test_utils import TestClient, TestServer
from helpers import FakeServices

from core import admin_mirror, notify
from core.config import Config
from portal.app import build_app
import portal.auth as auth_module
from portal.auth import Auth, rough_app_device
from portal.cache import TTLCache

SECRET = "s" * 40
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
CHOSEN = "Sam's iPhone https://plexbie-signout.example. No action needed"
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")


async def _init(path):
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


def _auth(*, admin: bool):
    cfg = Config()
    cfg.web_session_secret = SECRET
    cfg.discord_client_id, cfg.discord_client_secret = "123", "secret"
    cfg.discord_callback_url = "http://127.0.0.1/auth/discord/callback"
    auth = Auth(None, FakeServices(cfg), TTLCache())
    auth.pins = {}

    async def fetch(method, url, headers=None, **kw):
        if url.endswith("/oauth2/token"):
            return {"access_token": "DISCORD-TOKEN"}
        if url.endswith("/users/@me"):
            return {"id": "555", "username": "sam", "global_name": "Sam", "avatar": None}
        if "/pins/" in url:
            return {"code": "abcd", "authToken": "PLEX-SECRET-TOKEN"}
        return {"id": 77, "username": "pat", "email": "pat@example.com"}
    auth._fetch_json = fetch

    async def roles(uid):
        return {"member": True, "admin": admin, "in_guild": True}
    auth._discord_roles = roles

    async def share():
        # Plex account 77 is the server's owner (an admin), or just on the share list.
        return {"ids": {"77"}, "owner": "77" if admin else "1"}
    auth._plex_share_info = share

    async def forget(token, client):
        pass
    auth._forget_device = forget
    return auth


def _client(auth):
    app = build_app(FakeServices(auth.config), who=auth.who, readonly=False, dist=None,
                    image_cache=tempfile.mkdtemp(), auth=auth)
    return TestClient(TestServer(app))


def _run(scenario, *, admin=True, dm=None, alert=None):
    """Run a sign-in with the DM and the admins' alert caught: (result, dms, alerts)."""
    dms, alerts = [], []

    async def caught_dm(bot, services, user_id, **kw):
        dms.append({"to": str(user_id), **kw})
        return True

    def caught_alert(bot, config, **kw):
        alerts.append(kw)

    real = admin_mirror.dm_user_id, notify.alert_admins_soon
    admin_mirror.dm_user_id, notify.alert_admins_soon = dm or caught_dm, alert or caught_alert

    async def go():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "a.db")
        auth = _auth(admin=admin)
        client = _client(auth)
        await client.start_server()
        try:
            result = await scenario(auth, client)
            for _ in range(5):                       # whatever the sign-in left running
                await asyncio.gather(*list(auth._tasks), return_exceptions=True)
            return result
        finally:
            await client.close()
    try:
        return asyncio.run(go()), dms, alerts
    finally:
        admin_mirror.dm_user_id, notify.alert_admins_soon = real


async def _discord(client, ua=FIREFOX):
    go = await client.get("/auth/discord/login", params={"next": "/library"}, allow_redirects=False)
    state = parse_qs(urlsplit(go.headers["Location"]).query)["state"][0]
    return await client.get(f"/auth/discord/callback?state={state}&code=x", allow_redirects=False,
                            headers={"User-Agent": ua})


async def _plex(client, ua=IPHONE):
    """A Plex sign-in finished twice, as it is: the site's polling and the window's callback."""
    await client.get("/auth/plex/go", allow_redirects=False)
    await client.post("/auth/plex/pin", json={"id": 5, "code": "abcd", "next": "/"}, headers={"X-Plexbie": "1"})
    check = await client.get("/auth/plex/check", headers={"User-Agent": ua})
    window = await client.get("/auth/plex/callback?popup=1", headers={"User-Agent": ua})
    return check, window


def _everything(dms, alerts) -> str:
    return json.dumps([dms, alerts], default=str)


def _no_secrets(text: str, *cookies: str) -> None:
    for secret in ("DISCORD-TOKEN", "PLEX-SECRET-TOKEN", "127.0.0.1", "Mozilla", *cookies):
        assert secret not in text, secret


def test_an_admin_signing_in_with_discord_is_told_by_dm_and_the_admins_once():
    async def scenario(auth, client):
        done = await _discord(client)
        me = await (await client.get("/api/session")).json()
        return done, me

    (done, me), dms, alerts = _run(scenario)
    assert done.status == 302 and done.headers["Location"] == "/library", "the sign-in itself goes ahead"
    assert me["admin"] and me["user"]["id"] == "555"
    assert len(dms) == 1 and dms[0]["to"] == "555", "a DM to the admin who signed in"
    assert len(alerts) == 1, "one alert to the admins"
    dm, alert = dms[0]["content"], alerts[0]["title"] + " " + alerts[0]["body"]
    for text in (dm, alert):
        assert "Discord" in text and "Firefox on Windows" in text
        assert "If this wasn't you, sign out on the website and tell the other admins." in text
        assert "UTC" in text, "when"
    _no_secrets(_everything(dms, alerts), done.cookies["plexbie_session"].value)


def test_the_plex_owner_signing_in_is_told_once_though_the_sign_in_finishes_twice():
    (check, window), dms, alerts = _run(lambda auth, client: _plex(client))
    assert check.status == 200 and window.status == 200
    assert dms == [], "no Discord linked: nobody to DM"
    assert len(alerts) == 1, "one sign-in, one alert"
    text = alerts[0]["title"] + " " + alerts[0]["body"]
    assert "pat" in text and "Plex" in text and "Safari on iPhone" in text
    assert "pat@example.com" not in text
    _no_secrets(_everything(dms, alerts))


def test_an_owner_plex_couldnt_confirm_at_sign_in_is_still_told_once_it_can():
    async def scenario(auth, client):
        answers = iter([None])

        async def share():
            # plex.tv not answering at the sign-in (a fresh install: no owner known yet), then back.
            return next(answers, {"ids": {"77"}, "owner": "77"})
        auth._plex_share_info = share
        return await _plex(client)

    real = auth_module.RECHECK_SECONDS
    auth_module.RECHECK_SECONDS = 0
    try:
        (check, window), dms, alerts = _run(scenario)
    finally:
        auth_module.RECHECK_SECONDS = real
    assert check.status == 200 and window.status == 200
    assert len(alerts) == 1 and "pat" in alerts[0]["title"]


def test_a_member_plex_couldnt_check_tells_nobody_on_the_second_look_either():
    async def scenario(auth, client):
        async def share():
            return None
        auth._plex_share_info = share
        return await _plex(client)

    real = auth_module.RECHECK_SECONDS
    auth_module.RECHECK_SECONDS = 0
    try:
        _, dms, alerts = _run(scenario, admin=False)
    finally:
        auth_module.RECHECK_SECONDS = real
    assert dms == [] and alerts == []


def _pkce():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge, secrets.token_urlsafe(16)


def test_an_admin_signing_in_to_the_app_is_told_when_the_app_gets_its_session():
    async def scenario(auth, client):
        verifier, challenge, state = _pkce()
        await client.get("/auth/mobile/start?" + urlencode({
            "via": "plex", "challenge": challenge, "state": state, "redirect": "com.plexbie.app:/auth"}),
            allow_redirects=False)
        await client.post("/auth/plex/pin", headers={"X-Plexbie": "1"}, json={"id": 7, "code": "abcd"})
        await client.get("/auth/plex/callback", allow_redirects=False)
        back = await client.post("/auth/mobile/confirm", data={"answer": "yes"}, allow_redirects=False,
                                 headers={"Origin": str(client.make_url("")).rstrip("/")})
        code = parse_qs(urlsplit(back.headers["Location"]).query)["code"][0]
        await asyncio.gather(*list(auth._tasks), return_exceptions=True)
        before = len(caught)
        token = await client.post("/auth/mobile/token", headers={"X-Plexbie": "1", "User-Agent": "okhttp/4.12.0"},
                                  json={"code": code, "verifier": verifier, "device": CHOSEN})
        return before, token.status, (await token.json())["token"]

    caught = []
    (before, status, token), dms, alerts = _run(scenario, alert=lambda bot, config, **kw: caught.append(kw))
    assert before == 0, "nothing until the app holds a session"
    assert status == 200
    assert len(caught) == 1
    text = caught[0]["title"] + " " + caught[0]["body"]
    assert "app" in text and "Plex" in text and "an Android phone" in text
    for chosen in ("Sam's", "https", "plexbie-signout", "No action needed"):
        assert chosen not in text, "the device name is whatever the app sent: never repeated"
    _no_secrets(json.dumps(caught), token)


def test_the_app_device_is_named_from_fixed_words_only():
    assert rough_app_device("okhttp/4.12.0") == "an Android phone"
    assert rough_app_device("Plexbie/23 CFNetwork/1498.700.2 Darwin/23.6.0") == "an iPhone or iPad"
    assert rough_app_device("curl/8.7.1") == rough_app_device(None) == "an unknown device"


def test_a_member_signing_in_tells_nobody():
    async def scenario(auth, client):
        done = await _discord(client)
        client.session.cookie_jar.clear()
        await _plex(client)
        return done.status

    status, dms, alerts = _run(scenario, admin=False)
    assert status == 302
    assert dms == [] and alerts == []


def test_a_failure_to_tell_never_breaks_the_sign_in():
    async def broken_dm(*a, **kw):
        raise aiohttp.ClientError("Discord is down")

    def broken_alert(*a, **kw):
        raise RuntimeError("no push service")

    async def scenario(auth, client):
        done = await _discord(client)
        me = await (await client.get("/api/session")).json()

        async def broken_describe(s):
            raise RuntimeError("database gone")
        auth.describe = broken_describe
        client.session.cookie_jar.clear()
        again = await _discord(client)
        return done, me, again

    (done, me, again), _, _ = _run(scenario, dm=broken_dm, alert=broken_alert)
    assert done.status == 302 and done.headers["Location"] == "/library"
    assert done.cookies["plexbie_session"].value and me["user"]["id"] == "555"
    assert again.status == 302 and again.cookies["plexbie_session"].value


def test_a_dm_that_fails_still_leaves_the_admins_told():
    async def broken_dm(*a, **kw):
        raise aiohttp.ClientError("Discord is down")

    done, _, alerts = _run(lambda auth, client: _discord(client), dm=broken_dm)
    assert done.status == 302 and done.cookies["plexbie_session"].value
    assert len(alerts) == 1
