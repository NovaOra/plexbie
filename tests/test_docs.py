# path: tests/test_docs.py
"""Documentation that drifts is worse than none, because it is believed.

The command list in plugin.json had accumulated five entries for commands that
did not exist - including three in watch_tracking, which has never had any
commands at all. These tests hold README.md and config/.env.example to the same
standard: they are checked against the code, not against memory.
"""
import ast
import importlib
import json
import pathlib
import re

import conftest  # noqa: F401

ROOT = pathlib.Path(conftest.PROJECT_ROOT)
README = ROOT / "README.md"
ENV_EXAMPLE = ROOT / "config" / ".env.example"

ENV_READ = re.compile(
    r'(?:os\.getenv|os\.environ\.get|_env_int(?:_default)?|_env_bool|(?<![\w.])env)\(\s*["\']([A-Z][A-Z0-9_]*)["\']'
)


def _source_files():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if any(skip in rel for skip in ("sync-conflict", ".bak", "tests/", "backups/")):
            continue
        yield rel, path.read_text()


def _env_vars_the_code_reads():
    found = set()
    for _, text in _source_files():
        found.update(ENV_READ.findall(text))
    return found


def _env_vars_documented():
    documented = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        documented.add(line.split("=", 1)[0].strip())
    return documented


def test_env_example_exists():
    assert ENV_EXAMPLE.exists(), (
        "config/.env.example is how anyone else configures this bot"
    )


def test_every_setting_the_code_reads_is_documented():
    missing = sorted(_env_vars_the_code_reads() - _env_vars_documented())
    assert missing == [], (
        "read from the environment but absent from config/.env.example:\n  "
        + "\n  ".join(missing)
    )


def test_env_example_invents_nothing():
    """A documented setting that nothing reads sends people chasing ghosts."""
    invented = sorted(_env_vars_documented() - _env_vars_the_code_reads())
    assert invented == [], (
        "documented in config/.env.example but never read:\n  " + "\n  ".join(invented)
    )


def test_env_example_carries_no_values_for_secrets():
    """It is a template. A filled-in token here would be committed."""
    secretish = ("TOKEN", "PASSWORD", "SECRET", "API_KEY")
    filled = []
    for line in ENV_EXAMPLE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if any(word in name for word in secretish) and value.strip():
            filled.append(name)
    assert filled == [], "these have values in the template:\n  " + "\n  ".join(filled)


# --- README command tables ---

def _cog_class_name(plugin: str) -> str:
    return "".join(word.capitalize() for word in plugin.split("_")) + "Cog"


def _enabled_commands():
    """Fully-qualified names of every invocable command in an enabled plugin."""
    names = set()
    for path in sorted((ROOT / "plugins").iterdir()):
        if not (path / "cog.py").exists():
            continue
        manifest = path / "plugin.json"
        enabled = True
        if manifest.exists():
            try:
                enabled = json.loads(manifest.read_text()).get("enabled", True)
            except json.JSONDecodeError:
                enabled = True
        if not enabled:
            continue
        module = importlib.import_module(f"plugins.{path.name}.cog")
        cls = getattr(module, _cog_class_name(path.name), None)
        if cls is None:
            continue
        for command in getattr(cls, "__cog_app_commands__", []):
            def walk(node, prefix):
                children = getattr(node, "commands", None)
                if children:
                    for child in children:
                        walk(child, f"{prefix} {child.name}")
                else:
                    names.add(prefix)
            walk(command, command.name)
    return names


def _readme_commands():
    """Every /command named in the first cell of a README table row."""
    found = set()
    for line in README.read_text().splitlines():
        if not line.startswith("| `/"):
            continue
        first = line.split("|")[1].strip()
        match = re.match(r"`/([^`]+)`", first)
        if match:
            found.add(match.group(1).strip())
    return found


def test_readme_documents_every_enabled_command():
    missing = sorted(_enabled_commands() - _readme_commands())
    assert missing == [], (
        "these commands exist but are not in the README tables:\n  "
        + "\n  ".join("/" + name for name in missing)
    )


def test_readme_documents_no_command_that_does_not_exist():
    from_readme = _readme_commands()
    # Disabled plugins' commands are listed in the Plugins section, not the tables.
    real = _enabled_commands()
    for path in sorted((ROOT / "plugins").iterdir()):
        if not (path / "cog.py").exists():
            continue
        module = importlib.import_module(f"plugins.{path.name}.cog")
        cls = getattr(module, _cog_class_name(path.name), None)
        if cls is None:
            continue
        for command in getattr(cls, "__cog_app_commands__", []):
            def walk(node, prefix):
                children = getattr(node, "commands", None)
                if children:
                    for child in children:
                        walk(child, f"{prefix} {child.name}")
                else:
                    real.add(prefix)
            walk(command, command.name)

    phantom = sorted(from_readme - real)
    assert phantom == [], (
        "the README documents commands that do not exist:\n  "
        + "\n  ".join("/" + name for name in phantom)
    )


def test_readme_covers_the_topics_a_new_user_needs():
    """Cheap structural check, so a rewrite cannot quietly drop a section."""
    text = README.read_text()
    for heading in (
        "## Requirements",
        "## Quick start",
        "## Configuration",
        "## Commands",
        "## Webhooks",
        "## Plugins",
        "## Architecture",
        "## Development",
        "## Security",
        "## Limitations",
    ):
        assert heading in text, f"README is missing the section {heading!r}"


def test_readme_lists_exactly_the_plugins_that_exist():
    """The README's plugin table names every plugin in plugins/ and nothing else (the old
    stub plugins are gone, so it mustn't promise or warn about any)."""
    import re
    text = README.read_text()
    table = text.split("### Active by default", 1)[1].split("\n\n", 2)[1]
    listed = set(re.findall(r"^\| `([a-z_]+)` \|", table, flags=re.M))
    real = {p.parent.name for p in README.parent.joinpath("plugins").glob("*/plugin.json")}
    assert listed == real, f"README lists {sorted(listed - real)} that don't exist and misses {sorted(real - listed)}"
    assert "not yet implemented" not in text.lower()
