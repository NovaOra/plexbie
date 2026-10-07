# path: tests/test_commands.py
"""The Discord command surface: what users see, and who can see it.

Written for publishing the bot, and covering four things that were wrong:

  * commands were registered in the global scope as well as the guild scope.
    Discord merges the two, so 15 of them appeared twice in the picker. Nothing
    in the code created those globals any more - an older version had - and
    nothing removed them either.
  * not one command set default_member_permissions, so every admin command was
    visible to every member. They were refused at runtime, but the refusal was
    logged at ERROR with a traceback and answered with "An error occurred...
    try again later", which is not what happened.
  * descriptions carried "(Admin only)" as a substitute for actually gating.
  * two plugins claimed the top-level name /request, which silently prevents one
    of them from loading.

These read the real discord.py objects off each Cog class rather than parsing
source, so they describe what Discord would actually be sent.
"""
import ast
import importlib
import inspect
import json
import pathlib

import conftest  # noqa: F401

ROOT = pathlib.Path(conftest.PROJECT_ROOT)

#: Commands any member may see and run. Everything else must be admin-gated, so
#: adding a name here is a deliberate decision about who can use the bot.
MEMBER_FACING = {
    "join-plex",          # request Plex access
    "request",            # request media
    "watchparty-stats",   # your own stats
}


def _cog_class_name(plugin: str) -> str:
    return "".join(word.capitalize() for word in plugin.split("_")) + "Cog"


def _plugin_dirs():
    for path in sorted((ROOT / "plugins").iterdir()):
        if (path / "cog.py").exists():
            yield path


def _top_level_commands():
    """(plugin, command_or_group) for every top-level entry, as Discord sees it."""
    for path in _plugin_dirs():
        module = importlib.import_module(f"plugins.{path.name}.cog")
        cls = getattr(module, _cog_class_name(path.name), None)
        if cls is None:
            continue
        for command in getattr(cls, "__cog_app_commands__", []):
            yield path.name, command


def _is_admin_gated(command) -> bool:
    perms = command.default_permissions
    return bool(perms and perms.administrator)


# --- one name, one owner ---

def test_no_two_plugins_claim_the_same_top_level_name():
    """A collision stops one plugin loading, and the loader used to say only
    "Failed to load plugin X" - which sends you looking in the wrong place.

    Checked across every plugin, enabled or not: a disabled plugin is a trap for
    whoever enables it next. seerr's group was named `request`, colliding with
    media_requests' /request.
    """
    owners = {}
    clashes = []
    for plugin, command in _top_level_commands():
        if command.name in owners:
            clashes.append(f"/{command.name}: {owners[command.name]} and {plugin}")
        else:
            owners[command.name] = plugin
    assert clashes == [], "top-level command names must be unique:\n  " + "\n  ".join(clashes)


# --- visibility ---

def test_every_command_is_guild_only():
    """None of these mean anything in a DM, where they fail confusingly."""
    offenders = [
        f"/{command.name} ({plugin})"
        for plugin, command in _top_level_commands()
        if not command.guild_only
    ]
    assert offenders == [], (
        "add guild_only so Discord hides these in DMs:\n  " + "\n  ".join(offenders)
    )


def test_admin_commands_are_hidden_from_members():
    """Runtime checks refuse the command; default_permissions stops Discord
    offering it in the first place.
    """
    ungated = [
        f"/{command.name} ({plugin})"
        for plugin, command in _top_level_commands()
        if command.name not in MEMBER_FACING and not _is_admin_gated(command)
    ]
    assert ungated == [], (
        "these are visible to every member; add "
        "@app_commands.default_permissions(administrator=True) or list them in "
        "MEMBER_FACING:\n  " + "\n  ".join(ungated)
    )


def test_member_facing_commands_are_not_gated():
    """The allowlist must not quietly hide something users need."""
    wrongly_gated = [
        f"/{command.name} ({plugin})"
        for plugin, command in _top_level_commands()
        if command.name in MEMBER_FACING and _is_admin_gated(command)
    ]
    assert wrongly_gated == [], (
        "listed as member-facing but gated to admins:\n  " + "\n  ".join(wrongly_gated)
    )


def test_no_description_advertises_admin_only():
    """"(Admin only)" in a description was a workaround for not gating it.

    Kept as a test because the phrase is what you reach for when the gate is
    missing, so its reappearance is a useful signal.
    """
    offenders = []
    for plugin, command in _top_level_commands():
        text = (command.description or "").lower()
        if "admin only" in text or "(admin)" in text:
            offenders.append(f"/{command.name} ({plugin}): {command.description!r}")
    assert offenders == [], (
        "gate these instead of describing them as restricted:\n  " + "\n  ".join(offenders)
    )


# --- grouping ---

def test_cleanup_is_a_single_entry_with_its_subcommands():
    """Five top-level cleanup commands became one group."""
    groups = {
        command.name: command
        for _, command in _top_level_commands()
        if command.name == "cleanup"
    }
    assert "cleanup" in groups, "/cleanup should be a group"
    cleanup = groups["cleanup"]

    names = set()
    def walk(node, prefix=""):
        for child in getattr(node, "commands", []):
            qualified = f"{prefix}{child.name}"
            names.add(qualified)
            walk(child, f"{qualified} ")
    walk(cleanup)

    assert {"panel", "config"} <= names, f"missing panel/config: {sorted(names)}"
    assert {"exempt add", "exempt list", "exempt remove"} <= names, (
        f"exempt subgroup incomplete: {sorted(names)}"
    )
    assert _is_admin_gated(cleanup) and cleanup.guild_only


def test_watchparty_stays_split_on_purpose():
    """Not a group, and that is deliberate.

    Discord applies default_member_permissions to the top-level entry only, so a
    /watchparty group could not have an admin-only `active` alongside a
    member-visible `stats`. Two flat commands keep the visibility correct.
    """
    by_name = {command.name: command for _, command in _top_level_commands()}
    assert "watchparty-active" in by_name and "watchparty-stats" in by_name
    assert _is_admin_gated(by_name["watchparty-active"])
    assert not _is_admin_gated(by_name["watchparty-stats"])


# --- the documentation must not drift ---

def test_plugin_json_command_lists_match_the_real_commands():
    """plugin.json is the closest thing to user documentation in the repo."""
    real = {command.name for _, command in _top_level_commands()}
    stale = []
    for path in _plugin_dirs():
        manifest = path / "plugin.json"
        if not manifest.exists():
            continue
        try:
            listed = json.loads(manifest.read_text()).get("commands", [])
        except json.JSONDecodeError as e:
            stale.append(f"{path.name}/plugin.json is not valid JSON: {e}")
            continue
        for entry in listed:
            if not entry.startswith("/"):
                continue
            top = entry[1:].split(" - ")[0].split()[0]
            if top not in real:
                stale.append(f"{path.name}/plugin.json documents /{top}, which does not exist")
    assert stale == [], "\n  " + "\n  ".join(stale)


# --- registration and error handling ---

def _bot_source():
    return (ROOT / "bot.py").read_text()


def test_startup_removes_stale_global_commands():
    """Discord merges global and guild commands, so leftovers show up twice."""
    lookup = (ROOT / "core" / "discord_lookup.py").read_text()
    assert "fetch_commands()" in lookup, (
        "nothing checks the global scope, so stale global commands stay forever"
    )
    assert "bulk_upsert_global_commands(bot.application_id, payload=[])" in lookup, (
        "stale global commands are detected but never removed"
    )
    assert "stale global command" in _bot_source(), "the clean-up isn't logged"


def test_refusals_are_not_reported_as_crashes():
    """A denied command logged an ERROR with a traceback and told the user
    "An error occurred... please try again later", which was simply untrue.
    """
    source = _bot_source()
    tree = ast.parse(source)
    handler = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "on_app_command_error"
    )
    body = ast.get_source_segment(source, handler) or ""
    for expected in ("MissingPermissions", "NoPrivateMessage"):
        assert expected in body, f"the handler does not distinguish {expected}"
    assert "don't have permission" in body, "no honest message for a refusal"
