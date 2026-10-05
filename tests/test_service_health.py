# path: tests/test_service_health.py
"""Service health monitoring must survive the outage it reports.

Regression coverage for: the failure path read config.health_max_failures and
config.health_alert_cooldown, neither of which Config declared. Both raise
AttributeError, and discord.ext.tasks stops a Loop on an unhandled exception - so
the monitor died at the exact moment of the first outage and never checked again
until a restart.
"""
import asyncio
import inspect
import re
from datetime import datetime, timezone

import conftest  # noqa: F401

from core.config import Config


# --- the missing fields ---

def test_health_config_fields_exist():
    config = Config()
    for name in ("health_check_interval", "health_max_failures", "health_alert_cooldown"):
        assert hasattr(config, name), f"config.{name} raises AttributeError at runtime"


def test_health_defaults_are_sane():
    config = Config()
    assert config.health_max_failures >= 1
    assert config.health_alert_cooldown > 0
    assert config.health_check_interval > 0


def test_every_config_attribute_the_plugin_uses_is_declared():
    """The audit that found this: compare references against declared fields."""
    import pathlib

    declared = set(Config.model_fields)
    root = pathlib.Path(conftest.PROJECT_ROOT)
    missing = []
    for path in sorted(root.glob("plugins/*/cog.py")):
        text = path.read_text()
        for number, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            for match in re.finditer(r"services\.config\.([a-z_][a-z0-9_]*)", line):
                name = match.group(1)
                if name not in declared:
                    missing.append(f"{path.relative_to(root)}:{number} -> config.{name}")
    assert missing == [], "referenced but not declared:\n  " + "\n  ".join(missing)


# --- the loop must not die ---

def test_loop_guards_each_check():
    """An exception escaping the loop body stops the Loop permanently."""
    from plugins.service_health.cog import ServiceHealthCog

    source = inspect.getsource(ServiceHealthCog.health_check_loop.coro)
    assert "try:" in source and "except Exception" in source, (
        "an unguarded check would stop the monitor on any unexpected error"
    )


def test_a_raising_check_does_not_stop_the_loop():
    """Behavioural: one failing service must not prevent the other being checked."""
    from plugins.service_health.cog import ServiceHealthCog

    cog = object.__new__(ServiceHealthCog)
    updated = []

    async def boom():
        raise RuntimeError("plex exploded")

    async def fine():
        return "HEALTHY", None

    async def record(name, status, error, now):
        updated.append(name)

    cog._check_plex = boom
    cog._check_tautulli = fine
    cog._update_health = record

    # Must not raise - that is what would kill the Loop.
    asyncio.run(ServiceHealthCog.health_check_loop.coro(cog))

    assert updated == ["tautulli"], (
        f"tautulli should still have been checked after plex raised, got {updated}"
    )
