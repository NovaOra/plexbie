# path: core/security.py
"""Security utilities for token handling and redaction"""
import hmac
import re
from typing import Any, Dict, Optional


# Patterns for sensitive data
SENSITIVE_PATTERNS = [
    (re.compile(r"(token|key|secret|password|auth)[\"\']?\s*[:=]\s*[\"\']?([^\s\"\']+)", re.IGNORECASE), r"\1=REDACTED"),
    (re.compile(r"[a-zA-Z0-9+/]{32,}={0,2}"), lambda m: m.group()[:3] + "..." + m.group()[-3:]),  # Base64 tokens
    (re.compile(r"X-Plex-Token=([^&\s]+)"), r"X-Plex-Token=REDACTED"),
    # Email addresses: enough left to tell two apart ("a***@gmail.com"), not to contact anyone.
    (re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b"), r"\1***@\2"),
]


def redact(text: str) -> str:
    """Redact sensitive information from text"""
    if not text:
        return text
    
    result = text
    for pattern, replacement in SENSITIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    
    return result


SENSITIVE_KEYS = {"token", "key", "secret", "password", "auth", "api_key", "apikey"}


def _redact_value(value: Any) -> Any:
    """Redact a value of any shape, recursing through containers.

    Lists were previously returned untouched, so a payload like
    ``{"users": [{"token": "abc"}]}`` - the shape every *arr and Seerr webhook
    uses - logged its secrets in full.
    """
    if isinstance(value, dict):
        return redact_dict(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    if isinstance(value, str):
        return redact(value)
    return value


def redact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively redact sensitive data from a dictionary.

    Keys whose name looks sensitive are replaced wholesale; everything else is
    walked, including lists and tuples of nested dictionaries.
    """
    if not isinstance(data, dict):
        return data

    result = {}
    for key, value in data.items():
        if any(s in str(key).lower() for s in SENSITIVE_KEYS):
            result[key] = "REDACTED"
        else:
            result[key] = _redact_value(value)

    return result


def secure_equals(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time comparison of two secrets supplied as text.

    hmac.compare_digest refuses str arguments containing non-ASCII characters
    (it raises TypeError), so a request carrying a single non-ASCII byte in an
    auth header would otherwise crash the handler and return 500 instead of 401.
    Compare the UTF-8 bytes instead, which is defined for all input.
    """
    if provided is None or expected is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def plain_server_header(request, response) -> None:
    """aiohttp on_response_prepare hook for the setup page and the webhook listener."""
    response.headers["Server"] = "Plexbie"           # not which Python and aiohttp versions
