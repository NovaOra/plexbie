# path: tests/test_webhook_routes.py
"""Every inbound webhook route must be registered behind the validator.

Regression coverage for: /webhook/sonarr, /webhook/radarr and /webhook/plex were
registered straight onto the aiohttp app, so WebhookValidator never ran on them -
_validate_arr_signature was dead code - while the startup banner reported auth as
enabled whenever a secret was merely present in the environment.
"""
import asyncio

import conftest  # noqa: F401
from helpers import FakeServices

from core.config import Config
from core.webhooks import WebhookServer

EXPECTED_ROUTES = {
    "/webhook/tautulli",
    "/webhook/seerr",
    "/webhook/overseerr",      # older installs' address for Seerr, still accepted
    "/webhook/sonarr",
    "/webhook/radarr",
    "/webhook/plex",
}


def _fully_wired_server():
    """A server with every route registered, as bot.setup_hook does."""
    from plugins.new_media_added import cog as new_media_added
    from webhooks.radarr_handler import register_radarr_webhook
    from webhooks.sonarr_handler import register_sonarr_webhook
    from webhooks.seerr_handler import register_seerr_webhook
    from webhooks.tautulli_handler import register_tautulli_webhook

    server = WebhookServer(FakeServices(Config()))
    register_sonarr_webhook(server, object())
    register_radarr_webhook(server, object())
    register_seerr_webhook(server, object())
    register_tautulli_webhook(server, object())

    # The Plex route is registered by the new_media_added cog.
    cog = object.__new__(new_media_added.NewMediaAddedCog)
    cog.services = FakeServices(Config())
    cog.active_batches = {}
    cog._data_loaded = False
    asyncio.run(new_media_added.NewMediaAddedCog.register_webhook_routes(cog, server))
    return server


def _post_routes(server):
    return {
        route.resource.canonical: route.handler.__name__
        for route in server.app.router.routes()
        if route.method == "POST"
    }


def test_every_expected_route_is_registered():
    assert set(_post_routes(_fully_wired_server())) == EXPECTED_ROUTES


def test_no_post_route_bypasses_validation():
    """The invariant: a raw handler name here means an unauthenticated route."""
    routes = _post_routes(_fully_wired_server())
    unvalidated = {p: h for p, h in routes.items() if h != "validated_wrapper"}
    assert not unvalidated, f"routes bypassing the validator: {unvalidated}"


def test_server_tracks_registered_services_for_the_banner():
    """The startup banner reports registered-and-secret-set, not merely-set."""
    server = _fully_wired_server()
    assert server._registered_services == {
        "tautulli", "seerr", "sonarr", "radarr", "plex",
    }


def test_health_endpoint_stays_unauthenticated():
    """Monitoring must not need a secret; it exposes no data."""
    server = _fully_wired_server()
    gets = {r.resource.canonical for r in server.app.router.routes() if r.method == "GET"}
    assert "/health" in gets


def test_no_module_registers_a_post_route_directly():
    """Guards against a future handler being added straight onto .app.router.

    Registering on the router bypasses the validator silently - there is no error
    and no log line - which is exactly how three routes ended up unauthenticated.
    """
    import inspect

    import plugins.new_media_added.cog as new_media_cog
    import webhooks.arr_handler as arr_module
    import webhooks.radarr_handler as radarr_module
    import webhooks.sonarr_handler as sonarr_module

    for module in (arr_module, sonarr_module, radarr_module, new_media_cog):
        source = inspect.getsource(module)
        assert "router.add_post" not in source, (
            f"{module.__name__} calls router.add_post directly, which skips "
            f"validation - use WebhookServer.add_validated_post instead"
        )


def test_webhooks_module_funnels_registration_through_one_call_site():
    """core.webhooks owns the single permitted add_post call, and it lives inside
    add_validated_post. More than one means a route escaped the funnel.

    Walks the AST rather than grepping, so prose in docstrings that mentions
    router.add_post is not mistaken for a call to it.
    """
    import ast
    import inspect

    import core.webhooks as webhooks_module

    tree = ast.parse(inspect.getsource(webhooks_module))
    enclosing = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "add_post"
            ):
                enclosing.append(node.name)

    assert enclosing == ["add_validated_post"], (
        f"add_post should be called exactly once, from add_validated_post; "
        f"found calls in: {enclosing}"
    )
