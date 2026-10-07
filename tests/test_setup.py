# path: tests/test_setup.py
"""The first-start setup page (portal/setup.py)."""
import asyncio
import json
import os
import pathlib
import tempfile

import conftest  # noqa: F401
from aiohttp.test_utils import TestClient, TestServer

from portal import setup

KEYS = list(setup.FIELDS)


def _env(text: str) -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp())
    p = d / ".env"
    p.write_text(text)
    return p


def _scenario(env_file, steps, problem=None, code=True):
    """Run `steps` against a setup page; the client sends the setup code from the
    log unless code=False."""
    saved = {k: os.environ.pop(k, None) for k in KEYS + sorted(setup.SERVER_MADE)}
    try:
        async def go():
            s = setup.Setup(env_file, problem)
            client = TestClient(TestServer(s.app()), headers={"X-Setup-Code": s.code} if code else None)
            await client.start_server()
            try:
                return await steps(client, s)
            finally:
                await client.close()
        return asyncio.run(go())
    finally:
        for k in saved:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


def test_write_env_keeps_comments_and_order_and_appends_new_keys():
    p = _env("# top\nDISCORD_BOT_TOKEN=\n# why\nPLEX_URL=http://localhost:32400\n#PLEX_TOKEN=commented\n")
    setup.write_env(p, {"PLEX_URL": "http://10.0.0.5:32400", "DISCORD_BOT_TOKEN": "abc.def", "TMDB_API_KEY": "k"})
    lines = p.read_text().splitlines()
    assert lines[:5] == ["# top", "DISCORD_BOT_TOKEN=abc.def", "# why", "PLEX_URL=http://10.0.0.5:32400", "#PLEX_TOKEN=commented"]
    assert lines[-1] == "TMDB_API_KEY=k"
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_values_with_spaces_or_hashes_are_quoted_so_dotenv_reads_them_back():
    from dotenv import dotenv_values
    p = _env("SMTP_FROM=\n")
    setup.write_env(p, {"PLEX_PASSWORD": "pa ss#word", "SMTP_FROM": "Plexbie <p@x.y>"})
    v = dotenv_values(p)
    assert v["PLEX_PASSWORD"] == "pa ss#word"
    assert v["SMTP_FROM"] == "Plexbie <p@x.y>"


def test_setup_is_needed_until_discord_and_plex_are_set():
    saved = {k: os.environ.pop(k, None) for k in setup.REQUIRED}
    try:
        assert setup.needs_setup()
        os.environ.update(DISCORD_BOT_TOKEN="t", PLEX_URL="http://p:32400")
        assert setup.needs_setup()
        os.environ["PLEX_TOKEN"] = "x"
        assert not setup.needs_setup()
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_saved_secrets_are_never_sent_back_and_a_blank_secret_keeps_the_saved_one():
    p = _env("DISCORD_BOT_TOKEN=\nPLEX_URL=\n")

    async def steps(c, s):
        r = await c.post("/setup/api/save", json={"values": {"DISCORD_BOT_TOKEN": "secret.token", "PLEX_URL": "http://p:32400"}})
        state = await r.json()
        assert state["fields"]["DISCORD_BOT_TOKEN"] == {"set": True, "value": None}
        assert state["fields"]["PLEX_URL"] == {"set": True, "value": "http://p:32400"}
        assert "secret.token" not in await (await c.get("/setup/api/state")).text()
        await c.post("/setup/api/save", json={"values": {"DISCORD_BOT_TOKEN": "", "PLEX_URL": "http://q:32400"}})
        return p.read_text()

    text = _scenario(p, steps)
    assert "DISCORD_BOT_TOKEN=secret.token" in text and "PLEX_URL=http://q:32400" in text


def test_only_known_settings_can_be_written():
    p = _env("")

    async def steps(c, s):
        await c.post("/setup/api/save", json={"values": {"DB_URL": "sqlite:////etc/x", "WEB_PORT": "1"}})
        return p.read_text()

    assert "DB_URL" not in _scenario(p, steps)


def test_finish_lists_what_is_missing_then_lets_start_up_continue():
    p = _env("")

    async def steps(c, s):
        first = await (await c.post("/setup/api/finish", json={"values": {"DISCORD_BOT_TOKEN": "t"}})).json()
        assert first == {"ok": False, "missing": ["GUILD_ID", "PLEX_URL", "PLEX_TOKEN"]}
        assert not s.done.is_set()
        ok = await (await c.post("/setup/api/finish", json={"values": {
            "GUILD_ID": "42", "PLEX_URL": "http://p:32400", "PLEX_TOKEN": "x"}})).json()
        await asyncio.wait_for(s.done.wait(), 3)
        return ok

    assert _scenario(p, steps) == {"ok": True}


def test_every_page_is_the_setup_page_and_a_problem_is_shown():
    p = _env("")

    async def steps(c, s):
        page = await (await c.get("/app/requests")).text()
        state = await (await c.get("/setup/api/state")).json()
        bad = await c.get("/brand/..%2F..%2Fsetup.py")
        return page, state, bad.status

    page, state, status = _scenario(p, steps, problem="Discord turned down the bot token.")
    assert "Set up Plexbie" in page
    assert state["problem"] == "Discord turned down the bot token."
    assert status == 404


def test_more_lists_the_rest_of_the_template_but_not_local_ports_or_paths():
    p = _env("")

    async def steps(c, s):
        return await (await c.get("/setup/api/more")).json()

    d = _scenario(p, steps)
    keys = {k["key"] for sec in d["sections"] for k in sec["keys"]}
    assert {"NZBHYDRA_URL", "SONARR_WEBHOOK_SECRET", "SMTP_HOST", "LOG_LEVEL", "BOOKSHELF_EBOOK_WATCH"} <= keys
    assert not keys & (set(setup.FIELDS) | setup.LOCAL | setup.HIDDEN)
    secret = {k["key"]: k["secret"] for sec in d["sections"] for k in sec["keys"]}
    assert secret["NZBHYDRA_API_KEY"] and secret["SMTP_PASSWORD"] and not secret["NZBHYDRA_URL"]
    titles = [sec["title"] for sec in d["sections"]]
    assert "Book downloads" in titles and "Webhook listener" in titles
    assert "Integrations" not in titles and "Stats displays" not in titles, "all asked on earlier steps"


def test_an_old_env_is_imported_without_its_ports_paths_or_lan_address():
    p = _env("WEB_PORT=7979\n")
    old = ("DISCORD_BOT_TOKEN=tok\nDISCORD_GUILD_ID=42\nSTATS_CHANNEL_ID=7\nWEB_PORT=8102\nWEBHOOK_PORT=8081\n"
           "DB_URL=sqlite:////elsewhere.db\nWEB_PUBLIC_URL=http://192.168.1.20:8102\nPUID=99\nSMTP_FROM='Plexbie <a@b.c>'\n")

    async def steps(c, s):
        d = await (await c.post("/setup/api/import", json={"text": old})).json()
        return d, p.read_text()

    d, text = _scenario(p, steps)
    assert "DISCORD_BOT_TOKEN=tok" in text and "GUILD_ID=42" in text and "STATS_CHANNEL_ID=7" in text
    assert "WEB_PORT=7979" in text and "8102" not in text and "8081" not in text and "elsewhere" not in text
    assert {"WEB_PORT", "WEBHOOK_PORT", "DB_URL", "WEB_PUBLIC_URL", "PUID"} <= set(d["skipped"])
    assert "Plexbie <a@b.c>" in text


# ---- website "Sign in with Plex": small window + polling (portal/auth.py)

class _FakeHttp:
    """plex.tv: a PIN that's approved once `approved` is set."""

    def __init__(self):
        self.approved = False
        self.calls = []

    def request(self, method, url, **kw):
        http = self
        self.calls.append((method, url))

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            async def json(self, **kw):
                if "/pins?" in url or url.endswith("/pins"):
                    return {"id": 5, "code": "abcd"}
                if "/pins/5" in url:
                    return {"id": 5, "code": "abcd", "authToken": "plex-token" if http.approved else None}
                if url.endswith("/user"):
                    return {"id": 77, "username": "pat"}
                return {}
        return _Resp()

    def __getattr__(self, method):
        return lambda url, **kw: self.request(method, url, **kw)


def test_plex_sign_in_in_a_small_window_is_finished_by_polling():
    from aiohttp import web
    from core.config import Config
    from helpers import FakeServices
    from portal.auth import Auth
    from portal.cache import TTLCache

    async def go():
        cfg = Config()
        cfg.web_session_secret = "s" * 32
        services = FakeServices(cfg)
        services.http_session = _FakeHttp()
        auth = Auth(None, services, TTLCache())
        auth._forget_device = lambda token, client: asyncio.sleep(0)   # no plex.tv in tests
        app = web.Application()
        app.router.add_get("/auth/plex/go", auth.plex_go)
        app.router.add_post("/auth/plex/pin", auth.plex_pin)
        app.router.add_get("/auth/plex/check", auth.plex_check)
        app.router.add_get("/auth/plex/callback", auth.plex_callback)
        c = TestClient(TestServer(app))
        await c.start_server()
        try:
            page = await (await c.get("/auth/plex/go?next=%2Fapp%2Frequests")).text()
            # The browser makes the PIN (Plex refuses one made from another address).
            assert "https://plex.tv/api/v2/pins?strong=true" in page and "/auth/plex/pin" in page
            assert '"back"' not in page, "the small window shows what went wrong instead"
            h = {"X-Plexbie": "1"}
            assert (await c.post("/auth/plex/pin", json={"id": 5, "code": "<script>"}, headers=h)).status == 400
            got = await (await c.post("/auth/plex/pin", json={"id": 5, "code": "abcd", "next": "/app/requests"}, headers=h)).json()
            assert got["url"].startswith("https://app.plex.tv/auth/#!?") and "code=abcd" in got["url"] and "popup%3D1" in got["url"]
            assert (await (await c.get("/auth/plex/check")).json()) == {"waiting": True}
            services.http_session.approved = True          # approved, e.g. in the Plex app
            done = await (await c.get("/auth/plex/check")).json()
            assert done == {"done": True, "next": "/app/requests"}
            # The small window coming back afterwards just closes.
            page = await c.get("/auth/plex/callback?popup=1", allow_redirects=False)
            assert page.status == 200 and "close" in await page.text()
            # And a stale check after sign-in says done instead of failing.
            assert (await (await c.get("/auth/plex/check")).json())["done"] is True
        finally:
            await c.close()
    asyncio.run(go())


def test_someone_elses_plex_pin_cant_be_finished_from_another_browser():
    """Any visitor could name any PIN number and, by polling first, take over the
    sign-in of whoever approved it; another site could also start one (login CSRF)."""
    from aiohttp import web
    from core.config import Config
    from helpers import FakeServices
    from portal.auth import Auth
    from portal.cache import TTLCache

    async def go():
        cfg = Config()
        cfg.web_session_secret = "s" * 32
        services = FakeServices(cfg)
        services.http_session = _FakeHttp()
        auth = Auth(None, services, TTLCache())
        auth._forget_device = lambda token, client: asyncio.sleep(0)
        app = web.Application()
        app.router.add_get("/auth/plex/go", auth.plex_go)
        app.router.add_post("/auth/plex/pin", auth.plex_pin)
        app.router.add_get("/auth/plex/check", auth.plex_check)
        attacker = TestClient(TestServer(app))
        await attacker.start_server()
        try:
            no_header = await attacker.post("/auth/plex/pin", json={"id": 5, "code": "zzzz"})
            no_window = await attacker.post("/auth/plex/pin", json={"id": 5, "code": "zzzz"}, headers={"X-Plexbie": "1"})
            cross_site = await attacker.post("/auth/plex/pin", json={"id": 5, "code": "zzzz"},
                                             headers={"X-Plexbie": "1", "Origin": "https://evil.example"})
            await attacker.get("/auth/plex/go")
            # The attacker names the victim's PIN (5) but can't know its code.
            planted = await attacker.post("/auth/plex/pin", json={"id": 5, "code": "zzzz"}, headers={"X-Plexbie": "1"})
            services.http_session.approved = True          # the victim approves their own PIN
            stolen = await (await attacker.get("/auth/plex/check")).json()
            return no_header.status, no_window.status, cross_site.status, planted.status, stolen
        finally:
            await attacker.close()
    no_header, no_window, cross_site, planted, stolen = asyncio.run(go())
    assert no_header == 403 and cross_site == 403 and no_window == 410
    assert planted == 200 and stolen == {"waiting": True}, "the PIN's code doesn't match, so no session"


def test_whole_page_plex_sign_in_makes_its_pin_in_the_browser_and_finishes_in_that_tab():
    """The link without the small window (popup blocked, invite page): the server
    never makes the PIN, so plex.tv's different-address refusal covers it too."""
    from aiohttp import web
    from core.config import Config
    from helpers import FakeServices
    from portal.auth import Auth
    from portal.cache import TTLCache

    async def go():
        cfg = Config()
        cfg.web_session_secret = "s" * 32
        services = FakeServices(cfg)
        services.http_session = _FakeHttp()
        auth = Auth(None, services, TTLCache())
        auth._forget_device = lambda token, client: asyncio.sleep(0)
        app = web.Application()
        app.router.add_get("/auth/plex/login", auth.plex_login)
        app.router.add_post("/auth/plex/pin", auth.plex_pin)
        app.router.add_get("/auth/plex/callback", auth.plex_callback)
        c = TestClient(TestServer(app))
        await c.start_server()
        try:
            start = await c.get("/auth/plex/login?next=%2Fapp%2Frequests", allow_redirects=False)
            assert not services.http_session.calls, "the server asked plex.tv for a PIN itself"
            page = await start.text()
            assert start.status == 200 and "https://plex.tv/api/v2/pins?strong=true" in page and "/auth/plex/pin" in page
            # No window to close when it goes wrong: back to the site, with the banner for why.
            params = json.loads(page.split("var p=", 1)[1].split(",d={};", 1)[0])
            assert params["back"] == {"0": "/?login=failed", "410": "/?login=expired", "429": "/?login=busy"}
            got = await (await c.post("/auth/plex/pin", json={"id": 5, "code": "abcd", "next": "/app/requests"},
                                      headers={"X-Plexbie": "1"})).json()
            forward = got["url"].split("#!?", 1)[1]
            assert "callback" in forward and "popup" not in forward, "plex.tv sends this same tab back"
            waiting = await c.get("/auth/plex/callback", allow_redirects=False)
            assert waiting.headers["Location"] == "/?login=cancelled"
            services.http_session.approved = True
            done = await c.get("/auth/plex/callback", allow_redirects=False)
            assert done.status == 302 and done.headers["Location"] == "/app/requests"
            assert "plexbie_session" in done.cookies
            again = await c.get("/auth/plex/callback", allow_redirects=False)
            assert again.headers["Location"] == "/?login=expired"
            assert not [u for m, u in services.http_session.calls if m == "post"]
        finally:
            await c.close()
    asyncio.run(go())


def test_plex_sign_in_locks_are_let_go_once_nobody_waits_on_them():
    from aiohttp import web
    from core.config import Config
    from helpers import FakeServices
    from portal.auth import Auth
    from portal.cache import TTLCache

    async def go():
        cfg = Config()
        cfg.web_session_secret = "s" * 32
        auth = Auth(None, FakeServices(cfg), TTLCache())
        finished = []

        async def once(flow, request, response):
            await asyncio.sleep(0.01)
            finished.append(flow["pin"])
            return "/" if flow.get("approved") else None
        auth._finish_plex_once = once
        for pin in range(100):                     # never approved, e.g. made-up PIN numbers
            assert await auth._finish_plex({"pin": pin}, None, None) is None
        assert auth._pin_locks == {}, "a PIN nobody approves keeps no lock"
        # The polling tab and the sign-in window together: one finishes, the other gets its answer.
        finished.clear()
        both = await asyncio.gather(*(auth._finish_plex({"pin": 500, "approved": True}, None, web.Response())
                                      for _ in range(2)))
        assert both == ["/", "/"] and finished == [500] and auth._pin_locks == {}
    asyncio.run(go())


def test_login_redirects_stay_on_this_site():
    from portal.auth import safe_next
    for bad in ("/\t/evil.example", "/\n/evil.example", "/\r//evil.example", "//evil.example", "/\\evil.example",
                "https://evil.example", "/" + "a" * 400):
        assert safe_next(bad) == "/", repr(bad)
    assert safe_next("/app/title/tv/1?x=%09") == "/app/title/tv/1?x=%09"


def test_a_public_address_without_https_gets_it():
    from core.config import public_url
    assert public_url("Plexbie.com") == "https://Plexbie.com"
    assert public_url(" https://plexbie.example.com/ ") == "https://plexbie.example.com"
    assert public_url("http://10.0.0.5:7979") == "http://10.0.0.5:7979"
    assert public_url("") == ""


def test_check_tries_what_is_filled_in_and_skips_the_rest():
    p = _env("")
    there = tempfile.mkdtemp()

    async def steps(c, s):
        s.http = None   # nothing here may reach the network
        return await (await c.post("/setup/api/check", json={
            "keys": ["BOOKSHELF_AUDIOBOOK_LIBRARY", "BOOKSHELF_EBOOK_WATCH", "NZBHYDRA_URL", "INACTIVITY_WARNING_DAYS"],
            "values": {"BOOKSHELF_AUDIOBOOK_LIBRARY": there, "BOOKSHELF_EBOOK_WATCH": "/nowhere/at/all"}})).json()

    got = _scenario(p, steps)["results"]
    assert [(r["name"], r["ok"]) for r in got] == [("Audiobook library", True), ("Ebook watch", False)]
    assert "writable" in got[0]["message"] and "isn't there" in got[1]["message"]


def test_the_discord_extras_moved_to_the_discord_step():
    p = _env("")

    async def steps(c, s):
        return await (await c.get("/setup/api/more")).json()

    keys = {k["key"] for sec in _scenario(p, steps)["sections"] for k in sec["keys"]}
    assert not keys & {"STATS_CHANNEL_ID", "WATCH_PARTY_CHANNEL_ID", "NOW_WATCHING_MESSAGE_ID"}
    page = setup.PAGE.read_text()
    assert 'id="STATS_CHANNEL_ID"' in page and 'id="WATCH_PARTY_CHANNEL_ID"' in page


def test_set_up_this_server_makes_what_is_missing_and_reuses_the_rest():
    p = _env("")
    sent = []
    ids = iter(range(100, 200))

    async def fake_json(method, url, **kw):
        sent.append((method, url.split("/api/v10/")[1], kw.get("json")))
        path = url.split("/api/v10/")[1]
        if path == "users/@me":
            return {"id": "9"}
        if path.endswith("/roles") and method == "GET":
            return [{"id": "1", "name": "@everyone"}, {"id": "50", "name": "Plex Member"}]
        if path.endswith("/channels") and method == "GET":
            return [{"id": "60", "name": "plex-stats", "type": 0}]
        if "/messages?" in path:
            return []
        return {"id": str(next(ids))}

    async def steps(c, s):
        os.environ["DISCORD_BOT_TOKEN"] = "t"
        s._json = fake_json
        d = await (await c.post("/setup/api/discord/build", json={"guild": "42"})).json()
        return d, dict(os.environ)

    d, env = _scenario(p, steps)
    assert d["ok"], d
    assert "@Plex Member" in d["kept"] and "#plex-stats" in d["kept"]
    assert "@New on Plex" in d["made"] and "#new-on-plex" in d["made"] and "#plexbie-admin" in d["made"]
    assert env["PLEX_MEMBER_ROLE_ID"] == "50" and env["STATS_CHANNEL_ID"] == "60" and env["GUILD_ID"] == "42"
    for key in ("UPDATES_CHANNEL_ID", "ADMIN_CHANNEL_ID", "WATCH_PARTY_CHANNEL_ID", "ARRIVALS_ROLE_ID", "ADMIN_ROLE_ID",
                "NOW_WATCHING_MESSAGE_ID", "LEADERBOARD_MESSAGE_ID", "WATCH_STREAK_MESSAGE_ID"):
        assert env.get(key), key
    ping = next(j for m, path, j in sent if m == "POST" and path.endswith("/roles") and j["name"] == "New on Plex")
    assert ping["mentionable"] is True
    admin = next(j for m, path, j in sent if m == "POST" and j and j.get("name") == "plexbie-admin")
    assert any(o["id"] == "42" and int(o["deny"]) & 1024 for o in admin["permission_overwrites"]), "admin channel is private"
    assert any(j and "plexbie:arrivals_role" in str(j.get("components")) for m, path, j in sent), "the ping button is posted"


def test_the_invite_asks_for_exactly_the_documented_permissions():
    assert setup.BOT_PERMISSIONS == sum(setup.PERMISSIONS.values())
    assert setup.BOT_PERMISSIONS & 8 == 0, "never Administrator"
    readme = (pathlib.Path(conftest.PROJECT_ROOT) / "README.md").read_text()
    for name in setup.PERMISSIONS:
        assert name.split()[0] in readme, name


def test_a_restart_partway_through_returns_to_the_setup_page():
    """Required settings filled in isn't the same as finished: only Finish ends setup."""
    p = _env("")

    async def steps(c, s):
        await c.post("/setup/api/save", json={"values": {
            "DISCORD_BOT_TOKEN": "t", "GUILD_ID": "42", "PLEX_URL": "http://p:32400", "PLEX_TOKEN": "x"}})
        midway = setup.needs_setup(p.parent)
        await c.post("/setup/api/finish", json={"values": {}})
        return midway, setup.needs_setup(p.parent)

    midway, after = _scenario(p, steps)
    assert midway is True, "a restart now would skip the rest of setup"
    assert after is False


def test_book_folders_are_found_in_the_mounted_share():
    root = pathlib.Path(tempfile.mkdtemp())
    for d in ("usenet/complete/audiobooks", "usenet/complete/ebooks", "media/audiobooks", "media/movies", ".hidden/books",
              "media/audiobooks/Some Author/Some Book", "stl/thing/artbook", "Scripts/x/bookshelf_processor"):
        (root / d).mkdir(parents=True)
    original = setup.FOLDER_ROOTS
    setup.FOLDER_ROOTS = (str(root),)
    try:
        found = setup._book_folders()
    finally:
        setup.FOLDER_ROOTS = original
    rel = {str(pathlib.Path(f).relative_to(root)) for f in found}
    assert rel == {"usenet/complete/audiobooks", "usenet/complete/ebooks", "media/audiobooks"}, rel
    guess = setup.suggest_book_paths(found)
    assert guess["BOOKSHELF_AUDIOBOOK_WATCH"].endswith("usenet/complete/audiobooks")
    assert guess["BOOKSHELF_EBOOK_WATCH"].endswith("usenet/complete/ebooks")
    assert guess["BOOKSHELF_AUDIOBOOK_LIBRARY"].endswith("media/audiobooks")
    assert "BOOKSHELF_EBOOK_LIBRARY" not in guess


def test_book_downloads_is_its_own_section_with_plain_labels():
    p = _env("")

    async def steps(c, s):
        return await (await c.get("/setup/api/more")).json()

    secs = {sec["title"]: sec for sec in _scenario(p, steps)["sections"]}
    books = secs["Book downloads"]
    assert [k["key"] for k in books["keys"]] == ["NZBHYDRA_URL", "NZBHYDRA_API_KEY"]
    assert "NZBHydra" in books["note"] and "Tautulli" not in books["note"]
    smtp = {k["key"]: k for sec in secs.values() for k in sec["keys"]}
    assert "SMTP token" in smtp["SMTP_PASSWORD"]["label"]
    assert smtp["SMTP_FROM"]["default"] == "", "no example.com placeholder that mail servers refuse"


def test_a_saved_secret_is_never_sent_to_a_different_address():
    p = _env("SONARR_URL=http://sonarr.home:8989\nSONARR_TOKEN=real-key\n")
    os.environ.update(SONARR_URL="http://sonarr.home:8989", SONARR_TOKEN="real-key")

    async def steps(c, s):
        os.environ.update(SONARR_URL="http://sonarr.home:8989", SONARR_TOKEN="real-key")
        same = s._get("SONARR_TOKEN", {"SONARR_URL": "http://sonarr.home:8989/"})
        other = s._get("SONARR_TOKEN", {"SONARR_URL": "http://attacker.example"})
        typed = s._get("SONARR_TOKEN", {"SONARR_URL": "http://attacker.example", "SONARR_TOKEN": "typed"})
        return same, other, typed

    assert _scenario(p, steps) == ("real-key", "", "typed")


def test_passwords_with_quotes_survive_saving():
    from dotenv import dotenv_values
    p = _env("")
    tricky = "it's a " + '"pa$$"' + " #1 " + chr(92) + " ok"
    setup.write_env(p, {"SMTP_PASSWORD": tricky})
    assert dotenv_values(p)["SMTP_PASSWORD"] == tricky


def test_an_import_cannot_copy_one_secret_into_a_visible_field():
    p = _env("")
    os.environ["PLEX_TOKEN"] = "secret-token"

    async def steps(c, s):
        await c.post("/setup/api/import", json={"text": "GUILD_ID=${PLEX_TOKEN}\n"})
        return p.read_text()

    assert "secret-token" not in _scenario(p, steps)


def test_reopened_setup_asks_for_the_code_from_the_log():
    p = _env("")

    async def steps(c, s):
        locked = await c.get("/setup/api/state")
        wrong = await c.get("/setup/api/state", headers={"X-Setup-Code": "nope"})
        right = await c.get("/setup/api/state", headers={"X-Setup-Code": s.code.lower()})
        page = await c.get("/")
        return locked.status, (await locked.json()), wrong.status, right.status, page.status

    locked, body, wrong, right, page = _scenario(p, steps, problem="Discord turned down the bot token.", code=False)
    assert locked == 403 and body["needsCode"] and "fields" not in body
    assert wrong == 403 and right == 200 and page == 200


# ------------------------------------------------------- security (audit fixes)
def test_a_first_install_asks_for_the_code_too_and_wrong_codes_slow_down():
    """Whoever reached the port first used to set Plexbie up; a restart mid-setup
    reopened it with every saved secret and no code."""
    p = _env("")

    async def steps(c, s):
        assert len(setup._plain_code(s.code)) == setup.CODE_LENGTH
        locked = await c.get("/setup/api/state")
        save = await c.post("/setup/api/save", json={"values": {"PLEX_URL": "http://evil"}})
        tries = [(await c.get("/setup/api/state", headers={"X-Setup-Code": f"WRONG-{i}"})).status for i in range(8)]
        # Spaces, dashes and case don't matter; the right code still works for others.
        s._fails.clear()
        right = await c.get("/setup/api/state", headers={"X-Setup-Code": s.code.replace("-", " ").lower()})
        s._fail_total = setup.GLOBAL_TRIES
        after_cap = await c.get("/setup/api/state", headers={"X-Setup-Code": s.code})
        s._paused_until = 0.0                      # the pause runs out
        after_pause = await c.get("/setup/api/state", headers={"X-Setup-Code": s.code})
        return locked.status, save.status, tries, right.status, after_cap.status, after_pause.status

    locked, save, tries, right, after_cap, after_pause = _scenario(p, steps, code=False)
    assert locked == 403 and save == 403
    assert tries[:setup.FREE_TRIES - 1] == [403] * (setup.FREE_TRIES - 1) and 429 in tries, tries
    assert right == 200
    assert after_cap == 429, "past the overall limit nothing is accepted for a while"
    assert after_pause == 200, "a pause, so nobody can keep the owner out for good"
    assert "PLEX_URL=http://evil" not in p.read_text()


def test_a_line_break_in_a_value_cant_add_settings():
    """A carriage return in a value wrote WEB_DIST=config into .env, making the
    website serve config/.env to anyone."""
    from dotenv import dotenv_values
    p = _env("SMTP_FROM=\n")
    try:
        setup.write_env(p, {"SMTP_FROM": "x\rWEB_DIST=config"})
        raise AssertionError("write_env accepted a carriage return")
    except ValueError:
        pass

    async def steps(c, s):
        saved = await c.post("/setup/api/save", json={"values": {"SMTP_FROM": "x\rWEB_DIST=config"}})
        imported = await c.post("/setup/api/import", json={"text": "SMTP_FROM='x\rWEB_DIST=/'\n"})
        return saved.status, (await saved.json()), imported.status

    saved, body, imported = _scenario(p, steps)
    assert saved == 400 and "line break" in body["error"] and imported == 400
    assert "WEB_DIST" not in dotenv_values(p)


def test_moving_an_address_drops_its_saved_key():
    """Saving PLEX_URL=http://attacker then pressing Test sent the saved Plex token there."""
    p = _env("PLEX_URL=http://10.0.0.5:32400\nPLEX_TOKEN=owner-token\nSONARR_URL=http://10.0.0.5:8989\nSONARR_TOKEN=s-key\n")

    async def steps(c, s):
        os.environ.update(PLEX_URL="http://10.0.0.5:32400", PLEX_TOKEN="owner-token",
                          SONARR_URL="http://10.0.0.5:8989", SONARR_TOKEN="s-key")
        await c.post("/setup/api/save", json={"values": {"PLEX_URL": "http://attacker:32400"}})
        await c.post("/setup/api/save", json={"values": {"SONARR_URL": "http://10.0.0.9:8989", "SONARR_TOKEN": "typed-again"}})
        plex_after = os.getenv("PLEX_TOKEN")
        # An address plex.tv listed for the signed-in account keeps the token.
        os.environ["PLEX_TOKEN"] = "owner-token"
        s.plex_urls.add("https://10-0-0-5.abc.plex.direct:32400")
        await c.post("/setup/api/save", json={"values": {"PLEX_URL": "https://10-0-0-5.abc.plex.direct:32400"}})
        return plex_after, os.getenv("PLEX_TOKEN"), os.getenv("SONARR_TOKEN")

    plex_after, plex_listed, sonarr = _scenario(p, steps)
    assert plex_after == "", "the token stays behind when the address moves"
    assert plex_listed == "owner-token" and sonarr == "typed-again"


def test_login_and_webhook_secrets_are_never_taken_from_the_page():
    """Planting WEB_SESSION_SECRET let someone sign their own admin login after Finish."""
    p = _env("")

    async def steps(c, s):
        await c.post("/setup/api/save", json={"values": {"WEB_SESSION_SECRET": "x" * 40, "TAUTULLI_WEBHOOK_SECRET": "known"}})
        planted = (os.getenv("WEB_SESSION_SECRET"), os.getenv("TAUTULLI_WEBHOOK_SECRET"))
        got = await (await c.post("/setup/api/import", json={
            "text": "WEB_SESSION_SECRET=" + "y" * 40 + "\nTAUTULLI_WEBHOOK_SECRET=from-old-install\n"})).json()
        bad = await c.post("/setup/api/save", json={"values": {"DISCORD_CALLBACK_URL": "https://evil.example/steal"}})
        return planted, got, os.getenv("WEB_SESSION_SECRET"), os.getenv("TAUTULLI_WEBHOOK_SECRET"), bad.status

    planted, got, session, webhook, bad = _scenario(p, steps)
    assert planted == (None, None)
    assert session is None and "WEB_SESSION_SECRET" in got["skipped"]
    assert webhook == "from-old-install", "an old install's webhook secrets come across in an import"
    assert bad == 400


def test_the_plex_window_needs_a_one_use_ticket():
    p = _env("")

    async def steps(c, s):
        bare = await c.post("/setup/api/plex/pin", json={"id": 1, "code": "abcd"},
                            headers={"X-Setup-Code": ""})
        ticket = (await (await c.post("/setup/api/plex/ticket", json={})).json())["ticket"]
        page = await (await c.get(f"/setup/plex/go?t={ticket}")).text()
        first = await c.post("/setup/api/plex/pin", json={"id": 1, "code": "abcd"}, headers={"X-Setup-Ticket": ticket})
        again = await c.post("/setup/api/plex/pin", json={"id": 2, "code": "abcd"}, headers={"X-Setup-Ticket": ticket})
        return bare.status, ticket in page, first.status, again.status, s.pins.get("current")

    bare, in_page, first, again, pin = _scenario(p, steps)
    assert bare == 403 and in_page and first == 200 and again == 403 and pin == 1


def test_errors_shown_on_the_page_never_include_the_address():
    import aiohttp
    from multidict import CIMultiDict
    from yarl import URL
    url = URL("http://sab:8080/api?mode=queue&apikey=SECRET123")
    err = aiohttp.ClientResponseError(aiohttp.RequestInfo(url, "GET", CIMultiDict(), url), (), status=500, message="x")
    assert "SECRET123" not in setup._why(err) and "500" in setup._why(err)


def test_the_setup_page_has_its_own_content_security_policy():
    p = _env("")

    async def steps(c, s):
        r = await c.get("/")
        return r.headers.get("Content-Security-Policy", "")

    assert "frame-ancestors 'none'" in _scenario(p, steps, code=False)


def test_the_website_never_serves_settings_even_with_a_bad_web_dist():
    from pathlib import Path
    from portal.app import _safe_dist
    assert not _safe_dist(Path("/")) and not _safe_dist(Path("config")) and not _safe_dist(Path("."))
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "index.html").write_text("<p>site</p>")
    assert _safe_dist(d)
    (d / ".env").write_text("X=1")
    assert not _safe_dist(d), "a folder holding a .env is never the website"


def test_testing_plex_right_after_signing_in_uses_the_saved_token():
    """Sign in with Plex saved the token; picking the server and pressing Test sent
    no token, because the picked address wasn't saved yet."""
    p = _env("PLEX_URL=\nPLEX_TOKEN=\n")
    sent = {}

    async def steps(c, s):
        os.environ.update(PLEX_URL="", PLEX_TOKEN="owner-token")
        s.plex_urls.add("http://192.168.1.20:32400")

        async def fake_json(method, url, headers=None, **kw):
            sent[url] = (headers or {}).get("X-Plex-Token")
            return {"MediaContainer": {"friendlyName": "Home"}}
        s._json = fake_json
        listed = await (await c.post("/setup/api/test", json={"service": "plex",
                        "values": {"PLEX_URL": "http://192.168.1.20:32400"}})).json()
        other = await (await c.post("/setup/api/test", json={"service": "plex",
                       "values": {"PLEX_URL": "http://attacker:32400"}})).json()
        return listed, other

    listed, other = _scenario(p, steps)
    assert listed["ok"] and sent["http://192.168.1.20:32400/"] == "owner-token"
    assert sent.get("http://attacker:32400/") in (None, ""), "never to an address plex.tv didn't list"


def test_setup_only_opens_on_the_home_network():
    """Through Cloudflare (a public visitor) setup is refused, page and API alike."""
    p = _env("")

    async def steps(c, s):
        public = {"X-Forwarded-For": "8.8.8.8", "CF-Connecting-IP": "8.8.8.8"}
        page = await c.get("/setup", headers=public)
        api = await c.get("/setup/api/state", headers={**public, "X-Setup-Code": s.code})
        lan = await c.get("/setup/api/state", headers={"X-Forwarded-For": "192.168.1.20", "X-Setup-Code": s.code})
        return page.status, api.status, lan.status

    page, api, lan = _scenario(p, steps, code=False)
    assert page == 403 and api == 403
    assert lan == 200, "a home device through a local proxy still gets in"


def test_a_proxy_that_forwards_nobody_is_not_home():
    """A trusted proxy without X-Forwarded-For/CF-Connecting-IP could be passing on
    anyone, so its own private address doesn't open setup; loopback still does."""
    from portal.setup import Setup

    class Req:
        path = "/setup"

        def __init__(self, remote, headers=None):
            self.remote, self.headers = remote, headers or {}

    async def ok(request):
        return "opened"

    async def status(req):
        out = await Setup._home_only(None, req, ok)
        return out if out == "opened" else out.status

    assert asyncio.run(status(Req("172.17.0.2"))) == 403, "header-less proxy on the Docker network"
    assert asyncio.run(status(Req("172.17.0.2", {"X-Forwarded-For": "8.8.8.8"}))) == 403, "public visitor"
    assert asyncio.run(status(Req("172.17.0.2", {"X-Forwarded-For": "192.168.1.20"}))) == "opened", "home device via proxy"
    assert asyncio.run(status(Req("192.168.1.20"))) == "opened", "home device directly"
    assert asyncio.run(status(Req("127.0.0.1"))) == "opened", "on the server itself"


def test_the_address_check_proves_it_reaches_this_plexbie():
    """Setup asks the address for a one-time path only this server knows: a different
    app at that address, a name that doesn't exist, or plain http each say so."""
    import aiohttp
    from aiohttp import web

    async def steps(c, s):
        s.http = aiohttp.ClientSession()
        other = TestServer(web.Application())          # something else answering there
        other.app.router.add_get("/{tail:.*}", lambda r: web.Response(text="hello"))
        await other.start_server()
        try:
            ours = (await (await c.post("/setup/api/address", json={"url": str(c.make_url("")).rstrip("/")})).json())
            theirs = (await (await c.post("/setup/api/address", json={"url": str(other.make_url("")).rstrip("/")})).json())
            missing = (await (await c.post("/setup/api/address", json={"url": "plexbie.nowhere.invalid"})).json())
            nonsense = (await (await c.post("/setup/api/address", json={"url": "not an address"})).json())
            stray = await c.get("/setup/probe/guess", headers={"CF-Connecting-IP": "8.8.8.8"})
            return ours, theirs, missing, nonsense, stray.status, s._probes
        finally:
            await other.close()
            await s.http.close()

    ours, theirs, missing, nonsense, stray, left = _scenario(_env(""), steps)
    assert ours["ok"] and ours.get("warn") and "plain http" in ours["message"], ours
    assert not theirs["ok"] and "isn't this Plexbie" in theirs["message"]
    assert not missing["ok"] and ("doesn't exist" in missing["message"] or "Couldn't check" in missing["message"]
                                  or "Nothing answered" in missing["message"])
    assert not nonsense["ok"] and "isn't an address" in nonsense["message"]
    assert stray == 404 and left == {}, "a probe answers only while its own check runs"
