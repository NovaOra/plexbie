# path: core/plex_invites.py
"""Plex invites that haven't been accepted yet, on plex.tv.

An invite sent to the wrong address otherwise sits there for good: Plex shows
it nowhere in Plexbie, and the right person never gets one. These list the
owner's unaccepted invites, cancel one, or send it to a corrected address
(cancel the old, invite the new). All blocking: call through run_blocking.
"""
from typing import Any, Dict, List

from core.plex_account import owner_account


def _when(value) -> str:
    try:
        return value.isoformat()
    except AttributeError:
        return ""


def list_pending(config) -> List[Dict[str, Any]]:
    """Invites the owner sent that nobody has accepted, newest first."""
    out = []
    for invite in owner_account(config).pendingInvites(includeSent=True, includeReceived=False):
        email = (getattr(invite, "email", "") or "").strip()
        out.append({"email": email, "name": getattr(invite, "username", "") or getattr(invite, "friendlyName", "") or "",
                    "sentAt": _when(getattr(invite, "createdAt", None))})
    return sorted(out, key=lambda i: i["sentAt"], reverse=True)


def _find(account, email: str):
    for invite in account.pendingInvites(includeSent=True, includeReceived=False):
        if (getattr(invite, "email", "") or "").strip().lower() == email.strip().lower():
            return invite
    return None


def _cancel(account, invite) -> None:
    """Take an invite back. One to an address with no Plex account yet has no id
    (plexapi would send "nan" and get a 404); Plex knows those by the email."""
    from urllib.parse import quote
    from plexapi import utils
    from plexapi.myplex import MyPlexInvite
    invite_id = getattr(invite, "id", None)
    if isinstance(invite_id, int):
        account.cancelInvite(invite)
        return
    params = {"friend": int(bool(invite.friend)), "home": int(bool(invite.home)), "server": int(bool(invite.server))}
    account.query(f"{MyPlexInvite.REQUESTED}/{quote(invite.email, safe='')}" + utils.joinArgs(params),
                  account._session.delete)


def cancel(config, email: str) -> bool:
    """Take back the invite to `email`. False if there isn't one."""
    account = owner_account(config)
    invite = _find(account, email)
    if invite is None:
        return False
    _cancel(account, invite)
    return True


def resend(config, server, old: str, new: str) -> None:
    """Send the invite to `new` instead of `old` (cancelling the old one if it's still there)."""
    account = owner_account(config)
    invite = _find(account, old)
    if invite is not None:
        _cancel(account, invite)
    account.inviteFriend(user=new, server=server, sections=server.library.sections(), allowSync=False)
