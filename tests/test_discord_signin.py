# path: tests/test_discord_signin.py
"""Signing in to the website with Discord (portal/auth.py).

The sign-in leaves a signed flow cookie holding a random state, and Discord sends
that state back. A callback that doesn't carry this browser's own state, from a
sign-in started here with Discord, is someone else's sign-in being pushed onto
this browser: it must end in "expired" without calling Discord, and only a real
one may leave a session cookie behind.
"""
import asyncio
import pathlib
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

import conftest  # noqa: F401
from aiohttp.test_utils import TestClient, TestServer
from helpers import FakeServices

from core.config import Config
from portal.app import build_app
from portal.auth import FLOW_COOKIE, SESSION_COOKIE, Auth, sign, unsign
from portal.cache import TTLCache

SECRET = "s" * 40


async def _init(path):
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


def _auth():
    cfg = Config()
    cfg.web_session_secret = SECRET
    cfg.discord_client_id, cfg.discord_client_secret = "123", "secret"
    cfg.discord_callback_url = "http://127.0.0.1/auth/discord/callback"
    auth = Auth(None, FakeServices(cfg), TTLCache())
    auth.calls = []

    async def fetch(method, url, **kw):
        auth.calls.append(url)
        if url.endswith("/oauth2/token"):
            return {"access_token": "DISCORD-TOKEN"}
        return {"id": "555", "username": "sam", "global_name": "Sam", "avatar": None}
    auth._fetch_json = fetch

    async def roles(uid):
        return {"member": True, "admin": False, "in_guild": True}
    auth._discord_roles = roles
    return auth


def _client(auth):
    app = build_app(FakeServices(auth.config), who=auth.who, readonly=False, dist=None,
                    image_cache=tempfile.mkdtemp(), auth=auth)
    return TestClient(TestServer(app))


async def _start(client, next_="/"):
    """Begin a sign-in: the state Discord would send back, and the flow cookie holding it."""
    go = await client.get("/auth/discord/login", params={"next": next_}, allow_redirects=False)
    state = parse_qs(urlsplit(go.headers["Location"]).query)["state"][0]
    return state, go.cookies[FLOW_COOKIE].value


def _only(client, name, value):
    """This browser now holds just the one cookie."""
    jar = client.session.cookie_jar
    jar.clear()
    jar.update_cookies({name: value})


def test_a_callback_without_this_browsers_own_discord_state_never_reaches_discord():
    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "d.db")
        auth = _auth()
        client = _client(auth)
        await client.start_server()
        try:
            out = {}
            # Nothing started in this browser: a link someone else made.
            r = await client.get("/auth/discord/callback?state=abc&code=x", allow_redirects=False)
            out["no flow"] = (r.status, r.headers["Location"])

            state, _ = await _start(client)
            r = await client.get("/auth/discord/callback?state=someone-elses&code=x", allow_redirects=False)
            out["wrong state"] = (r.status, r.headers["Location"], r.cookies[FLOW_COOKIE].value)
            await _start(client)
            r = await client.get("/auth/discord/callback?code=x", allow_redirects=False)
            out["no state"] = (r.status, r.headers["Location"])

            # A Plex sign-in's flow cookie, signed by this server, even one carrying
            # a matching state, isn't a Discord sign-in.
            plex = sign(SECRET, {"via": "plex", "pin": 7, "code": "C", "client": "c", "next": "/",
                                 "state": "matching", "typ": FLOW_COOKIE, "exp": int(time.time()) + 600})
            _only(client, FLOW_COOKIE, plex)
            r = await client.get("/auth/discord/callback?state=matching&code=x", allow_redirects=False)
            out["plex flow"] = (r.status, r.headers["Location"])

            # The right state in a cookie that isn't a flow cookie.
            session = sign(SECRET, {"via": "discord", "state": state, "typ": SESSION_COOKIE,
                                    "exp": int(time.time()) + 600})
            _only(client, FLOW_COOKIE, session)
            r = await client.get(f"/auth/discord/callback?state={state}&code=x", allow_redirects=False)
            out["untyped flow"] = (r.status, r.headers["Location"])
            out["session"] = await (await client.get("/api/session")).json()
            return out, auth.calls
        finally:
            await client.close()
    out, calls = asyncio.run(scenario())
    assert out["no flow"] == (302, "/?login=expired")
    assert out["wrong state"] == (302, "/?login=expired", ""), "the flow cookie is cleared"
    assert out["no state"] == (302, "/?login=expired")
    assert out["plex flow"] == (302, "/?login=expired")
    assert out["untyped flow"] == (302, "/?login=expired")
    assert calls == [], "Discord was never asked"
    assert out["session"] is None, "nobody is signed in"


def test_a_discord_sign_in_leaves_a_revocable_session_and_goes_only_to_our_own_pages():
    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "d.db")
        auth = _auth()
        client = _client(auth)
        await client.start_server()
        try:
            state, flow = await _start(client, "/library")
            r = await client.get(f"/auth/discord/callback?state={state}&code=x", allow_redirects=False)
            done = (r.status, r.headers["Location"], r.cookies[FLOW_COOKIE].value,
                    r.cookies[SESSION_COOKIE].value, r.cookies[SESSION_COOKIE]["max-age"])
            me = await (await client.get("/api/session")).json()
            calls = list(auth.calls)

            # The same sign-in again (its flow cookie copied before it was cleared).
            _only(client, FLOW_COOKIE, flow)
            r = await client.get(f"/auth/discord/callback?state={state}&code=x", allow_redirects=False)
            replay = (r.status, r.headers["Location"], SESSION_COOKIE in r.cookies)
            replay_calls = auth.calls[len(calls):]

            state, _ = await _start(client, "//evil.example/x")
            r = await client.get(f"/auth/discord/callback?state={state}&code=x", allow_redirects=False)
            elsewhere = r.headers["Location"]
            return done, me, calls, replay, replay_calls, elsewhere
        finally:
            await client.close()
    done, me, calls, replay, replay_calls, elsewhere = asyncio.run(scenario())
    status, location, flow_left, session, max_age = done
    assert (status, location) == (302, "/library")
    assert flow_left == "", "the flow cookie is spent"
    payload = unsign(SECRET, session)
    assert payload["typ"] == SESSION_COOKIE and payload["via"] == "discord" and payload["id"] == "555"
    assert payload.get("sid"), "a session sign-out can revoke"
    assert "DISCORD-TOKEN" not in str(payload), "Discord's token isn't kept"
    assert int(max_age) == 30 * 86400
    assert calls == ["https://discord.com/api/oauth2/token", "https://discord.com/api/users/@me"]
    assert me["user"]["id"] == "555" and me["user"]["name"] == "Sam" and me["member"]
    assert replay == (302, "/?login=expired", False), "a state is good once"
    assert replay_calls == [], "a replay doesn't reach Discord"
    assert elsewhere == "/", "the page after sign-in is always one of ours"
