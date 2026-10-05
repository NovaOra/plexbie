"""Email and input validation utilities"""
import re


def validate_email(email: str) -> bool:
    """
    Validate email address format.

    Args:
        email: Email address to validate

    Returns:
        True if email format is valid, False otherwise
    """
    if not email or not isinstance(email, str):
        return False

    # Basic email regex pattern
    # Matches: user@domain.tld
    # Allows: letters, numbers, dots, hyphens, underscores, plus signs
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'

    # Check if email matches pattern
    if not re.match(pattern, email):
        return False

    # Additional checks
    # Email shouldn't be longer than 254 characters (RFC 5321)
    if len(email) > 254:
        return False

    # Split into local and domain parts
    parts = email.split('@')
    if len(parts) != 2:
        return False

    local, domain = parts

    # Local part (before @) shouldn't be longer than 64 characters
    if len(local) > 64:
        return False

    # Domain part shouldn't be longer than 253 characters
    if len(domain) > 253:
        return False

    # Local part shouldn't start or end with a dot
    if local.startswith('.') or local.endswith('.'):
        return False

    # No consecutive dots in local part
    if '..' in local:
        return False

    return True
