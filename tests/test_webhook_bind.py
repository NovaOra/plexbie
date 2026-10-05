# path: tests/test_webhook_bind.py
"""The webhook server must not publish itself to the whole network.

Regression coverage for: start() hard-coded web.TCPSite(runner, "0.0.0.0", port).
Combined with network_mode: host that exposed every /webhook/* route to the LAN,
while those routes pass unauthenticated requests through whenever the matching
secret is unset - which was true for all six services.
"""
import asyncio
import socket

import aiohttp

import conftest  # noqa: F401
from helpers import FakeServices

from core.config import Config
from core.webhooks import WebhookServer


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server(bind, port):
    config = Config()
    config.webhook_bind = bind
    config.webhook_port = port
    return WebhookServer(FakeServices(config))


def _lan_ip():
    """A non-loopback address of this host, or None if it has none."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") else ip


# --- parsing ---

def test_default_is_loopback_only():
    assert Config().webhook_bind == "127.0.0.1"


def test_single_address_parses():
    assert _server("127.0.0.1", 1)._bind_addresses() == ["127.0.0.1"]


def test_comma_separated_list_parses_in_order():
    server = _server("127.0.0.1, 172.17.0.1 ,192.168.1.5", 1)
    assert server._bind_addresses() == ["127.0.0.1", "172.17.0.1", "192.168.1.5"]


def test_blank_falls_back_to_loopback():
    assert _server("", 1)._bind_addresses() == ["127.0.0.1"]
    assert _server("  ,  ", 1)._bind_addresses() == ["127.0.0.1"]


def test_missing_config_attribute_falls_back_to_loopback():
    """An older config object must not cause a bind to everything."""
    class Bare:
        webhook_port = 1
    server = WebhookServer(FakeServices(Bare()))
    assert server._bind_addresses() == ["127.0.0.1"]


# --- actually listening, and actually not ---

def test_loopback_is_reachable_and_lan_is_not():
    """The core assertion: bound to loopback, the LAN address must be refused."""
    port = _free_port()
    lan = _lan_ip()

    async def scenario():
        server = _server("127.0.0.1", port)
        await server.start()
        try:
            results = {}
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as r:
                    results["loopback"] = r.status
                if lan:
                    try:
                        async with session.get(f"http://{lan}:{port}/health") as r:
                            results["lan"] = r.status
                    except aiohttp.ClientError:
                        results["lan"] = "refused"
            return results, server.bound_addresses
        finally:
            await server.stop()

    results, bound = asyncio.run(scenario())
    assert results["loopback"] == 200, results
    assert bound == ["127.0.0.1"]
    if lan:
        assert results["lan"] == "refused", (
            f"webhook server answered on the LAN address {lan}: {results}"
        )


def test_start_records_what_it_bound():
    port = _free_port()

    async def scenario():
        server = _server("127.0.0.1", port)
        await server.start()
        try:
            return server.bound_addresses
        finally:
            await server.stop()

    assert asyncio.run(scenario()) == ["127.0.0.1"]


def test_unbindable_address_does_not_crash_when_another_succeeds():
    """A bad entry in the list is logged and skipped, not fatal."""
    port = _free_port()

    async def scenario():
        server = _server("192.0.2.1,127.0.0.1", port)  # 192.0.2.0/24 is unroutable
        await server.start()
        try:
            return server.bound_addresses
        finally:
            await server.stop()

    assert asyncio.run(scenario()) == ["127.0.0.1"]


def test_no_bindable_address_raises():
    """Silently serving nothing would look like a working bot."""
    port = _free_port()

    async def scenario():
        server = _server("192.0.2.1", port)
        try:
            await server.start()
        except RuntimeError:
            return "raised"
        finally:
            await server.stop()
        return "started anyway"

    assert asyncio.run(scenario()) == "raised"


# --- structural ---

def test_no_hardcoded_bind_to_all_interfaces():
    """No TCPSite may be constructed with a literal address.

    Checks the AST rather than the text, because start() legitimately mentions
    "0.0.0.0" in the warning that detects an all-interfaces bind.
    """
    import ast
    import inspect

    import core.webhooks as webhooks_module

    tree = ast.parse(inspect.getsource(webhooks_module))
    literals = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "TCPSite"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    literals.append(arg.value)

    assert literals == [], (
        f"TCPSite is constructed with hard-coded address(es) {literals}; it must "
        f"use the configured bind addresses"
    )
