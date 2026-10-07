# path: tests/test_admin_routes.py
"""Every Manage route on the website (/api/admin/*) is for admins only.

Each handler calls `admin_only` by hand, so one that forgets would still work for
admins and nothing else would notice. This walks the app's own route table: every
admin route must be listed below with an address that reaches it, and each one
refuses a signed-out visitor (401), someone without Plex access (403) and a member
(403) before it does anything. The routes are taken from the app built both with
and without sign-in, read-write and with invite links on, so a new admin route
that isn't listed fails here until it is, and can't skip the check.
"""
import asyncio
import inspect
import json
import tempfile

import conftest  # noqa: F401
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from helpers import FakeServices
from test_portal import MEMBER, OK_HEADERS, OUTSIDER

from core.config import Config
from portal.actions import Actions
from portal.app import build_app

#: Each admin route as the app registers it, and the addresses that reach it. A
#: route that branches on its address lists one for every branch.
ADMIN_ROUTES = {
    ("GET", "/api/admin/{section}"): tuple(f"/api/admin/{section}" for section in (
        "requests", "joins", "people", "cleanup", "health", "invites", "links", "discord",
        "help", "plexinvites", "messages", "all", "tickets")),
    ("GET", "/api/admin/messages/{who}"): "/api/admin/messages/d7",
    ("GET", "/api/admin/cleanup/search"): "/api/admin/cleanup/search?q=sintel",
    ("GET", "/api/admin/request/{key}"): "/api/admin/request/123",
    ("GET", "/api/admin/ticket/{id}"): "/api/admin/ticket/abcdefabcdef",
    ("GET", "/api/admin/blocked"): "/api/admin/blocked",
    ("GET", "/api/admin/blocked/{app}/{did}"): "/api/admin/blocked/sonarr/SABnzbd_nzo_1",
    ("GET", "/api/admin/arr/{app}/library"): "/api/admin/arr/radarr/library?q=sintel",
    ("GET", "/api/admin/arr/sonarr/series/{id}/episodes"): "/api/admin/arr/sonarr/series/7/episodes",
    ("POST", "/api/admin/help/{id}/search"): "/api/admin/help/5/search",
    ("POST", "/api/admin/help/{id}/episodes"): "/api/admin/help/5/episodes",
    ("POST", "/api/admin/help/{id}/name"): "/api/admin/help/5/name",
    ("POST", "/api/admin/help/{id}/resolve"): "/api/admin/help/5/resolve",
    ("POST", "/api/admin/blocked/{app}/{did}/import"): "/api/admin/blocked/radarr/SABnzbd_nzo_1/import",
    ("POST", "/api/admin/request/{key}/ticket"): "/api/admin/request/123/ticket",
    ("POST", "/api/admin/ticket/{id}/comment"): "/api/admin/ticket/abcdefabcdef/comment",
    ("POST", "/api/admin/ticket/{id}/status"): "/api/admin/ticket/abcdefabcdef/status",
    ("POST", "/api/admin/ticket/{id}/take"): "/api/admin/ticket/abcdefabcdef/take",
    ("POST", "/api/admin/request/{key}/search/{how}"): "/api/admin/request/123/search/again",
    ("POST", "/api/admin/{what}/{id}/{decision}"): ("/api/admin/requests/123/approve", "/api/admin/requests/123/decline",
                                                    "/api/admin/joins/123/approve", "/api/admin/joins/123/decline"),
    ("POST", "/api/admin/cleanup/exempt"): "/api/admin/cleanup/exempt",
    ("POST", "/api/admin/people/remove"): "/api/admin/people/remove",
    ("POST", "/api/admin/invites"): "/api/admin/invites",
    ("POST", "/api/admin/invites/{id}/revoke"): "/api/admin/invites/abc/revoke",
    ("POST", "/api/admin/invites/{id}/delete"): "/api/admin/invites/abc/delete",
    ("POST", "/api/admin/invites/{id}/renew"): "/api/admin/invites/abc/renew",
    ("POST", "/api/admin/plex-invites/cancel"): "/api/admin/plex-invites/cancel",
    ("POST", "/api/admin/plex-invites/change"): "/api/admin/plex-invites/change",
    ("POST", "/api/admin/people/link"): "/api/admin/people/link",
    ("POST", "/api/admin/people/unlink"): "/api/admin/people/unlink",
    ("POST", "/api/admin/people/match"): "/api/admin/people/match",
    ("POST", "/api/admin/people/keep"): "/api/admin/people/keep",
    ("POST", "/api/admin/people/rename"): "/api/admin/people/rename",
    ("POST", "/api/admin/say"): "/api/admin/say",
    ("POST", "/api/admin/messages/{who}/reply"): "/api/admin/messages/d7/reply",
    ("POST", "/api/admin/messages/{who}/done"): "/api/admin/messages/d7/done",
    ("POST", "/api/admin/message/{key}/to-ticket"): "/api/admin/message/20261005T120000000000-abcdef/to-ticket",
    ("POST", "/api/admin/inbox"): "/api/admin/inbox",
    ("POST", "/api/admin/cleanup/settings"): "/api/admin/cleanup/settings",
    ("POST", "/api/admin/cleanup/scan"): "/api/admin/cleanup/scan",
}


class _Recorded(Actions):
    """The real guards (header, origin, limits); every action is recorded, never run."""

    def __init__(self, services, calls):
        super().__init__(bot=None, services=services, data=None, public_url="https://plexbie.com")
        for name, fn in inspect.getmembers(Actions, inspect.iscoroutinefunction):
            setattr(self, name, self._record(calls, name))

    @staticmethod
    def _record(calls, name):
        async def recorded(*args, **kwargs):
            calls.append(name)
            return {"ok": True}
        return recorded


class _Invites:
    """Invite links, recorded the same way."""

    def __init__(self, calls):
        self.calls = calls

    def __getattr__(self, name):
        async def recorded(*args, **kwargs):
            self.calls.append(f"invites.{name}")
            return []
        return recorded


class _SignIn:
    """Sign-in turned on. build_app only picks up its handlers, and no admin route uses them."""

    def __getattr__(self, name):
        async def handler(*args, **kwargs):
            raise AssertionError(f"auth.{name} was used")
        return handler


def _app(user, calls, auth=None):
    services = FakeServices(Config())

    async def who(request):
        return user

    # Read-write, with invite links on: every admin route is registered.
    return build_app(services, who=who, readonly=False, dist=None, image_cache=tempfile.mkdtemp(),
                     auth=auth, actions=_Recorded(services, calls), invites=_Invites(calls))


def _addresses(listed) -> tuple:
    return (listed,) if isinstance(listed, str) else listed


def _admin_routes(app) -> dict:
    """(method, path) -> route for every /api/admin route the app has. HEAD comes
    with each GET and runs the same handler."""
    return {(route.method, route.resource.canonical): route for route in app.router.routes()
            if route.method != "HEAD" and route.resource is not None
            and route.resource.canonical.startswith("/api/admin")}


def test_every_admin_route_is_listed_with_an_address_that_reaches_it():
    async def scenario():
        seen = set()
        for auth in (None, _SignIn()):
            app = _app(None, [], auth)
            routes = _admin_routes(app)
            seen |= set(routes)
            unlisted = sorted(set(routes) - set(ADMIN_ROUTES))
            assert not unlisted, f"admin routes missing from ADMIN_ROUTES (add each with an address): {unlisted}"
            for key, route in routes.items():
                for path in _addresses(ADMIN_ROUTES[key]):
                    match = await app.router.resolve(make_mocked_request(key[0], path))
                    assert match.route is route, f"{key[0]} {path} doesn't reach {key[1]}"
        gone = sorted(set(ADMIN_ROUTES) - seen)
        assert not gone, f"ADMIN_ROUTES lists routes the app no longer has: {gone}"
    asyncio.run(scenario())


def test_every_admin_route_refuses_everyone_but_admins_before_doing_anything():
    async def scenario():
        refused = []
        for who, user, expected in (("signed out", None, 401), ("outsider", OUTSIDER, 403), ("member", MEMBER, 403)):
            calls = []
            # With sign-in on, as a real install runs.
            client = TestClient(TestServer(_app(user, calls, _SignIn())))
            await client.start_server()
            try:
                for (method, _), listed in ADMIN_ROUTES.items():
                    for path in _addresses(listed):
                        resp = await client.request(method, path, headers=OK_HEADERS, data=json.dumps({}))
                        if resp.status != expected or calls:
                            refused.append((who, method, path, resp.status, list(calls)))
                        calls.clear()
            finally:
                await client.close()
        return refused

    wrong = asyncio.run(scenario())
    assert not wrong, f"admin routes that let the wrong people in (who, method, path, status, actions run): {wrong}"
