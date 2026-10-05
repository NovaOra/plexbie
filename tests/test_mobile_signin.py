# path: tests/test_mobile_signin.py
"""Signing in from the Plexbie app (portal/mobile.py).

The app's sign-in runs in the phone's browser sheet and comes back to the app with
a one-time code, traded with the app's PKCE secret for a Bearer token. A code caught
on the way back, a forged start link, or a token sent from somewhere else must get
nobody anywhere, and the Plex token must never leave the bot.
"""
import asyncio
import base64
import hashlib
import json
import pathlib
import re
import secrets
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

import conftest  # noqa: F401
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from helpers import FakeServices

from core.config import Config
from portal import mobile
from portal.app import build_app
from portal.auth import Auth
from portal.cache import TTLCache

REDIRECT = "com.plexbie.app:/auth"
APP_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{32,512}$")     # what the app accepts (src/api/schemas.ts)


def _pkce():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge, secrets.token_urlsafe(16)


async def _init(path):
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


def _auth(discord=False):
    cfg = Config()
    cfg.web_session_secret = "s" * 40
    if discord:
        cfg.discord_client_id, cfg.discord_client_secret = "123", "secret"
        cfg.discord_callback_url = "http://127.0.0.1/auth/discord/callback"
    auth = Auth(None, FakeServices(cfg), TTLCache())
    auth.forgotten = []

    async def forget(token, client):
        auth.forgotten.append(token)
    auth._forget_device = forget

    async def share():
        return {"ids": {"42"}, "owner": "1"}
    auth._plex_share_info = share

    async def roles(uid):
        return {"member": True, "admin": False, "in_guild": True}
    auth._discord_roles = roles
    return auth


def _client(auth):
    app = build_app(FakeServices(auth.config), who=auth.who, readonly=False, dist=None,
                    image_cache=tempfile.mkdtemp(), auth=auth)
    return TestClient(TestServer(app))


def _start(via, challenge, state, redirect=REDIRECT, **extra):
    from urllib.parse import urlencode
    return "/auth/mobile/start?" + urlencode({"via": via, "challenge": challenge, "state": state, "redirect": redirect, **extra})


# ------------------------------------------------------------ the start link
def test_only_a_proper_pkce_start_for_the_apps_own_address_is_accepted():
    _, challenge, state = _pkce()
    good = {"challenge": challenge, "state": state, "redirect": REDIRECT}
    assert mobile.start_params(good) == good
    for bad in ({**good, "redirect": "https://evil.example/auth"},
                {**good, "redirect": "com.plexbie.app:/auth/../x"},
                {**good, "redirect": "com.plexbie.app://auth"},
                {**good, "challenge_method": "plain"},
                {**good, "challenge": challenge[:-1]},
                {**good, "challenge": challenge[:-1] + "="},
                {**good, "state": "short"},
                {k: v for k, v in good.items() if k != "challenge"},
                {k: v for k, v in good.items() if k != "state"}):
        assert mobile.start_params(bad) is None, bad


def test_a_bad_start_link_is_refused_before_plex_or_discord_and_not_echoed():
    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "m.db")
        client = _client(_auth())
        await client.start_server()
        try:
            _, challenge, state = _pkce()
            bad = await client.get(_start("plex", challenge, state, redirect="https://evil.example/steal"), allow_redirects=False)
            discord = await client.get(_start("discord", challenge, state), allow_redirects=False)   # not set up here
            good = await client.get(_start("plex", challenge, state), allow_redirects=False)
            return ((bad.status, await bad.text(), bad.cookies), discord.status,
                    (good.status, good.headers, good.cookies))
        finally:
            await client.close()
    bad, discord, good = asyncio.run(scenario())
    assert bad[0] == 400 and "evil.example" not in bad[1] and not bad[2]
    assert discord == 400, "a sign-in this server doesn't offer is refused"
    status, headers, cookies = good
    assert status == 200 and "plexbie_plex" in cookies
    assert headers["Cache-Control"] == "no-store" and headers["Referrer-Policy"] == "same-origin"


def test_too_many_starts_from_one_address_go_back_to_the_app_as_busy():
    async def scenario():
        auth = _auth()
        _, challenge, state = _pkce()
        out = None
        for _ in range(mobile.START_TRIES[0] + 1):
            try:
                out = await auth.mobile_start(make_mocked_request("GET", _start("plex", challenge, state)))
            except web.HTTPFound as e:
                out = e
        return out
    last = asyncio.run(scenario())
    assert isinstance(last, web.HTTPFound)
    assert last.location.startswith(REDIRECT + "?") and parse_qs(urlsplit(last.location).query)["error"] == ["busy"]


# ---------------------------------------------------------- the whole flows
def test_plex_sign_in_from_the_app_ends_in_a_code_then_a_bearer_token_and_never_a_cookie():
    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "m.db")
        auth = _auth()

        async def fetch(method, url, **kw):
            if "/pins/" in url:
                return {"code": "ABCD1234", "authToken": "PLEX-SECRET-TOKEN"}
            return {"id": 42, "username": "pat", "email": "pat@example.com"}
        auth._fetch_json = fetch
        client = _client(auth)
        await client.start_server()
        try:
            verifier, challenge, state = _pkce()
            start = await client.get(_start("plex", challenge, state), allow_redirects=False)
            assert start.status == 200
            pin = await client.post("/auth/plex/pin", headers={"X-Plexbie": "1"}, json={"id": 7, "code": "ABCD1234", "next": "/x"})
            plex_url = (await pin.json())["url"]
            confirm = await client.get("/auth/plex/callback", allow_redirects=False)
            page = await client.get(confirm.headers["Location"])
            page_text, page_csp, page_policy = (await page.text(), page.headers["Content-Security-Policy"],
                                                page.headers["Referrer-Policy"])
            # As a browser sends it from that page: its own origin (see Referrer-Policy above).
            back = await client.post("/auth/mobile/confirm", data={"answer": "yes"}, allow_redirects=False,
                                     headers={"Origin": str(client.make_url("")).rstrip("/")})
            location = back.headers["Location"]
            cookies_after = {c.key for c in client.session.cookie_jar}
            again = await client.post("/auth/mobile/confirm", data={"answer": "yes"}, allow_redirects=False)
            query = parse_qs(urlsplit(location).query)
            exchange = await client.post("/auth/mobile/token", headers={"X-Plexbie": "1"},
                                         json={"code": query["code"][0], "verifier": verifier, "device": "Pixel 9"})
            token = await exchange.json()
            client.session.cookie_jar.clear()
            me = await client.get("/api/session", headers={"Authorization": f"Bearer {token['token']}", "X-Plexbie": "1"})
            return (plex_url, confirm, page_text, page_csp, page_policy, again.status, back, location, query, cookies_after,
                    exchange, token, await me.json(), auth)
        finally:
            await client.close()

    (plex_url, confirm, page, csp, page_policy, again, back, location, query, cookies, exchange, token, me,
     auth) = asyncio.run(scenario())
    assert confirm.status == 302 and confirm.headers["Location"] == "/auth/mobile/confirm", \
        "plex.tv may sign someone in without a click, so the app sign-in is confirmed here first"
    assert "Sign in to the Plexbie app as <b>pat</b>?" in page and "frame-ancestors 'none'" in csp
    assert page_policy == "same-origin", "under no-referrer the Sign in button posts Origin: null and is refused"
    assert again == 410, "one tap, one code"
    forward = parse_qs(plex_url.split("#!?", 1)[1])["forwardUrl"][0]
    assert forward.endswith("/auth/plex/callback"), "the sheet comes back to the whole-page callback, not the popup"
    assert back.status == 302 and location.startswith(REDIRECT + "?")
    assert set(query) == {"code", "state"}
    assert "PLEX-SECRET-TOKEN" not in location and "pat" not in location
    assert "plexbie_session" not in cookies, "the sheet gets no web session"
    assert back.headers["Referrer-Policy"] == "same-origin" and back.headers["Cache-Control"] == "no-store"
    assert exchange.status == 200 and exchange.headers["Cache-Control"] == "no-store"
    assert set(token) == {"token", "expiresAt"} and APP_TOKEN.match(token["token"])
    assert "PLEX-SECRET-TOKEN" not in json.dumps(token)
    assert token["expiresAt"] > time.time() + 86400 * 30
    assert me["user"]["id"] == "42" and me["user"]["via"] == "plex" and me["member"]
    assert auth.forgotten == ["PLEX-SECRET-TOKEN"], "Plexbie still signs itself out of their Plex account"


def test_discord_sign_in_from_the_app_ends_in_a_code_and_a_refusal_says_denied():
    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "m.db")
        auth = _auth(discord=True)

        async def fetch(method, url, **kw):
            if url.endswith("/oauth2/token"):
                return {"access_token": "DISCORD-TOKEN"}
            return {"id": "555", "username": "sam", "avatar": None}
        auth._fetch_json = fetch
        client = _client(auth)
        await client.start_server()
        try:
            verifier, challenge, state = _pkce()
            go = await client.get(_start("discord", challenge, state), allow_redirects=False)
            discord_state = parse_qs(urlsplit(go.headers["Location"]).query)["state"][0]
            confirm = await client.get(f"/auth/discord/callback?state={discord_state}&code=x", allow_redirects=False)
            assert confirm.headers["Location"] == "/auth/mobile/confirm" and "plexbie_session" not in confirm.cookies
            back = await client.post("/auth/mobile/confirm", data={"answer": "yes"}, allow_redirects=False)
            code = parse_qs(urlsplit(back.headers["Location"]).query)
            exchange = await client.post("/auth/mobile/token", headers={"X-Plexbie": "1"},
                                         json={"code": code["code"][0], "verifier": verifier})

            _, challenge2, state2 = _pkce()
            go2 = await client.get(_start("discord", challenge2, state2), allow_redirects=False)
            s2 = parse_qs(urlsplit(go2.headers["Location"]).query)["state"][0]
            denied = await client.get(f"/auth/discord/callback?state={s2}&error=access_denied", allow_redirects=False)

            _, challenge3, state3 = _pkce()
            go3 = await client.get(_start("discord", challenge3, state3), allow_redirects=False)
            s3 = parse_qs(urlsplit(go3.headers["Location"]).query)["state"][0]
            await client.get(f"/auth/discord/callback?state={s3}&code=y", allow_redirects=False)
            cancelled = await client.post("/auth/mobile/confirm", data={"answer": "no"}, allow_redirects=False)
            assert parse_qs(urlsplit(cancelled.headers["Location"]).query) == {"error": ["denied"], "state": [state3]}
            return go, back, code, state, exchange.status, denied.headers["Location"], state2
        finally:
            await client.close()

    go, back, code, state, status, denied, state2 = asyncio.run(scenario())
    assert go.status == 302 and go.headers["Location"].startswith("https://discord.com/oauth2/authorize?")
    assert go.headers["Referrer-Policy"] == "same-origin", "the start link (and its challenge) isn't passed to Discord"
    assert back.headers["Location"].startswith(REDIRECT + "?") and code["state"] == [state]
    assert "plexbie_session" not in back.cookies
    assert status == 200
    assert parse_qs(urlsplit(denied).query) == {"error": ["denied"], "state": [state2]}


# ------------------------------------------------------------- the code itself
def _minted(sessions, challenge):
    return sessions.mint_code({"challenge": challenge, "state": "s" * 22, "redirect": REDIRECT},
                              "plex", {"id": "42", "name": "pat", "email": None, "avatar": None})


def test_a_code_works_once_only_with_the_apps_secret_and_only_for_a_minute():
    async def scenario():
        sessions = mobile.MobileSessions()
        sessions._sessions = {}

        async def no_save():
            return None
        sessions._save = no_save
        verifier, challenge, _ = _pkce()

        wrong_first = _minted(sessions, challenge)
        wrong = await sessions.exchange(wrong_first, secrets.token_urlsafe(32))
        right_after_wrong = await sessions.exchange(wrong_first, verifier)

        late = _minted(sessions, challenge)
        sessions._codes[mobile._hash(late)]["exp"] = time.time() - 1
        expired = await sessions.exchange(late, verifier)

        short = await sessions.exchange(_minted(sessions, challenge), "a" * 42)
        made_up = await sessions.exchange(secrets.token_urlsafe(32), verifier)

        code = _minted(sessions, challenge)
        first, second = await asyncio.gather(sessions.exchange(code, verifier), sessions.exchange(code, verifier))
        return wrong, right_after_wrong, expired, short, made_up, first, second, sessions

    wrong, right_after_wrong, expired, short, made_up, first, second, sessions = asyncio.run(scenario())
    assert wrong is None and right_after_wrong is None, "a code gets one try"
    assert expired is None and short is None and made_up is None
    assert (first is None) != (second is None), "two racing exchanges: exactly one wins"
    winner = first or second
    assert APP_TOKEN.match(winner["token"])
    assert all(mobile.CODE.fullmatch(c) is None for c in sessions._codes), "codes are kept only as hashes"
    assert winner["token"] not in json.dumps(sessions._sessions), "tokens are kept only as hashes"


def test_a_code_used_again_ends_the_session_it_made():
    async def scenario():
        sessions = mobile.MobileSessions()
        sessions._sessions = {}

        async def no_save():
            return None
        sessions._save = no_save
        verifier, challenge, _ = _pkce()
        code = _minted(sessions, challenge)
        made = await sessions.exchange(code, verifier)
        alive = sessions.lookup(made["token"]) is not None
        replay = await sessions.exchange(code, verifier)
        return alive, replay, sessions.lookup(made["token"])
    alive, replay, after = asyncio.run(scenario())
    assert alive and replay is None and after is None


def test_the_token_answer_is_the_same_for_every_kind_of_bad_code():
    async def scenario():
        auth = _auth()
        auth.mobile._sessions = {}
        verifier, challenge, _ = _pkce()
        code = _minted(auth.mobile, challenge)
        answers = []
        for body in ({"code": code, "verifier": "x" * 43}, {"code": code, "verifier": verifier},
                     {"code": "nope", "verifier": verifier}, {}, "not an object"):
            request = make_mocked_request("POST", "/auth/mobile/token", headers={"X-Plexbie": "1"})

            async def body_of(body=body):
                return body
            request.json = body_of
            resp = await auth.mobile_token(request)
            answers.append((resp.status, resp.text))
        no_header = await auth.mobile_token(make_mocked_request("POST", "/auth/mobile/token"))
        return answers, no_header.status
    answers, no_header = asyncio.run(scenario())
    assert len(set(answers)) == 1 and answers[0][0] == 400
    assert no_header == 403


# ---------------------------------------------------------------- the token
def _with_session(auth):
    verifier, challenge, _ = _pkce()
    code = _minted(auth.mobile, challenge)
    return asyncio.run(auth.mobile.exchange(code, verifier))["token"]


def test_the_token_works_only_as_a_bearer_header_with_x_plexbie():
    auth = _auth()
    auth.mobile._sessions = {}
    token = _with_session(auth)

    def session(headers):
        return auth.session(make_mocked_request("GET", "/api/session", headers=headers))
    assert session({"Authorization": f"Bearer {token}", "X-Plexbie": "1"})["id"] == "42"
    assert session({"Authorization": f"bearer {token}", "X-Plexbie": "1"}) is not None
    assert session({"Authorization": f"Bearer {token}"}) is None, "not without the header other sites can't send"
    assert session({"Cookie": f"plexbie_session={token}", "X-Plexbie": "1"}) is None, "never as a cookie"
    assert session({"Authorization": f"Bearer {token}x", "X-Plexbie": "1"}) is None


def test_an_unused_or_old_app_session_ends():
    auth = _auth()
    auth.mobile._sessions = {}
    token = _with_session(auth)
    record = next(iter(auth.mobile._sessions.values()))
    headers = {"Authorization": f"Bearer {token}", "X-Plexbie": "1"}
    record["last"] = int(time.time()) - mobile.APP_IDLE_DAYS * 86400 - 5
    assert auth.session(make_mocked_request("GET", "/", headers=headers)) is None
    token = _with_session(auth)
    record = next(iter(auth.mobile._sessions.values()))
    record["exp"] = int(time.time()) - 1
    assert auth.session(make_mocked_request("GET", "/", headers={**headers, "Authorization": f"Bearer {token}"})) is None


def test_a_bad_token_is_a_401_everywhere_and_sign_out_ends_a_good_one():
    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "m.db")
        auth = _auth()
        await auth.mobile.load()
        verifier, challenge, _ = _pkce()
        token = (await auth.mobile.exchange(_minted(auth.mobile, challenge), verifier))["token"]
        good = {"Authorization": f"Bearer {token}", "X-Plexbie": "1"}
        client = _client(auth)
        await client.start_server()
        try:
            out = {}
            for path in ("/api/session", "/api/requests", "/img/tmdb/w342/abc.jpg"):
                out[path] = (await client.get(path, headers={"Authorization": "Bearer pxa_" + "A" * 43, "X-Plexbie": "1"})).status
            out["malformed"] = (await client.get("/api/session", headers={"Authorization": "Bearer junk", "X-Plexbie": "1"})).status
            out["no header"] = (await client.get("/api/session", headers={"Authorization": f"Bearer {token}"})).status
            out["basic"] = (await client.get("/api/session", headers={"Authorization": "Basic dTpw"})).status
            out["info"] = await (await client.get("/api/mobile")).json()
            out["good"] = (await client.get("/api/session", headers=good)).status
            # A fresh bot (a restart) still knows the session: it's kept in the database.
            fresh = _auth()
            await fresh.mobile.load()
            out["after restart"] = fresh.session(make_mocked_request("GET", "/", headers=good)) is not None
            await auth.logout(make_mocked_request("POST", "/api/logout", headers=good))
            out["after logout"] = (await client.get("/api/session", headers=good)).status
            fresh = _auth()
            await fresh.mobile.load()
            out["logout kept"] = fresh.session(make_mocked_request("GET", "/", headers=good)) is None
            return out
        finally:
            await client.close()
    out = asyncio.run(scenario())
    assert out["/api/session"] == out["/api/requests"] == out["/img/tmdb/w342/abc.jpg"] == 401
    assert out["malformed"] == 401 and out["no header"] == 401
    assert out["basic"] == 200, "other Authorization schemes aren't the app's"
    assert out["info"] == {"version": mobile.API_VERSION, "auth": ["plex"], "push": [], "home": None}
    assert out["good"] == 200 and out["after restart"]
    assert out["after logout"] == 401 and out["logout kept"]


def test_the_confirm_page_escapes_the_name_and_needs_its_own_cookie():
    async def scenario():
        auth = _auth()
        _, challenge, state = _pkce()
        pending = {"mobile": {"challenge": challenge, "state": state, "redirect": REDIRECT},
                   "person": {"via": "discord", "id": "5", "name": "<img src=x onerror=alert(1)>"}}
        auth._cookie = lambda request, name: pending if name == "plexbie_app_signin" else None
        page = await auth.mobile_confirm_page(make_mocked_request("GET", "/auth/mobile/confirm"))
        auth._cookie = lambda request, name: None
        nothing = await auth.mobile_confirm_page(make_mocked_request("GET", "/auth/mobile/confirm"))
        return page.text, nothing.status
    page, nothing = asyncio.run(scenario())
    assert "<img" not in page and "&lt;img" in page
    assert nothing == 410


def test_one_account_signing_in_over_and_over_only_replaces_its_own_app_sessions():
    async def scenario():
        sessions = mobile.MobileSessions()
        sessions._sessions = {}

        async def no_save():
            return True
        sessions._save = no_save
        verifier, challenge, _ = _pkce()
        other = sessions.mint_code({"challenge": challenge, "state": "s" * 22, "redirect": REDIRECT},
                                   "discord", {"id": "7", "name": "household member"})
        await sessions.exchange(other, verifier)
        for _ in range(mobile.PER_PERSON + 15):
            await sessions.exchange(_minted(sessions, challenge), verifier)
        return sessions._sessions
    stored = asyncio.run(scenario())
    mine = [r for r in stored.values() if r["id"] == "42"]
    assert len(mine) == mobile.PER_PERSON
    assert any(r["id"] == "7" for r in stored.values()), "someone else's sign-in is untouched"


def test_an_unreadable_store_is_unavailable_not_signed_out_and_nothing_is_lost():
    async def scenario():
        auth = _auth()

        async def broken(*a, **k):
            raise RuntimeError("database is locked")
        mobile.kv_get, saved_get = broken, mobile.kv_get
        try:
            verifier, challenge, _ = _pkce()
            code = _minted(auth.mobile, challenge)
            request = make_mocked_request("POST", "/auth/mobile/token", headers={"X-Plexbie": "1"})

            async def body():
                return {"code": code, "verifier": verifier}
            request.json = body
            first = (await auth.mobile_token(request)).status
            client = _client(auth)
            await client.start_server()
            try:
                api = (await client.get("/api/session", headers={"Authorization": "Bearer pxa_" + "A" * 43,
                                                                 "X-Plexbie": "1"})).status
            finally:
                await client.close()
        finally:
            mobile.kv_get = saved_get
        auth.mobile._sessions = {}                   # the database is back
        retried = await auth.mobile.exchange(code, verifier)
        return first, api, retried
    first, api, retried = asyncio.run(scenario())
    assert first == 503 and api == 503
    assert retried is not None, "the code wasn't spent by the failed attempt"


def test_sign_out_says_so_when_it_could_not_be_stored():
    async def scenario():
        auth = _auth()
        auth.mobile._sessions = {}

        async def saved():
            return True
        auth.mobile._save = saved
        verifier, challenge, _ = _pkce()
        token = (await auth.mobile.exchange(_minted(auth.mobile, challenge), verifier))["token"]

        async def failed():
            return False
        auth.mobile._save = failed
        out = await auth.logout(make_mocked_request("POST", "/api/logout",
                                                    headers={"Authorization": f"Bearer {token}", "X-Plexbie": "1"}))
        return out.status
    assert asyncio.run(scenario()) == 503


def test_a_strange_retry_count_is_not_a_crash():
    async def scenario():
        auth = _auth()
        _, challenge, state = _pkce()
        auth._cookie = lambda request, name: {"via": "plex", "pin": 7, "code": "C", "client": "c",
                                              "mobile": {"challenge": challenge, "state": state, "redirect": REDIRECT}}

        async def fetch(method, url, **kw):
            return {"code": "C", "authToken": None}
        auth._fetch_json = fetch
        out = []
        for tries in ("\u00b2", "1", "99999999999999999999"):
            try:
                out.append((await auth.plex_callback(make_mocked_request("GET", f"/auth/plex/callback?try={tries}"))).status)
            except web.HTTPFound as e:
                out.append(parse_qs(urlsplit(e.location).query).get("error"))
        return out
    assert asyncio.run(scenario()) == [["denied"], 200, ["denied"]]


# ------------------------------------------------------- invites and App Links
class _Invites:
    """One live invite, code INVITE-CODE, key k1."""

    async def find(self, token):
        return ("k1", {"label": "Sam", "created_by": "Omar"}) if token == "INVITE-CODE" else None

    async def usable(self, key):
        return ("k1", {"label": "Sam", "created_by": "Omar", "expires_at": None}) if key == "k1" else None


def test_an_invite_opened_in_the_app_is_accepted_during_its_plex_sign_in():
    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "m.db")
        auth = _auth()
        auth.invites, used = _Invites(), []

        async def fetch(method, url, **kw):
            if "/pins/" in url:
                return {"code": "ABCD1234", "authToken": "PLEX-SECRET-TOKEN"}
            return {"id": 77, "username": "sam", "email": "sam@example.com"}

        async def use_invite(key, me, token):
            used.append((key, me["username"], token))
            return "ok"
        auth._fetch_json, auth._use_invite = fetch, use_invite
        client = _client(auth)
        await client.start_server()
        try:
            check = await client.post("/api/invite/check", headers={"X-Plexbie": "1"}, json={"token": "INVITE-CODE"})
            bad = await client.post("/api/invite/check", headers={"X-Plexbie": "1"}, json={"token": "nope"})
            verifier, challenge, state = _pkce()
            await client.get(_start("plex", challenge, state, invite="INVITE-CODE"), allow_redirects=False)
            await client.post("/auth/plex/pin", headers={"X-Plexbie": "1"}, json={"id": 7, "code": "ABCD1234", "next": "/x"})
            await client.get("/auth/plex/callback", allow_redirects=False)
            back = await client.post("/auth/mobile/confirm", data={"answer": "yes"}, allow_redirects=False,
                                     headers={"Origin": str(client.make_url("")).rstrip("/")})
            return await check.json(), await bad.json(), used, parse_qs(urlsplit(back.headers["Location"]).query)
        finally:
            await client.close()
    info, bad, used, query = asyncio.run(scenario())
    assert info["valid"] and info["label"] == "Sam" and info["inviter"] == "Omar" and bad == {"valid": False}
    assert used == [("k1", "sam", "PLEX-SECRET-TOKEN")], "accepted with their own Plex sign-in, as on the website"
    assert query["invite"] == ["ok"] and set(query) == {"code", "state", "invite"}


def test_sign_in_can_return_through_the_sites_verified_link_and_falls_back_to_the_app_scheme():
    cfg_public = "https://plexbie.example"
    mobile.allow_return(cfg_public)
    https_return = cfg_public + mobile.RETURN_PATH
    _, challenge, state = _pkce()
    assert mobile.start_params({"redirect": https_return, "challenge": challenge, "state": state}) is not None
    assert mobile.start_params({"redirect": "https://evil.example" + mobile.RETURN_PATH, "challenge": challenge, "state": state}) is None
    forwarded = mobile.forward_to_app({"code": "C" * 43, "state": state, "invite": "ok", "next": "https://evil.example"})
    assert forwarded.location.startswith(REDIRECT + "?")
    assert set(parse_qs(urlsplit(forwarded.location).query)) == {"code", "state", "invite"}, "nothing else is passed on"


def test_a_moved_plexbie_keeps_old_addresses_for_app_sign_in_and_names_its_home():
    """After moving (WEB_PUBLIC_URL new, WEB_ALIASES old), a phone still signing in
    through the old address may come back there, and /api/mobile names the new one so
    the app moves itself."""
    import os
    from core.config import _aliases
    assert _aliases(" old.example, https://old.example/ ,http://lan:7979,, plexbie.com") == \
        ["https://old.example", "https://plexbie.com"], "https only, no duplicates"
    saved = {k: os.environ.get(k) for k in ("WEB_PUBLIC_URL", "WEB_ALIASES")}
    os.environ["WEB_PUBLIC_URL"], os.environ["WEB_ALIASES"] = "https://home.moved.example", "https://moved.example"
    try:
        auth = _auth()
        _, challenge, state = _pkce()
        for base in ("https://home.moved.example", "https://moved.example"):
            assert mobile.start_params({"redirect": base + mobile.RETURN_PATH, "challenge": challenge, "state": state}), base
        assert mobile.start_params({"redirect": "https://elsewhere.example" + mobile.RETURN_PATH,
                                    "challenge": challenge, "state": state}) is None

        async def scenario():
            client = _client(auth)
            await client.start_server()
            try:
                return await (await client.get("/api/mobile")).json()
            finally:
                await client.close()
        assert asyncio.run(scenario())["home"] == "https://home.moved.example"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_profile_pictures_follow_discord_and_plex_as_they_change():
    """The picture by your name is the one Discord or Plex has now, not the one you
    signed in with: its address changes with it, so the website and the app refresh."""
    auth = _auth()

    class User:
        def __init__(self, key):
            self.avatar = type("A", (), {"key": key})() if key else None

    class Bot:
        live = "b" * 32

        def get_user(self, uid):
            return User(self.live)
    auth.bot = Bot()
    thumb = {"v": "https://plex.tv/users/3016a4115baba975/avatar?c=1700000000"}

    async def share():
        return {"ids": {"42"}, "owner": "1", "thumbs": {"42": thumb["v"]}}
    auth._plex_share_info = share
    assert auth._discord_avatar_hash(7, "a" * 32) == "b" * 32, "Discord's current picture, not the sign-in one"
    Bot.live = None
    assert auth._discord_avatar_hash(7, "a" * 32) == "a" * 32 and auth._discord_avatar_hash(7, "nope") is None
    first = asyncio.run(auth._plex_avatar(42))
    thumb["v"] = "https://plex.tv/users/3016a4115baba975/avatar?c=1800000000"
    second = asyncio.run(auth._plex_avatar(42))
    assert first.startswith("/img/plex-avatar/42/") and first != second, "a new picture is a new address"
    thumb["v"] = "https://evil.example/avatar.png"
    assert asyncio.run(auth._plex_avatar(42)) is None, "only plex.tv pictures are fetched"
    assert asyncio.run(auth._plex_avatar(99)) is None
