# path: core/logging.py
"""Structured logging configuration"""
import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.security import redact_dict


class JSONFormatter(logging.Formatter):
    """JSON log formatter with secret redaction"""
    
    def format(self, record):
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        }
        
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        
        # Redact sensitive data
        log_obj = redact_dict(log_obj)
        
        return json.dumps(log_obj)


def _env_int(name: str, default: int) -> int:
    """Read a positive integer env var, falling back on anything unusable.

    Deliberately standalone rather than reusing core.config._env_int_default:
    core.config imports this module for its logger, so depending on it here would
    be circular. A bad value must never prevent logging from starting.
    """
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"Ignoring invalid {name}={raw!r}, using {default}", file=sys.stderr)
        return default
    if value <= 0:
        print(f"Ignoring non-positive {name}={raw!r}, using {default}", file=sys.stderr)
        return default
    return value


#: Rotate once the active log reaches this size, keeping this many old files.
#: The defaults cap total disk use at roughly 6 x 20 MB = 120 MB.
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

#: Marks the handlers this module installed, so a second call replaces them
#: rather than stacking a duplicate set (which would log every line twice).
_HANDLER_TAG = "_plexbie_handler"


def setup_logging(log_level: str = None, enable_http_debug: bool = False):
    """Configure logging for the application.

    Args:
        log_level: Logging level (INFO, DEBUG, ...). Defaults to $LOG_LEVEL.
        enable_http_debug: If True, log Discord HTTP requests and rate limits.

    The file handler rotates. It previously used logging.FileHandler, which grows
    a single file without limit: on one deployment that reached 138 MB, and the
    log itself contained three "No space left on device" errors where a webhook
    was dropped because the disk had filled.

    Size is used rather than time so that a burst cannot outrun the policy - the
    same deployment produced 26 MB from one repeatedly-reprocessed item.
    """
    # bot.py calls this with no arguments, before Config() exists, so the
    # configured LOG_LEVEL has to be read from the environment here or it is
    # silently ignored.
    if log_level is None:
        log_level = os.getenv("LOG_LEVEL", "INFO")

    max_bytes = _env_int("LOG_MAX_BYTES", DEFAULT_MAX_BYTES)
    backup_count = _env_int("LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT)

    # Create logs directory
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Drop any handlers a previous call installed, so repeated setup is idempotent.
    for existing in [h for h in root_logger.handlers if getattr(h, _HANDLER_TAG, False)]:
        root_logger.removeHandler(existing)
        existing.close()

    # Console handler (JSON to stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(JSONFormatter())
    setattr(console_handler, _HANDLER_TAG, True)
    root_logger.addHandler(console_handler)

    # Rotating file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "plexbie.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(JSONFormatter())
    setattr(file_handler, _HANDLER_TAG, True)
    root_logger.addHandler(file_handler)

    # Reduce noise from libraries
    if enable_http_debug:
        # Enable HTTP request logging to monitor rate limits
        logging.getLogger("discord.http").setLevel(logging.DEBUG)
        logging.getLogger("discord.gateway").setLevel(logging.INFO)
    else:
        logging.getLogger("discord").setLevel(logging.WARNING)

    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("plexapi").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance"""
    return logging.getLogger(name)
