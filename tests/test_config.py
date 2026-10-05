# path: tests/test_config.py
"""Integer config coercion must be loud, not silent.

Regression coverage for: a live .env had BOT_OWNER_ID=novaora - a Plex username
where a Discord snowflake belonged. _int_or_none() caught the ValueError and
returned None, so the bot started cleanly and never applied the setting, with
nothing in the logs to explain why. 25 fields shared that behaviour.
"""
import logging
import os

import conftest  # noqa: F401

from core.config import Config, _env_int, _env_int_default, _int_or_none

INT_FIELD_ENV_VARS = [
    "GUILD_ID", "BOT_OWNER_ID", "ADMIN_CHANNEL_ID", "STATS_CHANNEL_ID",
    "UPDATES_CHANNEL_ID", "PLEX_MEMBER_ROLE_ID", "ADMIN_ROLE_ID",
    "NOW_WATCHING_MESSAGE_ID",
    "WATCH_STREAK_MESSAGE_ID", "LEADERBOARD_MESSAGE_ID", "WATCH_PARTY_CHANNEL_ID",
]


class _CaptureWarnings:
    """Collect warnings emitted by core.config for the duration of a block."""

    def __init__(self):
        self.records = []

    def __enter__(self):
        self.logger = logging.getLogger("core.config")
        self.handler = logging.Handler()
        self.handler.emit = self.records.append
        self.logger.addHandler(self.handler)
        self.previous_level = self.logger.level
        self.logger.setLevel(logging.WARNING)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.previous_level)

    @property
    def messages(self):
        return [record.getMessage() for record in self.records]


def _with_env(name, value):
    """Set an env var, returning a restore callable."""
    previous = os.environ.get(name)

    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value

    def restore():
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    return restore


# --- the original bug ---

def test_non_numeric_value_is_discarded_with_a_warning():
    restore = _with_env("BOT_OWNER_ID", "novaora")
    try:
        with _CaptureWarnings() as captured:
            assert _env_int("BOT_OWNER_ID") is None
        assert captured.messages, "a discarded value must warn"
        message = captured.messages[0]
        assert "BOT_OWNER_ID" in message, "warning must name the variable"
        assert "novaora" in message, "warning must show the rejected value"
    finally:
        restore()


def test_valid_value_parses_without_warning():
    restore = _with_env("BOT_OWNER_ID", "123456789012345678")
    try:
        with _CaptureWarnings() as captured:
            assert _env_int("BOT_OWNER_ID") == 123456789012345678
        assert captured.messages == [], "a valid value must stay quiet"
    finally:
        restore()


def test_empty_value_is_quiet():
    """Empty means "not configured", which is normal - warning would be noise."""
    restore = _with_env("BOT_OWNER_ID", "")
    try:
        with _CaptureWarnings() as captured:
            assert _env_int("BOT_OWNER_ID") is None
        assert captured.messages == []
    finally:
        restore()


def test_absent_value_is_quiet():
    restore = _with_env("BOT_OWNER_ID", None)
    try:
        with _CaptureWarnings() as captured:
            assert _env_int("BOT_OWNER_ID") is None
        assert captured.messages == []
    finally:
        restore()


def test_warning_names_its_source():
    with _CaptureWarnings() as captured:
        _int_or_none("garbage", source="config.yml:guild_id")
    assert "config.yml:guild_id" in captured.messages[0]


# --- fields that previously crashed the whole bot on a typo ---

def test_bad_int_with_default_falls_back_instead_of_raising():
    restore = _with_env("WEBHOOK_PORT", "not-a-port")
    try:
        with _CaptureWarnings() as captured:
            assert _env_int_default("WEBHOOK_PORT", 8080) == 8080
        assert captured.messages, "the fallback must be announced"
    finally:
        restore()


def test_bad_int_does_not_prevent_config_construction():
    """Previously int(os.getenv(...)) raised and the bot never started."""
    restore = _with_env("HEALTH_CHECK_INTERVAL", "5 minutes")
    try:
        assert Config().health_check_interval == 300
    finally:
        restore()


def test_valid_override_is_still_honoured():
    restore = _with_env("HEALTH_CHECK_INTERVAL", "600")
    try:
        assert Config().health_check_interval == 600
    finally:
        restore()


def test_absent_uses_documented_default():
    restore = _with_env("HEALTH_CHECK_INTERVAL", None)
    try:
        assert Config().health_check_interval == 300
    finally:
        restore()


# --- structural guarantees ---

def test_no_int_field_uses_an_unguarded_int_call():
    """A bare int(os.getenv(...)) reintroduces the crash-on-typo behaviour."""
    import inspect

    import core.config as config_module

    source = inspect.getsource(config_module)
    body = source.split("class Config", 1)[1]
    assert "int(os.getenv" not in body, (
        "Config fields must use _env_int / _env_int_default so a bad value warns "
        "instead of raising"
    )


def test_every_int_env_var_is_read_through_the_guarded_helper():
    import inspect

    import core.config as config_module

    source = inspect.getsource(config_module)
    for name in INT_FIELD_ENV_VARS:
        assert f'_env_int("{name}")' in source, (
            f"{name} is not read via _env_int, so a bad value would be discarded "
            f"silently"
        )


def test_settings_that_would_do_something_drastic_fall_back_safely():
    """@everyone's id as the admin role made every member an admin; removal days
    under the warning days removed people without a warning; 0 intervals spun."""
    import os
    from core.config import Config, MIN_NOTICE_DAYS
    keys = ("GUILD_ID", "ADMIN_ROLE_ID", "PLEX_MEMBER_ROLE_ID", "INACTIVITY_WARNING_DAYS",
            "INACTIVITY_REMOVAL_DAYS", "HEALTH_CHECK_INTERVAL")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update(GUILD_ID="111", ADMIN_ROLE_ID="111", PLEX_MEMBER_ROLE_ID="222",
                          INACTIVITY_WARNING_DAYS="25", INACTIVITY_REMOVAL_DAYS="3", HEALTH_CHECK_INTERVAL="0")
        c = Config()
        assert c.admin_role_id is None and c.plex_member_role_id == 222
        assert c.inactivity_removal_days == 25 + MIN_NOTICE_DAYS
        assert c.health_check_interval == 30
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_renamed_settings_keep_working_and_env_gets_the_new_names():
    import os
    import tempfile
    from pathlib import Path
    from core.config import env, rename_env_keys
    saved = {k: os.environ.pop(k, None) for k in ("SEERR_URL", "OVERSEERR_URL", "SEERR_TOKEN", "OVERSEERR_TOKEN")}
    try:
        os.environ["OVERSEERR_URL"] = "http://seerr:5055"
        assert env("SEERR_URL") == "http://seerr:5055", "the old name still works"
        os.environ["SEERR_URL"] = "http://new:5055"
        assert env("SEERR_URL") == "http://new:5055", "the new name wins"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    path = Path(tempfile.mkdtemp()) / ".env"
    path.write_text("# Seerr\nOVERSEERR_URL=http://192.168.1.20:5055\nOVERSEERR_TOKEN='abc=='\n"
                    "# OVERSEERR_WEBHOOK_SECRET=commented\nSEERR_WEBHOOK_SECRET=x\nOVERSEERR_WEBHOOK_SECRET=y\n")
    os.chmod(path, 0o600)
    renamed = rename_env_keys(path)
    text = path.read_text()
    assert renamed == ["OVERSEERR_URL", "OVERSEERR_TOKEN"]
    assert "SEERR_URL=http://192.168.1.20:5055\n" in text and "SEERR_TOKEN='abc=='\n" in text
    assert "# OVERSEERR_WEBHOOK_SECRET=commented" in text, "comments are left alone"
    assert "OVERSEERR_WEBHOOK_SECRET=y" in text, "not renamed onto a name that's already set"
    assert oct(path.stat().st_mode & 0o777) == "0o600", "the file stays private"
    assert rename_env_keys(path) == [], "nothing to do the second time"
