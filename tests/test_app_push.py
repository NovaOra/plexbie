# path: tests/test_app_push.py
"""Alerts in the Plexbie app (core/notify.py, "the app").

The app registers an Expo push token for whoever is signed in. It must only ever be
that person's, never be moved by someone who merely knows a token, be forgotten when
the phone drops the app, and get what web push and Discord DMs already say.
"""
import asyncio
import json
import os
import pathlib
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import conftest  # noqa: F401

# These tests are about sending app alerts, which an install has to turn on (APP_PUSH).
os.environ["APP_PUSH"] = "expo"

from core import notify

TOKEN = "ExponentPushToken[abcdefghij0123456789]"
OTHER = "ExponentPushToken[zzzzzzzzzz9876543210]"


async def _init(path):
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


def _db(body):
    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "a.db")
        return await body()
    return asyncio.run(scenario())


class _Expo:
    """A stand-in for Expo's push service: records what was sent, answers with tickets."""

    def __init__(self, tickets):
        self.tickets, self.sent = tickets, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.sent.extend(body)
                out = json.dumps({"data": [outer.tickets(m) for m in body]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.saved = notify.EXPO_PUSH_URL
        notify.EXPO_PUSH_URL = f"http://127.0.0.1:{self.server.server_port}/push"

    def close(self):
        notify.EXPO_PUSH_URL = self.saved
        self.server.shutdown()


def test_only_real_expo_tokens_are_kept_and_only_by_their_owner():
    async def body():
        bad = [await notify.register_app(t, "android", plex_account_id="7", plex_name=None, discord_id=None)
               for t in ("", "ExponentPushToken[]", "https://evil.example", "ExponentPushToken[a b]", None)]
        nobody = await notify.register_app(TOKEN, "android", plex_account_id=None, plex_name=None, discord_id=None)
        mine = await notify.register_app(TOKEN, "android", plex_account_id="7", plex_name=None, discord_id=None)
        takeover = await notify.register_app(TOKEN, "android", plex_account_id=None, plex_name=None, discord_id="999")
        await notify.unregister_app(TOKEN, plex_account_id=None, discord_id="999")      # not theirs: ignored
        still = len(await notify.apps_for(plex_account_id="7"))
        await notify.unregister_app(TOKEN, plex_account_id="7", discord_id=None)
        gone = len(await notify.apps_for(plex_account_id="7"))
        return bad, nobody, mine, takeover, still, gone
    bad, nobody, mine, takeover, still, gone = _db(body)
    assert bad == [False] * 5 and nobody is False
    assert mine is True and takeover is False, "knowing someone's token doesn't move their alerts"
    assert still == 1 and gone == 0


def test_each_member_keeps_a_few_phones_the_oldest_going_first():
    async def body():
        for i in range(notify.MAX_APPS + 2):
            await notify.register_app(f"ExponentPushToken[phone{i:02d}aaaaaaaa]", "ios", plex_account_id="7", plex_name=None, discord_id=None)
        return sorted(r["token"] for _, r in await notify.apps_for(plex_account_id="7"))
    kept = _db(body)
    assert len(kept) == notify.MAX_APPS and "ExponentPushToken[phone00aaaaaaaa]" not in kept


def test_a_phone_that_dropped_the_app_is_forgotten_and_the_rest_are_told():
    expo = _Expo(lambda m: {"status": "error", "details": {"error": "DeviceNotRegistered"}} if m["to"] == OTHER else {"status": "ok", "id": "t"})
    try:
        async def body():
            await notify.register_app(TOKEN, "android", plex_account_id="7", plex_name=None, discord_id="55")
            await notify.register_app(OTHER, "ios", plex_account_id="7", plex_name=None, discord_id="55")
            sent = await notify.push_app_to_discord("55", title="**Approved**", body="Dune is on its way")
            left = [r["token"] for _, r in await notify.apps_for(discord_id="55")]
            return sent, left
        sent, left = _db(body)
    finally:
        expo.close()
    assert sent == 1 and left == [TOKEN]
    assert {m["to"] for m in expo.sent} == {TOKEN, OTHER}
    assert all(m["channelId"] == "default" and m["data"]["url"] == "/app" for m in expo.sent)


def test_a_member_without_discord_gets_app_alerts_before_email():
    expo = _Expo(lambda m: {"status": "ok", "id": "t"})
    try:
        class Services:
            class config:
                web_public_url = "https://plexbie.example"
                smtp_host = smtp_username = smtp_password = None

        async def body():
            await notify.register_app(TOKEN, "android", plex_account_id="7", plex_name=None, discord_id=None)
            return await notify.notify_member(Services, title="Leaving soon", body="Watch something to stay",
                                              plex_account_id="7", context="test")
        how = _db(body)
    finally:
        expo.close()
    assert how == "push" and expo.sent and expo.sent[0]["title"] == "Leaving soon"


def test_expo_being_down_is_never_an_error_for_the_caller():
    saved = notify.EXPO_PUSH_URL
    notify.EXPO_PUSH_URL = "http://127.0.0.1:9/nothing-here"
    try:
        async def body():
            await notify.register_app(TOKEN, "android", plex_account_id=None, plex_name=None, discord_id="55")
            return await notify.push_app_to_discord("55", title="t", body="b"), len(await notify.apps_for(discord_id="55"))
        sent, kept = _db(body)
    finally:
        notify.EXPO_PUSH_URL = saved
    assert sent == 0 and kept == 1, "a failed send keeps the phone"


def test_every_discord_dm_also_reaches_the_app_without_waiting_for_it():
    from core import admin_mirror
    calls = []

    async def fake(discord_id, *, title, body, url="/app"):
        await asyncio.sleep(0.05)
        calls.append((discord_id, title, body))
        return 1
    saved = notify.push_app_to_discord
    notify.push_app_to_discord = fake
    try:
        class User:
            id = 55

            async def send(self, content=None, embed=None):
                raise RuntimeError("DMs closed")

        async def scenario():
            await _init(pathlib.Path(tempfile.mkdtemp()) / "d.db")
            try:
                await admin_mirror.send_user_dm(None, None, User(), context="test", content="**Dune** was approved")
            except RuntimeError:
                pass
            before = list(calls)
            await asyncio.sleep(0.2)
            return before, list(calls)
        before, after = asyncio.run(scenario())
    finally:
        notify.push_app_to_discord = saved
    assert before == [], "the DM doesn't wait on Expo"
    assert after == [(55, "Plexbie", "Dune was approved")], "even a refused DM reaches the phone"


def test_a_phone_signed_in_as_someone_else_takes_its_own_token_over():
    async def body():
        await notify.register_app(TOKEN, "android", plex_account_id="7", plex_name=None, discord_id=None, session="s-old")
        web = await notify.register_app(TOKEN, "android", plex_account_id=None, plex_name=None, discord_id="55")
        app = await notify.register_app(TOKEN, "android", plex_account_id=None, plex_name=None, discord_id="55", session="s-new")
        rows = await notify.apps_for(discord_id="55")
        return web, app, len(await notify.apps_for(plex_account_id="7")), rows[0][1].get("session") if rows else None
    web, app, old_owner, session = _db(body)
    assert web is False, "not without the app's own sign-in"
    assert app is True and old_owner == 0 and session == "s-new"


def test_an_ended_sign_in_stops_its_phones_alerts():
    expo = _Expo(lambda m: {"status": "ok", "id": "t"})
    saved = notify.session_alive
    notify.session_alive = lambda key: key == "live"
    try:
        async def body():
            await notify.register_app(TOKEN, "android", plex_account_id=None, plex_name=None, discord_id="55", session="ended")
            await notify.register_app(OTHER, "ios", plex_account_id=None, plex_name=None, discord_id="55", session="live")
            sent = await notify.push_app_to_discord("55", title="t", body="b")
            return sent, [r["token"] for _, r in await notify.apps_for(discord_id="55")]
        sent, left = _db(body)
    finally:
        notify.session_alive = saved
        expo.close()
    assert sent == 1 and left == [OTHER]
    assert [m["to"] for m in expo.sent] == [OTHER], "the signed-out phone got nothing"


def test_signing_out_of_the_app_forgets_its_phone_at_once():
    from aiohttp.test_utils import make_mocked_request
    from helpers import FakeServices
    from core.config import Config
    from portal.auth import Auth
    from portal.cache import TTLCache
    from portal import mobile

    async def body():
        auth = Auth(None, FakeServices(Config()), TTLCache())
        auth.mobile._sessions = {}
        verifier = "v" * 43
        code = auth.mobile.mint_code({"challenge": mobile.s256(verifier), "state": "s" * 22, "redirect": mobile.APP_REDIRECT},
                                     "discord", {"id": "55", "name": "Sam"})
        token = (await auth.mobile.exchange(code, verifier))["token"]
        headers = {"Authorization": f"Bearer {token}", "X-Plexbie": "1"}
        key = auth.session(make_mocked_request("GET", "/", headers=headers))["app"]
        await notify.register_app(TOKEN, "android", plex_account_id=None, plex_name=None, discord_id="55", session=key)
        before = len(await notify.apps_for(discord_id="55"))
        await auth.logout(make_mocked_request("POST", "/api/logout", headers=headers))
        return before, len(await notify.apps_for(discord_id="55")), notify.session_alive(key)
    before, after, alive = _db(body)
    assert before == 1 and after == 0 and alive is False
