# path: tests/test_no_blocking_plex_calls.py
"""No plexapi call may run on the asyncio event loop.

Regression coverage for: plexapi is fully synchronous with a 30 second default
timeout (plexapi.TIMEOUT), and every call site invoked it directly from a
coroutine. The worst offenders were watch_tracking's 10-second loop and
media_cleanup's full library walk, which blocked the loop - and discord.py's
gateway heartbeat - for as long as Plex took to answer.

This is a static check on purpose. A behavioural test would need a Plex server,
and the defect is structural: a call in the wrong place, not a wrong result.
"""
import ast
import pathlib

import conftest  # noqa: F401

PROJECT_ROOT = pathlib.Path(conftest.PROJECT_ROOT)

# plexapi methods that perform HTTP.
PLEX_METHODS = {
    "sessions", "systemAccounts", "history", "sections", "section", "all",
    "seasons", "episodes", "recentlyAdded", "search", "reload",
    "inviteFriend", "removeFriend",
}
# Constructors that perform an HTTP handshake.
PLEX_CTORS = {"PlexServer", "MyPlexAccount"}
# Receiver names that denote a plexapi object.
PLEX_RECEIVERS = {
    "plex", "plex_server", "library", "section", "account",
    "item", "episode", "season", "show", "movie",
}


def _iter_source_files():
    for path in sorted(PROJECT_ROOT.rglob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if any(skip in rel for skip in ("sync-conflict", ".bak", "tests/", "alembic")):
            continue
        yield rel, path.read_text()


def _receiver_names(func_node):
    """Every name in the receiver chain: self.services.plex_server.sessions
    -> {"plex_server", "services", "self"}.

    Collects the whole path rather than just the left-most name, which for a
    method on self would always be "self" and match nothing. Stops at a Call, so
    result.scalars().all() yields nothing and is correctly ignored.
    """
    names = set()
    current = func_node.value
    while isinstance(current, ast.Attribute):
        names.add(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        names.add(current.id)
    return names


def _is_plex_call(node, line):
    if isinstance(node.func, ast.Name):
        return node.func.id in PLEX_CTORS
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr in PLEX_CTORS:
        return True
    if node.func.attr not in PLEX_METHODS and node.func.attr != "delete":
        return False
    # Narrow by receiver so SQLAlchemy .all()/.delete() and aiohttp .delete()
    # are not mistaken for plexapi.
    names = _receiver_names(node.func)
    if node.func.attr == "delete":
        # Only a plexapi media object's .delete(); never a session or HTTP client.
        return bool(names & {"item", "episode", "season", "show", "movie"})
    return bool(names & PLEX_RECEIVERS) or any("plex" in n.lower() for n in names)


class _Auditor(ast.NodeVisitor):
    def __init__(self, rel, lines):
        self.rel = rel
        self.lines = lines
        self.stack = []
        self.findings = []

    def _func(self, node, is_async):
        self.stack.append(is_async)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._func(node, False)

    def visit_AsyncFunctionDef(self, node):
        self._func(node, True)

    def visit_Call(self, node):
        in_async = bool(self.stack) and self.stack[-1]
        if in_async:
            line = self.lines[node.lineno - 1]
            # Passing a bound method to run_blocking is not a call to it, so only
            # an actual invocation on this line is a finding.
            if _is_plex_call(node, line) and "run_blocking" not in line:
                self.findings.append(f"{self.rel}:{node.lineno}: {line.strip()[:90]}")
        self.generic_visit(node)


def _audit():
    findings = []
    for rel, text in _iter_source_files():
        auditor = _Auditor(rel, text.splitlines())
        auditor.visit(ast.parse(text))
        findings.extend(auditor.findings)
    return findings


def test_no_plexapi_call_inside_a_coroutine():
    findings = _audit()
    assert findings == [], (
        "plexapi calls still on the event loop:\n  " + "\n  ".join(findings)
    )


def test_blocking_helper_exists_and_is_used():
    from core.blocking import run_blocking

    assert callable(run_blocking)
    users = [
        rel for rel, text in _iter_source_files()
        if "run_blocking(" in text and "def run_blocking" not in text
    ]
    assert len(users) >= 8, f"expected the helper to be used widely, found {users}"


def test_auditor_would_catch_a_regression():
    """Guard the guard: the audit must fail on a reintroduced blocking call."""
    sample = (
        "async def f(self):\n"
        "    return self.services.plex_server.sessions()\n"
    )
    auditor = _Auditor("sample.py", sample.splitlines())
    auditor.visit(ast.parse(sample))
    assert auditor.findings, "auditor failed to flag a direct plexapi call"


def test_auditor_accepts_a_wrapped_call():
    sample = (
        "async def f(self):\n"
        "    return await run_blocking(self.services.plex_server.sessions)\n"
    )
    auditor = _Auditor("sample.py", sample.splitlines())
    auditor.visit(ast.parse(sample))
    assert auditor.findings == []


def test_auditor_ignores_sqlalchemy_and_http_lookalikes():
    """.all() and .delete() are everywhere in this codebase and are not plexapi."""
    sample = (
        "async def f(self, session, result):\n"
        "    rows = result.scalars().all()\n"
        "    await session.delete(rows[0])\n"
        "    async with await self.services.api.radarr.delete(url) as r:\n"
        "        pass\n"
    )
    auditor = _Auditor("sample.py", sample.splitlines())
    auditor.visit(ast.parse(sample))
    assert auditor.findings == []


def test_check_item_for_cleanup_is_synchronous():
    """It performs per-show season/episode requests, so it must not be awaited
    from the loop - it is called from inside a worker thread instead.
    """
    import inspect

    from plugins.media_cleanup.cog import MediaCleanupCog

    assert not inspect.iscoroutinefunction(MediaCleanupCog.check_item_for_cleanup)
    assert not inspect.iscoroutinefunction(MediaCleanupCog._scan_libraries_for_cleanup)


def test_reconnect_plex_is_awaitable():
    """It constructs a PlexServer, and is called from a 10-second task loop."""
    import inspect

    from core.services import BotServices

    assert inspect.iscoroutinefunction(BotServices.reconnect_plex)
