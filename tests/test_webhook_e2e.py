# path: tests/test_webhook_e2e.py
"""End-to-end webhook auth over a real aiohttp server and real HTTP requests.

These exist because unit-level tests on the validator cannot see what the wrapper
does to the request object. In particular: create_validated_handler reads the body
before the handler runs, and Plex posts multipart/form-data, which a handler parses
with request.post(). Only a real request proves those compose correctly.

Also pins the behaviour the live deployment depends on: with secrets unset, every
route must keep passing traffic through unchanged.
"""
import asyncio
import json

import aiohttp
from aiohttp import web

import conftest  # noqa: F401
from helpers import EmptySecretsConfig, SetSecretsConfig

from core.webhook_security import WebhookValidator, create_validated_handler

PORT = 18099
BASE = f"http://127.0.0.1:{PORT}"


class _Harness:
    """Runs the three handler shapes the codebase actually uses."""

    def __init__(self, config):
        self.validator = WebhookValidator(config)
        self.seen = {}

    async def _plex(self, request):
        """Mirrors plugins/new_media_added: multipart via request.post()."""
        data = await request.post()
        payload = data.get("payload")
        if payload:
            self.seen["plex"] = json.loads(payload)
        return web.json_response({"status": "ok"})

    async def _sonarr(self, request):
        """Mirrors webhooks/sonarr_handler: JSON via request.json()."""
        self.seen["sonarr"] = await request.json()
        return web.json_response({"status": "ok"})

    async def _tautulli(self, request):
        """Mirrors core/webhooks._handle_tautulli: prefers the pre-read body."""
        body = request.get("_validated_body")
        self.seen["tautulli"] = json.loads(body) if body else await request.json()
        return web.json_response({"status": "ok"})

    def build_app(self):
        app = web.Application()
        for path, handler, service in (
            ("/webhook/plex", self._plex, "plex"),
            ("/webhook/sonarr", self._sonarr, "sonarr"),
            ("/webhook/tautulli", self._tautulli, "tautulli"),
        ):
            app.router.add_post(
                path, create_validated_handler(handler, self.validator, service)
            )
        return app


def _serve(config, scenario):
    """Run `scenario(session, harness)` against a live server."""
    async def runner():
        harness = _Harness(config)
        aiohttp_runner = web.AppRunner(harness.build_app())
        await aiohttp_runner.setup()
        site = web.TCPSite(aiohttp_runner, "127.0.0.1", PORT)
        await site.start()
        try:
            async with aiohttp.ClientSession() as session:
                return await scenario(session, harness)
        finally:
            await aiohttp_runner.cleanup()

    return asyncio.run(runner())


def _multipart(payload):
    form = aiohttp.FormData()
    form.add_field("payload", json.dumps(payload))
    return form


# --- the multipart case: wrapper + request.post() must compose ---

def test_plex_multipart_is_parsed_through_the_wrapper():
    """The validator reads the body first; multipart parsing must still work."""
    async def scenario(session, harness):
        url = f"{BASE}/webhook/plex?token={SetSecretsConfig.plex_webhook_secret}"
        async with session.post(url, data=_multipart({"event": "library.new"})) as r:
            return r.status, harness.seen.get("plex")

    status, seen = _serve(SetSecretsConfig, scenario)
    assert status == 200
    assert seen == {"event": "library.new"}, f"handler saw {seen!r}"


def test_plex_without_a_secret_is_refused_even_on_loopback():
    """No secret: refused, wherever the listener is (start-up makes one)."""
    async def scenario(session, harness):
        async with session.post(f"{BASE}/webhook/plex",
                                data=_multipart({"event": "library.new"})) as r:
            return r.status, harness.seen.get("plex")

    status, seen = _serve(EmptySecretsConfig, scenario)
    assert status == 401
    assert seen is None


# --- json bodies must survive the wrapper too ---

def test_json_body_reaches_handler_when_authenticated():
    async def scenario(session, harness):
        headers = {"X-Api-Key": SetSecretsConfig.sonarr_webhook_secret}
        async with session.post(f"{BASE}/webhook/sonarr",
                                json={"eventType": "Test"}, headers=headers) as r:
            return r.status, harness.seen.get("sonarr")

    status, seen = _serve(SetSecretsConfig, scenario)
    assert status == 200
    assert seen == {"eventType": "Test"}


def test_prevalidated_body_is_reused_by_handler():
    async def scenario(session, harness):
        headers = {"X-Webhook-Secret": SetSecretsConfig.tautulli_webhook_secret}
        async with session.post(f"{BASE}/webhook/tautulli",
                                json={"event_type": "play"}, headers=headers) as r:
            return r.status, harness.seen.get("tautulli")

    status, seen = _serve(SetSecretsConfig, scenario)
    assert status == 200
    assert seen == {"event_type": "play"}


# --- rejection paths, end to end ---

def test_rejections_return_401_not_500():
    """Every bad-credential shape must be a clean 401.

    The non-ASCII cases are the important ones: hmac.compare_digest raises
    TypeError on non-ASCII str, which escaped the wrapper as an unhandled 500 -
    reachable by any unauthenticated caller.
    """
    async def scenario(session, harness):
        results = {}

        async with session.post(f"{BASE}/webhook/plex", data=aiohttp.FormData()) as r:
            results["plex no token"] = r.status
        async with session.post(f"{BASE}/webhook/plex?token=wrong",
                                data=aiohttp.FormData()) as r:
            results["plex wrong token"] = r.status
        async with session.post(f"{BASE}/webhook/sonarr", json={"eventType": "Test"}) as r:
            results["sonarr no key"] = r.status
        async with session.post(f"{BASE}/webhook/sonarr", json={"eventType": "Test"},
                                headers={"X-Api-Key": "wrong"}) as r:
            results["sonarr wrong key"] = r.status
        async with session.post(f"{BASE}/webhook/sonarr", json={"eventType": "Test"},
                                headers={"X-Api-Key": "sécret"}) as r:
            results["sonarr non-ascii key"] = r.status
        async with session.post(f"{BASE}/webhook/tautulli", json={"event_type": "play"},
                                headers={"X-Webhook-Secret": "nøpe"}) as r:
            results["tautulli non-ascii secret"] = r.status
        return results

    results = _serve(SetSecretsConfig, scenario)
    assert all(status == 401 for status in results.values()), results


def test_authenticated_requests_are_not_rejected():
    """Guard against the gate being so strict it blocks valid traffic."""
    async def scenario(session, harness):
        results = {}
        async with session.post(
            f"{BASE}/webhook/plex?token={SetSecretsConfig.plex_webhook_secret}",
            data=_multipart({"event": "ok"}),
        ) as r:
            results["plex"] = r.status
        async with session.post(
            f"{BASE}/webhook/sonarr", json={"eventType": "Test"},
            headers={"X-Api-Key": SetSecretsConfig.sonarr_webhook_secret},
        ) as r:
            results["sonarr"] = r.status
        return results

    results = _serve(SetSecretsConfig, scenario)
    assert all(status == 200 for status in results.values()), results


def test_a_wrong_tautulli_header_is_refused_even_with_the_right_query_secret():
    async def scenario(session, harness):
        url = f"{BASE}/webhook/tautulli?secret={SetSecretsConfig.tautulli_webhook_secret}"
        async with session.post(url, json={"event_type": "play"},
                                headers={"Authorization": "Bearer wrong"}) as r:
            return r.status, harness.seen.get("tautulli")

    status, seen = _serve(SetSecretsConfig, scenario)
    assert status == 401
    assert seen is None


# --- the real Seerr route, as bot.setup_hook registers it ---

def _serve_seerr(scenario):
    """Run `scenario(session)` against WebhookServer with the real Seerr handler."""
    from helpers import FakeServices
    from core.webhooks import WebhookServer
    from webhooks.seerr_handler import register_seerr_webhook

    class Bot:
        services = FakeServices(SetSecretsConfig)

        def get_cog(self, _):
            return None

    async def runner():
        server = WebhookServer(Bot.services)
        register_seerr_webhook(server, Bot())
        aiohttp_runner = web.AppRunner(server.app)
        await aiohttp_runner.setup()
        await web.TCPSite(aiohttp_runner, "127.0.0.1", PORT).start()
        try:
            async with aiohttp.ClientSession() as session:
                return await scenario(session)
        finally:
            await aiohttp_runner.cleanup()

    return asyncio.run(runner())


def test_the_seerr_route_refuses_a_wrong_secret_and_takes_the_right_one():
    test = {"notification_type": "TEST_NOTIFICATION", "subject": "Test"}

    async def scenario(session):
        results = {}
        for path in ("/webhook/seerr", "/webhook/overseerr"):
            for label, auth in (("none", None), ("wrong", "Bearer wrong"),
                                ("bare", SetSecretsConfig.seerr_webhook_secret),
                                ("bearer", f"Bearer {SetSecretsConfig.seerr_webhook_secret}")):
                headers = {"Authorization": auth} if auth else {}
                async with session.post(f"{BASE}{path}", json=test, headers=headers) as r:
                    results[(path, label)] = r.status
        return results

    results = _serve_seerr(scenario)
    for path in ("/webhook/seerr", "/webhook/overseerr"):
        assert results[(path, "none")] == 401, results
        assert results[(path, "wrong")] == 401, results
        assert results[(path, "bare")] == 200, results
        assert results[(path, "bearer")] == 200, results
