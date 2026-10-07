# path: tests/test_codebase_patterns.py
"""Codebase-wide invariants, not individual bugs.

Several defects in this project recurred because a fix was applied to one site and
not to its siblings: the DM-before-removal ordering was corrected in the automatic
path and left in the manual one, and views were gated one at a time until three
were missed. These tests assert the pattern, so the next instance fails a test
instead of reaching production.

Each allowlist below is deliberately explicit: adding an entry is a conscious
decision that shows up in review, which is the behaviour we want.
"""
import ast
import inspect
import pathlib
import re

import discord

import conftest  # noqa: F401

ROOT = pathlib.Path(conftest.PROJECT_ROOT)


def _source_files():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if any(skip in rel for skip in ("sync-conflict", ".bak", "tests/", "alembic", "backups/")):
            continue
        yield rel, path.read_text()


# --- pattern: every view is either gated or consciously public ---

#: Views intended for any user. Each entry must be genuinely user-facing.
PUBLIC_VIEWS = {
    "ArrivalsRoleView",       # opt-in ping role: each member toggles only their own role
}


def _class_defs():
    """(file, ClassDef, base names) for every class in the codebase."""
    for rel, text in _source_files():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ClassDef):
                bases = [b.id if isinstance(b, ast.Name) else b.attr for b in node.bases
                         if isinstance(b, (ast.Name, ast.Attribute))]
                yield rel, node, bases


def _descendants(roots):
    """Names of classes that inherit, directly or through the codebase's own
    classes, from any of `roots`."""
    classes = list(_class_defs())
    found = set(roots)
    grew = True
    while grew:
        grew = False
        for _, node, bases in classes:
            if node.name not in found and any(b in found for b in bases):
                found.add(node.name)
                grew = True
    return found


def _view_classes():
    """(file, class name, base names) for every discord.ui.View subclass,
    including ones built on a shared base class of our own."""
    views = _descendants({"View"}) - {"View"}
    for rel, node, bases in _class_defs():
        if node.name in views:
            yield rel, node.name, bases, node


def test_every_view_is_gated_or_explicitly_public():
    ungated = []
    # Gated by a shared base: admins only, or only the member who started it.
    admin_gated = _descendants({"AdminOnlyView", "RequesterOnlyView"})
    for rel, name, bases, node in _view_classes():
        if name in admin_gated:
            continue
        if any(
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == "interaction_check"
            for item in node.body
        ):
            continue
        if name in PUBLIC_VIEWS:
            continue
        ungated.append(f"{rel}: {name}")

    assert ungated == [], (
        "these views have no permission gate and are not listed as public - "
        "subclass AdminOnlyView, add an interaction_check, or add to PUBLIC_VIEWS "
        "with a reason: " + str(ungated)
    )


def test_public_view_allowlist_has_no_stale_entries():
    """A removed or renamed view must not linger in the allowlist."""
    existing = {name for _, name, _, _ in _view_classes()}
    stale = sorted(PUBLIC_VIEWS - existing)
    assert stale == [], f"PUBLIC_VIEWS lists views that no longer exist: {stale}"


# --- pattern: never announce a destructive action before it succeeds ---

#: Calls that tell a user something happened.
NOTIFY_CALLS = ("send_user_dm", "dm_user_id", "_dm_tracked", "_notify_without_discord", "_send_farewell_dm", "_send_manual_removal_dm")
#: Calls that actually perform the destructive action. These are mostly handed to
#: run_blocking rather than called, so naming one counts as calling it.
DESTRUCTIVE_CALLS = ("removeFriend", "_remove_friend")


def _removals_and_notices():
    """(file, function, notify lines, destructive lines) for every function."""
    for rel, text in _source_files():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            notify_lines, destructive_lines = [], []
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    label = getattr(inner.func, "attr", None) or getattr(inner.func, "id", None)
                    if label in NOTIFY_CALLS:
                        notify_lines.append(inner.lineno)
                elif isinstance(inner, (ast.Attribute, ast.Name)):
                    if (getattr(inner, "attr", None) or getattr(inner, "id", None)) in DESTRUCTIVE_CALLS:
                        destructive_lines.append(inner.lineno)
            yield rel, node.name, notify_lines, destructive_lines


def test_no_notification_precedes_the_destructive_call():
    """The exact defect fixed twice: DM sent, then removal fails, user misinformed."""
    offenders = []
    for rel, name, notify_lines, destructive_lines in _removals_and_notices():
        if notify_lines and destructive_lines:
            if min(notify_lines) < max(destructive_lines):
                offenders.append(f"{rel}:{name} (notify at {min(notify_lines)}, "
                                 f"destructive at {max(destructive_lines)})")

    assert offenders == [], (
        "a user is told about a removal before it is confirmed; move the "
        "notification after the destructive call: " + str(offenders)
    )


def test_the_ordering_check_sees_both_ways_someone_is_removed():
    """The removals hand removeFriend to run_blocking rather than calling it, so a
    check that only looked for calls would pass whatever order they ran in."""
    seen = {(rel, name) for rel, name, notify_lines, destructive_lines in _removals_and_notices()
            if notify_lines and destructive_lines}
    for name in ("_remove_inactive_user", "remove_plex_user"):
        assert ("plugins/user_mgmt/cog.py", name) in seen, (
            f"{name} tells someone they were removed, but the check found no removal in it"
        )


# --- pattern: embed field values must be bounded ---

def test_joined_embed_values_are_truncated():
    """An over-long field value is a 400, surfaced to users as a generic error."""
    offenders = []
    for rel, text in _source_files():
        for number, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            # value=<something>.join(...) is unbounded unless it is clamped.
            if re.search(r"value=.*\.join\(", line) and "truncate_field" not in line:
                offenders.append(f"{rel}:{number}: {line.strip()[:70]}")
    assert offenders == [], (
        "wrap these in utils.embeds.truncate_field: " + str(offenders)
    )


# --- pattern: persistent views must be registered where the loader reaches ---

def test_no_add_view_inside_setup():
    """core.plugin_manager never calls setup(), so registration there is dead code."""
    offenders = []
    for rel, text in _source_files():
        in_setup = False
        for number, line in enumerate(text.splitlines(), 1):
            if line.startswith("async def setup"):
                in_setup = True
            elif line and not line[0].isspace():
                in_setup = line.startswith("async def setup")
            if in_setup and "add_view(" in line and not line.strip().startswith("#"):
                offenders.append(f"{rel}:{number}")
    assert offenders == [], (
        "move to cog_load or __init__, which add_cog actually triggers: " + str(offenders)
    )


# --- pattern: every config attribute referenced must be declared ---

def test_no_undeclared_config_attribute():
    """An undeclared attribute raises AttributeError, which killed the health loop."""
    from core.config import Config

    declared = set(Config.model_fields)
    offenders = []
    for rel, text in _source_files():
        for number, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            for match in re.finditer(r"services\.config\.([a-z_][a-z0-9_]*)", line):
                if match.group(1) not in declared:
                    offenders.append(f"{rel}:{number} -> config.{match.group(1)}")
    assert offenders == [], "referenced but not declared: " + str(offenders)


# --- pattern: only Forbidden is not enough when editing nicknames ---

def test_nickname_edits_handle_http_exception():
    """Forbidden is a subclass of HTTPException, so catching only it misses a 400."""
    assert issubclass(discord.Forbidden, discord.HTTPException)

    offenders = []
    for rel, text in _source_files():
        if "edit(nick=" not in text:
            continue
        if "except discord.HTTPException" not in text:
            offenders.append(rel)
    assert offenders == [], (
        "these edit nicknames but never catch HTTPException, so an over-long "
        "nickname escapes every handler: " + str(offenders)
    )
