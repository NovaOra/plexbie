# path: tests/test_logging_rotation.py
"""Log file rotation and handler setup.

Regression coverage for: logging.FileHandler grows one file without limit. On the
live deployment it reached 138 MB, and that same log contained three
"No space left on device" errors where a Plex webhook was dropped because the
disk had filled.
"""
import json
import logging
import logging.handlers
import os
import tempfile
from pathlib import Path

import conftest  # noqa: F401

from core.logging import (
    DEFAULT_BACKUP_COUNT, DEFAULT_MAX_BYTES, JSONFormatter, _HANDLER_TAG, _env_int,
    setup_logging,
)


class _Chdir:
    """setup_logging() writes to ./logs, so run these in a temp directory."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp()

    def __enter__(self):
        self.previous = os.getcwd()
        os.chdir(self.tmp)
        return Path(self.tmp)

    def __exit__(self, *exc):
        os.chdir(self.previous)
        _reset_root_logger()


def _reset_root_logger():
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def _silence_console():
    """Drop the stdout handler so these tests do not spam the runner output."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(
            handler, logging.FileHandler
        ):
            root.removeHandler(handler)


def _file_handlers():
    """The file handlers *this project* installed, not every one on the root logger.

    Filtering on isinstance(FileHandler) alone was wrong: under pytest the logging
    plugin installs its own _pytest.logging._FileHandler pointing at /dev/null,
    which is a FileHandler and is not rotating - so the suite passed under
    run_all.py and failed under pytest, and the README claimed both worked.

    _HANDLER_TAG is the marker setup_logging already uses to recognise its own
    handlers so repeated calls replace rather than stack them.
    """
    return [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.FileHandler) and getattr(h, _HANDLER_TAG, False)
    ]


# --- the handler must rotate ---

def test_file_handler_is_rotating():
    with _Chdir():
        setup_logging("INFO")
        handlers = _file_handlers()
        assert handlers, "no file handler installed"
        assert all(
            isinstance(h, logging.handlers.RotatingFileHandler) for h in handlers
        ), "plain FileHandler grows without limit"


def test_rotation_limits_are_applied():
    with _Chdir():
        setup_logging("INFO")
        handler = _file_handlers()[0]
        assert handler.maxBytes == DEFAULT_MAX_BYTES
        assert handler.backupCount == DEFAULT_BACKUP_COUNT


def test_rotation_is_configurable():
    os.environ["LOG_MAX_BYTES"] = "4096"
    os.environ["LOG_BACKUP_COUNT"] = "2"
    try:
        with _Chdir():
            setup_logging("INFO")
            handler = _file_handlers()[0]
            assert handler.maxBytes == 4096
            assert handler.backupCount == 2
    finally:
        del os.environ["LOG_MAX_BYTES"]
        del os.environ["LOG_BACKUP_COUNT"]


def test_it_actually_rotates_and_caps_disk_use():
    """The real behaviour: write past the limit and count what survives."""
    os.environ["LOG_MAX_BYTES"] = "2048"
    os.environ["LOG_BACKUP_COUNT"] = "2"
    try:
        with _Chdir() as root:
            setup_logging("INFO")
            _silence_console()
            logger = logging.getLogger("rotation-test")
            for i in range(400):
                logger.info(f"padding line {i} " + "x" * 120)

            logs = sorted(p.name for p in (root / "logs").glob("plexbie.log*"))
            # Active file plus at most backupCount archives.
            assert len(logs) <= 1 + 2, logs
            assert "plexbie.log" in logs
            assert (root / "logs" / "plexbie.log").stat().st_size < 2048 * 3

            total = sum(p.stat().st_size for p in (root / "logs").glob("plexbie.log*"))
            assert total < 2048 * 4, f"disk use not capped: {total} bytes"
    finally:
        del os.environ["LOG_MAX_BYTES"]
        del os.environ["LOG_BACKUP_COUNT"]


def test_rotated_content_is_still_valid_json_lines():
    """Rotation must not corrupt the structured format."""
    os.environ["LOG_MAX_BYTES"] = "2048"
    try:
        with _Chdir() as root:
            setup_logging("INFO")
            _silence_console()
            logging.getLogger("json-test").info("hello rotation")
            for handler in _file_handlers():
                handler.flush()
            lines = (root / "logs" / "plexbie.log").read_text().splitlines()
            assert lines
            parsed = json.loads(lines[-1])
            assert parsed["message"]
            assert parsed["level"] == "INFO"
    finally:
        del os.environ["LOG_MAX_BYTES"]


# --- repeated setup must not double-log ---

def test_repeated_setup_does_not_stack_handlers():
    with _Chdir():
        setup_logging("INFO")
        first = len(logging.getLogger().handlers)
        setup_logging("INFO")
        setup_logging("INFO")
        assert len(logging.getLogger().handlers) == first, (
            "each call added another set of handlers, so every line would be "
            "written once per call"
        )


def test_repeated_setup_writes_each_line_once():
    with _Chdir() as root:
        setup_logging("INFO")
        setup_logging("INFO")
        _silence_console()
        logging.getLogger("dup-test").info("unique-marker-xyz")
        for handler in _file_handlers():
            handler.flush()
        content = (root / "logs" / "plexbie.log").read_text()
        assert content.count("unique-marker-xyz") == 1, content


# --- log level now honours the environment ---

def test_log_level_defaults_to_env():
    """bot.py calls setup_logging() with no argument, so $LOG_LEVEL must apply."""
    os.environ["LOG_LEVEL"] = "WARNING"
    try:
        with _Chdir():
            setup_logging()
            assert logging.getLogger().level == logging.WARNING
    finally:
        os.environ["LOG_LEVEL"] = "INFO"


def test_explicit_level_beats_env():
    os.environ["LOG_LEVEL"] = "WARNING"
    try:
        with _Chdir():
            setup_logging("DEBUG")
            assert logging.getLogger().level == logging.DEBUG
    finally:
        os.environ["LOG_LEVEL"] = "INFO"


def test_unknown_level_falls_back_to_info():
    with _Chdir():
        setup_logging("NONSENSE")
        assert logging.getLogger().level == logging.INFO


# --- the env parser must never stop logging from starting ---

def test_env_int_accepts_valid():
    os.environ["X_TEST_INT"] = "1234"
    try:
        assert _env_int("X_TEST_INT", 99) == 1234
    finally:
        del os.environ["X_TEST_INT"]


def test_env_int_rejects_garbage_and_non_positive():
    for bad in ("not-a-number", "0", "-5", ""):
        os.environ["X_TEST_INT"] = bad
        try:
            assert _env_int("X_TEST_INT", 99) == 99, bad
        finally:
            del os.environ["X_TEST_INT"]


def test_env_int_absent_uses_default():
    os.environ.pop("X_TEST_INT_ABSENT", None)
    assert _env_int("X_TEST_INT_ABSENT", 77) == 77


def test_secrets_are_still_redacted_after_the_change():
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname="p", lineno=1,
        msg="token=abcdef1234567890abcdef", args=(), exc_info=None,
    )
    assert "abcdef1234567890abcdef" not in JSONFormatter().format(record)
