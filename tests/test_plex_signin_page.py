# path: tests/test_plex_signin_page.py
"""Whole-page "Sign in with Plex" (/auth/plex/login, then /auth/plex/callback).

The link used where the small window can't open: a blocked popup, and the
invite page. The browser makes the PIN, plex.tv sends this same tab back, and
only the browser that made the PIN may finish it: another browser naming the
same PIN, before or after it's approved, gets no session.
"""
import asyncio
import json
import pathlib
import tempfile

import aiohttp
import conftest  # noqa: F401
from aiohttp.test_utils import TestClient, TestServer
from helpers import FakeServices

from core.config import Config
from portal.app import build_app
from portal.auth import Auth, unsign
from portal.cache import TTLCache

SECRET = "s" * 40
WINDOW = {"X-Plexbie": "1"}


async def _init(path):
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


class _PlexTv:
    """plex.tv as far as sign-in goes: PINs made by a browser, each readable only
    with the device id that made it (any other gets a 404, unless `any_device`),
    and an account for whoever approves one."""

    def __init__(self):
        self.pins = {}
        self.calls = []
        self.any_device = False

    def make_pin(self, client, pin_id=5, code="abcd"):
        self.pins[pin_id] = {"client": client, "code": code, "token": None}
        return pin_id, code

    def approve(self, pin_id=5):
        self.pins[pin_id]["token"] = "PLEX-SECRET-TOKEN"

    async def fetch(self, method, url, headers=None, **kw):
        self.calls.append((method, url))
        headers = headers or {}
        if "/pins/" in url:
            pin = self.pins.get(int(url.rsplit("/", 1)[1]))
            if not pin or (pin["client"] != headers.get("X-Plex-Client-Identifier") and not self.any_device):
                raise aiohttp.ClientResponseError(None, (), status=404)
            return {"code": pin["code"], "authToken": pin["token"]}
        if url.endswith("/user") and headers.get("X-Plex-Token") == "PLEX-SECRET-TOKEN":
            return {"id": 77, "username": "pat", "email": "pat@example.com"}
        raise aiohttp.ClientResponseError(None, (), status=401)


def _auth():
    cfg = Config()
    cfg.web_session_secret = SECRET
    auth = Auth(None, FakeServices(cfg), TTLCache())
    auth.plex = _PlexTv()
    auth._fetch_json = auth.plex.fetch
    auth.forgotten = []

    async def forget(token, client):
        auth.forgotten.append(token)
    auth._forget_device = forget
    return auth


def _client(auth):
    app = build_app(FakeServices(auth.config), who=auth.who, readonly=False, dist=None,
                    image_cache=tempfile.mkdtemp(), auth=auth)
    return TestClient(TestServer(app))


def _params(page: str) -> dict:
    """What the sign-in page hands its script: the device id, where to post the PIN..."""
    return json.loads(page.split("var p=", 1)[1].split(",d={};", 1)[0])


def _session(response):
    morsel = response.cookies.get("plexbie_session")
    return unsign(SECRET, morsel.value) if morsel is not None and morsel.value else None


def _cleared(response, name):
    morsel = response.cookies.get(name)
    return morsel is not None and not morsel.value


async def _start(browser, auth, next_="/app/requests", pin_id=5, code="abcd"):
    """One browser's whole-page sign-in up to plex.tv: the page, its PIN, and the
    plex.tv address it's sent on to. Returns (login, pin answer, client id)."""
    login = await browser.get("/auth/plex/login", params={"next": next_}, allow_redirects=False)
    client = _params(await login.text())["client"]
    auth.plex.make_pin(client, pin_id, code)
    pin = await browser.post("/auth/plex/pin", json={"id": pin_id, "code": code, "next": next_}, headers=WINDOW)
    return login, pin, client


def _run(scenario):
    async def go():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        auth = _auth()
        client = _client(auth)
        await client.start_server()
        try:
            return await scenario(auth, client)
        finally:
            await client.close()
    return asyncio.run(go())


def test_the_login_page_starts_a_sign_in_in_this_browser_and_the_pin_sets_the_flow_cookie():
    async def scenario(auth, c):
        login, pin, client = await _start(c, auth)
        page = await login.text()
        second = _params(await (await c.get("/auth/plex/login", allow_redirects=False)).text())["client"]
        return login, page, await pin.json(), pin, client, second

    login, page, answer, pin, client, second = _run(scenario)
    assert login.status == 200 and login.headers["Cache-Control"] == "no-store"
    assert "plexbie_plex" in login.cookies and "plexbie_flow" not in login.cookies
    assert "plexbie_session" not in login.cookies
    params = _params(page)
    assert params["post"] == "/auth/plex/pin" and params["next"] == "/app/requests"
    assert client != second, "each sign-in gets its own device id"
    assert pin.status == 200 and "plexbie_flow" in pin.cookies and _cleared(pin, "plexbie_plex")
    assert answer["url"].startswith("https://app.plex.tv/auth/#!?")
    forward = answer["url"].split("#!?", 1)[1]
    assert "code=abcd" in forward and f"clientID={client}" in forward
    assert "callback" in forward and "popup" not in forward, "plex.tv sends this same tab back"


def test_a_callback_with_no_sign_in_under_way_goes_back_as_expired():
    async def scenario(auth, c):
        bare = await c.get("/auth/plex/callback", allow_redirects=False)
        # The page was opened but no PIN made yet (script blocked, plex.tv down).
        await c.get("/auth/plex/login", allow_redirects=False)
        started = await c.get("/auth/plex/callback", allow_redirects=False)
        return bare, started, auth.plex.calls

    bare, started, calls = _run(scenario)
    for response in (bare, started):
        assert response.status == 302 and response.headers["Location"] == "/?login=expired"
        assert _session(response) is None
    assert not calls, "nothing to ask plex.tv about"


def test_a_pin_not_approved_yet_goes_back_as_cancelled_with_no_session():
    async def scenario(auth, c):
        await _start(c, auth)
        back = await c.get("/auth/plex/callback", allow_redirects=False)
        return back, {k.key for k in c.session.cookie_jar}

    back, cookies = _run(scenario)
    assert back.status == 302 and back.headers["Location"] == "/?login=cancelled"
    assert _session(back) is None and "plexbie_session" not in cookies


def test_an_approved_pin_signs_this_tab_in_and_goes_only_to_a_page_on_this_site():
    for asked, lands in (("/app/requests", "/app/requests"), ("//evil.example", "/"),
                         ("https://evil.example/x", "/"), ("/\t/evil.example", "/")):
        async def scenario(auth, c):
            await _start(c, auth, next_=asked)
            auth.plex.approve()
            done = await c.get("/auth/plex/callback", allow_redirects=False)
            await asyncio.sleep(0)                      # the sign-out of their Plex account
            me = await (await c.get("/api/session")).json()
            again = await c.get("/auth/plex/callback", allow_redirects=False)
            return done, me, again, auth.forgotten

        done, me, again, forgotten = _run(scenario)
        assert done.status == 302 and done.headers["Location"] == lands, asked
        person = _session(done)
        assert person and person["via"] == "plex" and person["id"] == "77" and person["name"] == "pat"
        assert "PLEX-SECRET-TOKEN" not in json.dumps(person), "the one-time Plex token is never kept"
        assert _cleared(done, "plexbie_flow")
        assert me["user"]["id"] == "77" and me["user"]["via"] == "plex"
        assert again.headers["Location"] == "/?login=expired", "the sign-in is used up"
        assert forgotten == ["PLEX-SECRET-TOKEN"], "Plexbie signs itself out of their Plex account"


def test_another_browser_naming_the_same_pin_never_gets_a_session():
    """Someone who learns a PIN's number (and even its code) plants it in their own
    browser, and comes back before and after its owner approves it."""
    async def scenario(auth, c):
        url = lambda path: str(c.make_url(path))
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as other:
            async def plant(code):
                other.cookie_jar.clear()
                await other.get(url("/auth/plex/login"))
                await other.post(url("/auth/plex/pin"), json={"id": 5, "code": code}, headers=WINDOW)

            async def come_back():
                return await other.get(url("/auth/plex/callback"), allow_redirects=False)

            out = {}
            await _start(c, auth)                       # the owner's own sign-in, PIN 5
            await plant("zzzz")
            out["early"] = await come_back()
            auth.plex.approve()
            # A wrong code, PIN approved: refused even if plex.tv answered any device.
            auth.plex.any_device = True
            await plant("zzzz")
            out["wrong code"] = await come_back()
            auth.plex.any_device = False
            await plant("abcd")                         # the right code, but not the browser that made it
            out["right code"] = await come_back()
            out["owner"] = await c.get("/auth/plex/callback", allow_redirects=False)
            await plant("zzzz")                         # after the owner is signed in
            out["after"] = await come_back()
            await plant("zzzz")
            out["after, polling"] = await other.get(url("/auth/plex/check"))
            out["polled"] = await out["after, polling"].json()
            out["jar"] = {k.key for k in other.cookie_jar}
            # The owner's own plex.tv link opened in another browser.
            other.cookie_jar.clear()
            out["forwarded"] = await come_back()
            return out

    out = _run(scenario)
    assert out["early"].headers["Location"] == "/?login=failed", "plex.tv won't answer another device"
    assert out["wrong code"].headers["Location"] == "/?login=cancelled", "the PIN's code doesn't match"
    assert out["right code"].headers["Location"] == "/?login=failed", "plex.tv won't answer another device"
    assert out["owner"].headers["Location"] == "/app/requests" and _session(out["owner"])
    # The owner has finished: the once-per-PIN answer only ends the other browser's flow.
    assert _session(out["after"]) is None and _cleared(out["after"], "plexbie_flow")
    assert out["after"].headers["Location"] == "/app/requests", "the first answer's page, nothing more"
    assert _session(out["after, polling"]) is None and out["polled"]["done"] is True
    assert out["polled"]["next"] == "/app/requests"
    assert "plexbie_session" not in out["jar"], "no session left in the other browser"
    assert out["forwarded"].headers["Location"] == "/?login=expired"
    for name in ("early", "wrong code", "right code", "after", "forwarded"):
        assert _session(out[name]) is None, name
