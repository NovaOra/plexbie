# path: core/plex_account.py
"""Signing in to plex.tv as the server's owner.

Inviting people, removing them and reading who the server is shared with all
happen on plex.tv, as the owner. The owner's Plex token does that: the one
"Sign in with Plex" on the setup page saves as PLEX_TOKEN. A username and
password (PLEX_USERNAME / PLEX_PASSWORD) are only a fallback for installs
whose PLEX_TOKEN can't (a server-only token, say).
"""
from core.logging import get_logger

logger = get_logger(__name__)


def can_sign_in(config) -> bool:
    return bool(getattr(config, "plex_token", None)
                or (getattr(config, "plex_username", None) and getattr(config, "plex_password", None)))


def owner_account(config):
    """Blocking: the owner's MyPlexAccount. Raises LookupError when nothing can sign in."""
    from plexapi.myplex import MyPlexAccount
    token = getattr(config, "plex_token", None)
    if token:
        try:
            return MyPlexAccount(token=token)
        except Exception as e:
            if not (config.plex_username and config.plex_password):
                raise
            logger.info(f"PLEX_TOKEN couldn't sign in to plex.tv ({e}); trying PLEX_USERNAME/PLEX_PASSWORD")
    if getattr(config, "plex_username", None) and getattr(config, "plex_password", None):
        return MyPlexAccount(config.plex_username, config.plex_password)
    raise LookupError("No way to sign in to plex.tv: set PLEX_TOKEN (Sign in with Plex) or PLEX_USERNAME/PLEX_PASSWORD")


def shared_accounts(config):
    """Blocking: everyone the server is shared with on plex.tv, plus the owner (who
    isn't in their own share list), as {id, title, username, email, owner, thumb}.
    thumb: their plex.tv picture, whose address changes when they change it."""
    account = owner_account(config)
    out = [{"id": account.id, "title": account.username, "username": account.username,
            "email": (account.email or "").lower(), "owner": True, "thumb": getattr(account, "thumb", None)}]
    for u in account.users():
        if u.title:
            out.append({"id": u.id, "title": u.title, "username": u.username or "",
                        "email": (u.email or "").lower(), "owner": False, "thumb": getattr(u, "thumb", None)})
    return out
