# path: tests/test_no_blocking_file_io.py
"""No unreviewed synchronous file I/O may run on the asyncio event loop.

Sibling of test_no_blocking_plex_calls.py, which only ever looked at plexapi.
Synchronous file I/O was never audited, which is how a 661 KB JSON
read-modify-write sat on the loop unnoticed.

Two things the Plex audit does not need but this one does:

* It follows calls into sync helpers. Most of the file I/O here is not written
  inline in a coroutine; it sits in a sync method (MediaTrackingManager's
  save_tracking_data) that coroutines call. A direct-call audit sees none of
  it. So any sync function that does file I/O, directly or through another
  project function, taints its name, and a coroutine calling that name is a
  finding. Resolution is by bare name, so it over-approximates: that is the
  safe direction for an audit backed by a reviewed allowlist.

* It cannot demand zero. Most sites cost well under a millisecond and are not
  worth a thread hop, so each one that remains is listed in KNOWN_SITES with
  the cost measured on the maintainer's server. A new site fails the test until someone
  measures it and either moves it off the loop or lists it; a listed site that
  disappears fails too, so the list only ever shrinks with the code.

Not in scope: metadata calls (exists, stat, iterdir, glob, mkdir). They are
everywhere, cost microseconds on the cache SSD, and would drown the rest.
"""
import ast
import pathlib

import conftest  # noqa: F401

PROJECT_ROOT = pathlib.Path(conftest.PROJECT_ROOT)

PATH_IO = {"read_text", "write_text", "read_bytes", "write_bytes"}
MODULE_IO = {
    "json": {"load", "dump"},
    "io": {"open"},
    "os": {"fsync"},
    "shutil": {"copy", "copy2", "copyfile", "copyfileobj", "copytree", "move", "rmtree"},
}

# Every accepted site, keyed "file::coroutine::call". Costs are medians (p95)
# measured 2026-09-30 on the maintainer's server against the live files; config/ and the
# container layer are on the cache SSD. Anything writing to /watch or /library
# goes to the array through shfs and is not acceptable here: a 6.8 MB cover took
# 137 ms median, 388 ms max, and a 400-byte hint file 49 ms at p95.
KNOWN_SITES = {
    # Startup only, once per plugin.  0.02 ms.
    "core/plugin_manager.py::load_plugin::open": "startup, 0.02 ms",
    "core/plugin_manager.py::load_plugin::json.load": "startup, 0.02 ms",
    # One-shot legacy migration at startup.
    "database/session.py::_migrate_media_requests_to_kv::read_text": "startup, one-shot",
    # Before anything else runs: renamed settings in config/.env (a few lines).  < 1 ms.
    "bot.py::main::rename_env_keys()": "startup, once, < 1 ms",
    # media_tracking.json, 58 KB: load 0.45 ms, save 2.7 ms including fsync.
    # Rare (a Sonarr/Radarr grab or a request); moving it off JSON is §7.3.
    "plugins/media_cleanup/cog.py::enforce_request_monitor_cleanup::_prune_media_tracking_cache()": "2.7 ms, daily",
    "plugins/media_requests/cog.py::_register_with_tracking::register_request()": "2.7 ms per request",
    "plugins/new_media_added/cog.py::_arrivals_locked::mark_episode_available()": "2.7 ms per arrival",
    "plugins/new_media_added/cog.py::_check_if_monitored::load_tracking_data()": "0.45 ms per arrival",
    "plugins/new_media_added/cog.py::_send_requester_availability_dm::save_tracking_data()": "2.7 ms per DM",
    "webhooks/radarr_handler.py::_handle_grab::save_tracking_data()": "2.7 ms per grab",
    "webhooks/sonarr_handler.py::_handle_grab::save_tracking_data()": "2.7 ms per grab",
    # bookshelf hint file, read from the array: 0.23 ms (0.27).
    "plugins/bookshelf_processor/cog.py::process_item::open": "0.23 ms per book",
    "plugins/bookshelf_processor/cog.py::process_item::json.load": "0.23 ms per book",
    # watch_tracking small files: watch_streaks.json 912 B.
    "plugins/watch_tracking/cog.py::update_watch_streaks::read_text": "0.22 ms",
    "plugins/watch_tracking/cog.py::update_watch_streaks::write_text": "0.32 ms",
    "plugins/watch_tracking/cog.py::update_streaks_display::read_text": "0.22 ms",
    # user_aliases.json: a stat per call, re-read only when mtime/size change.
    "plugins/watch_tracking/cog.py::update_now_watching::_get_display_name()": "0.06 ms stat",
    "plugins/watch_tracking/cog.py::update_leaderboard::_get_display_name()": "0.06 ms stat",
    "plugins/watch_tracking/cog.py::update_leaderboard::_apply_aliases_to_users()": "0.06 ms stat",
    "plugins/watch_tracking/cog.py::_post_streaks_update::_get_display_name()": "0.06 ms stat",
}


def _iter_source_files():
    for path in sorted(PROJECT_ROOT.rglob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if any(skip in rel for skip in ("sync-conflict", ".bak", "tests/", "alembic")):
            continue
        yield rel, path.read_text()


def _io_primitive(func):
    """The name of the file I/O primitive this call is, or None."""
    if isinstance(func, ast.Name):
        return "open" if func.id == "open" else None
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in PATH_IO:
        return func.attr
    if isinstance(func.value, ast.Name) and func.attr in MODULE_IO.get(func.value.id, ()):
        return f"{func.value.id}.{func.attr}"
    return None


def _callee_name(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _own_calls(func_node):
    """Calls made by this function's own body, and which of them are awaited.

    Nested defs and lambdas are skipped: they run when called, typically in a
    worker thread via run_blocking, not as part of this body.
    """
    calls, awaited = [], set()
    todo = list(ast.iter_child_nodes(func_node))
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            awaited.add(id(node.value))
        if isinstance(node, ast.Call):
            calls.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return calls, awaited


def _tainted_sync_names(trees):
    """Names of sync functions that do file I/O, directly or transitively."""
    tainted, callees = set(), {}
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            calls, _ = _own_calls(node)
            if any(_io_primitive(c.func) for c in calls):
                tainted.add(node.name)
            callees.setdefault(node.name, set()).update(
                n for n in (_callee_name(c.func) for c in calls) if n
            )
    changed = True
    while changed:
        changed = False
        for name, called in callees.items():
            if name not in tainted and called & tainted:
                tainted.add(name)
                changed = True
    return tainted


def _audit(sources):
    """sources: iterable of (rel, text). Returns sorted finding keys."""
    parsed = [(rel, ast.parse(text)) for rel, text in sources]
    tainted = _tainted_sync_names(tree for _, tree in parsed)
    findings = set()
    for rel, tree in parsed:
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            calls, awaited = _own_calls(node)
            for call in calls:
                what = _io_primitive(call.func)
                if what is None and id(call) not in awaited:
                    # An awaited call is a coroutine - audited in its own right -
                    # even when a sync function elsewhere shares its name.
                    name = _callee_name(call.func)
                    if name in tainted:
                        what = f"{name}()"
                if what:
                    findings.add(f"{rel}::{node.name}::{what}")
    return sorted(findings)


def test_no_unreviewed_file_io_inside_a_coroutine():
    new = [f for f in _audit(_iter_source_files()) if f not in KNOWN_SITES]
    assert new == [], (
        "synchronous file I/O on the event loop. Measure it: move it into "
        "run_blocking(), or add it to KNOWN_SITES with its cost:\n  " + "\n  ".join(new)
    )


def test_known_sites_are_not_stale():
    """A fixed site must leave the list, so the list cannot hide a regression."""
    found = set(_audit(_iter_source_files()))
    stale = sorted(k for k in KNOWN_SITES if k not in found)
    assert stale == [], "no longer found - remove from KNOWN_SITES:\n  " + "\n  ".join(stale)


def test_array_writes_stay_off_the_loop():
    """The two measured-expensive sites, named so a regression reads plainly."""
    found = _audit(_iter_source_files())
    offenders = [
        f for f in found
        if f.startswith("plugins/bookshelf_processor/cog.py::download_cover::")
        or f.startswith("plugins/media_requests/cog.py::_submit_to_download::")
    ]
    assert offenders == [], offenders


# --- guard the guard ---------------------------------------------------------

def _findings(sample):
    return _audit([("sample.py", sample)])


def test_auditor_flags_direct_io():
    sample = (
        "async def f(path):\n"
        "    path.write_text('x')\n"
        "    with open(path) as fh:\n"
        "        json.load(fh)\n"
        "    shutil.copy2(path, path)\n"
    )
    assert _findings(sample) == [
        "sample.py::f::json.load", "sample.py::f::open",
        "sample.py::f::shutil.copy2", "sample.py::f::write_text",
    ]


def test_auditor_follows_sync_helpers():
    """The media_tracking shape: coroutine -> sync method -> sync method -> dump."""
    sample = (
        "class Tracker:\n"
        "    def save(self):\n"
        "        with open('t.json', 'w') as fh:\n"
        "            json.dump({}, fh)\n"
        "    def register(self):\n"
        "        self.save()\n"
        "async def handler(tracker):\n"
        "    tracker.register()\n"
    )
    assert _findings(sample) == ["sample.py::handler::register()"]


def test_auditor_accepts_work_moved_to_a_thread():
    sample = (
        "def save(p):\n"
        "    p.write_bytes(b'x')\n"
        "async def f(p):\n"
        "    await run_blocking(save, p)\n"
        "    await run_blocking(lambda: p.write_text('x'))\n"
        "    def _inner():\n"
        "        p.write_text('x')\n"
        "    await asyncio.to_thread(_inner)\n"
    )
    assert _findings(sample) == []


def test_auditor_ignores_awaited_coroutine_sharing_a_name():
    """new_media_added's cog has an async save_tracking_data backed by the DB,
    next to MediaTrackingManager's sync one that writes JSON."""
    sample = (
        "class Manager:\n"
        "    def save_tracking_data(self):\n"
        "        open('t.json', 'w')\n"
        "class Cog:\n"
        "    async def save_tracking_data(self):\n"
        "        await kv_set_many('ns', {})\n"
        "    async def on_event(self):\n"
        "        await self.save_tracking_data()\n"
    )
    assert _findings(sample) == []


def test_auditor_ignores_string_json_and_metadata():
    sample = (
        "async def f(p, raw):\n"
        "    json.loads(raw)\n"
        "    json.dumps({})\n"
        "    p.exists()\n"
        "    p.stat()\n"
        "    p.mkdir(exist_ok=True)\n"
    )
    assert _findings(sample) == []
