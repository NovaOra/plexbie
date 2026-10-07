# path: tests/test_user_removal.py
"""Taking someone off Plex: who is removed, and what happens when they already left.

Two ways this went wrong:

- `/remove-user` (and Remove on the website) asked systemAccounts() whether the
  person was on Plex. That lists every account the server has ever seen, so
  someone already taken off the share in Plex was "found", removeFriend then
  failed, and the admin was told about an error while the row stayed forever.
- The daily inactivity removal named the person by the email or name stored on
  their row. Those can be stale, and a stale value can belong to another friend
  by now: the account removed has to be the one whose inactivity was measured.
"""
import asyncio
from contextlib import asynccontextmanager

import conftest  # noqa: F401

from plexapi.exceptions import NotFound


class _Friend:
    """What account.users() hands back: a plexapi MyPlexUser, reduced to what's read."""

    def __init__(self, account_id, title, email=""):
        self.id = account_id
        self.title = title
        self.username = title
        self.email = email


class _Account:
    """The owner's MyPlexAccount. removeFriend behaves like plexapi's: a string is
    looked up among the friends by name or email, and NotFound if nobody matches."""

    def __init__(self, friends):
        self.friends = list(friends)
        self.removed = []

    def users(self):
        return list(self.friends)

    def removeFriend(self, user):
        if isinstance(user, str):
            match = next((f for f in self.friends
                          if user.lower() in (f.title.lower(), f.username.lower(), f.email.lower())), None)
            if match is None:
                raise NotFound(f"Unable to find user {user}")
            user = match
        self.friends.remove(user)
        self.removed.append(user.id)


class _SystemAccount:
    def __init__(self, account_id, name):
        self.id = account_id
        self.name = name


class _Config:
    plex_token = "owner-token"
    plex_username = None
    plex_password = None
    bot_owner_id = 999
    inactivity_removal_days = 30


def _cog(module, account, system_accounts, calls):
    class Server:
        def systemAccounts(self):
            return list(system_accounts)

    class Services:
        config = _Config()
        plex_server = Server()

    cog = object.__new__(module.UserMgmtCog)
    cog.services = Services()
    cog.bot = None

    async def stats(name):
        return {}

    async def remove_role(discord_id):
        calls.append(("role", discord_id))

    async def manual_dm(user, stats, removed_by):
        calls.append(("removed DM", user.plex_username))

    async def farewell(user, stats):
        calls.append(("farewell", user.plex_username))

    cog._get_user_stats = stats
    cog._remove_plex_role = remove_role
    cog._send_manual_removal_dm = manual_dm
    cog._send_farewell_dm = farewell
    return cog


def _remove_by_hand(person, account, system_accounts):
    """Run remove_plex_user against fakes; returns (ok, message, calls)."""
    from core import plex_invites
    from plugins.user_mgmt import cog as module

    calls = []

    class Session:
        async def execute(self, stmt):
            calls.append("delete row")

        async def commit(self):
            pass

    @asynccontextmanager
    async def get_session():
        yield Session()

    async def person_by_name(session, name):
        return person if name == person.plex_username else None

    cog = _cog(module, account, system_accounts, calls)
    saved = module.get_session, module.person_by_name, module.owner_account, plex_invites.cancel
    module.get_session, module.person_by_name = get_session, person_by_name
    module.owner_account = lambda config: account
    plex_invites.cancel = lambda config, email: calls.append(("cancel invite", email)) and False
    try:
        ok, message = asyncio.run(cog.remove_plex_user(person.plex_username, "Omar"))
    finally:
        module.get_session, module.person_by_name, module.owner_account, plex_invites.cancel = saved
    return ok, message, calls


def _person(**kw):
    from plugins.user_mgmt.models import PlexUser
    spec = {"id": 7, "plex_username": "departed", "plex_email": "departed@example.com", "discord_id": 42}
    spec.update(kw)
    return PlexUser(**spec)


# ===================================================================
# removing by hand someone who already left Plex
# ===================================================================

def test_someone_taken_off_the_share_in_plex_can_still_be_forgotten():
    """Still in systemAccounts (it keeps everyone), no longer shared with."""
    account = _Account([_Friend(8, "current", "current@example.com")])
    system = [_SystemAccount(1, "Owner"), _SystemAccount(7, "departed"), _SystemAccount(8, "current")]
    ok, message, calls = _remove_by_hand(_person(), account, system)
    assert ok, f"the admin was told this failed: {message}"
    assert "delete row" in calls, "the tracking row stayed"
    assert ("role", 42) in calls, "their Plex role in Discord stayed"
    assert account.removed == [], "nobody else may be taken off the share"
    assert not any(c[0] == "removed DM" for c in calls if isinstance(c, tuple)), (
        "they weren't removed by anyone here, so no 'you've been removed' message"
    )
    assert "already left" in message


def test_plex_saying_not_found_means_already_gone():
    """Off the share between the check and the removal: plexapi raises NotFound."""
    friend = _Friend(7, "departed", "departed@example.com")
    account = _Account([friend])

    def gone(user):
        account.friends.remove(friend)
        raise NotFound("Unable to find user departed")
    account.removeFriend = gone
    system = [_SystemAccount(1, "Owner"), _SystemAccount(7, "departed")]
    ok, message, calls = _remove_by_hand(_person(), account, system)
    assert ok, f"the admin was told this failed: {message}"
    assert "delete row" in calls and ("role", 42) in calls
    assert not any(c[0] == "removed DM" for c in calls if isinstance(c, tuple))


def test_not_found_while_still_on_the_share_is_an_error_and_keeps_them():
    account = _Account([_Friend(7, "departed", "departed@example.com")])

    def refused(user):
        raise NotFound("Unable to find user")
    account.removeFriend = refused
    ok, message, calls = _remove_by_hand(_person(), account, [_SystemAccount(1, "Owner")])
    assert not ok, "they still have Plex access"
    assert "delete row" not in calls and ("role", 42) not in calls


def test_a_stale_stored_email_still_removes_the_person_on_the_share():
    """The email on the row is old; the Plex account is found on the share by name."""
    account = _Account([_Friend(7, "departed", "departed@new.example.com")])
    person = _person(plex_email="departed@old.example.com")
    ok, message, calls = _remove_by_hand(person, account, [_SystemAccount(1, "Owner")])
    assert ok, message
    assert account.removed == [7], "they kept their Plex access"
    assert ("removed DM", "departed") in calls and "delete row" in calls


def test_a_plex_rename_is_still_found_by_account_id():
    """Renamed on Plex since the last daily check: the row still has the old name."""
    account = _Account([_Friend(7, "departed.renamed", "departed@example.com")])
    person = _person(plex_user_id=7)
    ok, message, calls = _remove_by_hand(person, account, [_SystemAccount(1, "Owner")])
    assert ok, message
    assert account.removed == [7], "forgotten while still on the share"
    assert ("removed DM", "departed") in calls


def test_a_known_account_that_left_is_not_matched_by_its_old_name():
    """Account 7 left; another account now carries the name."""
    account = _Account([_Friend(8, "departed")])
    person = _person(plex_user_id=7)
    ok, message, calls = _remove_by_hand(person, account, [_SystemAccount(1, "Owner")])
    assert ok, message
    assert account.removed == [], "someone else was removed under a reused name"
    assert "delete row" in calls


def test_the_server_owner_is_never_removed_or_forgotten():
    for person in (_person(plex_username="Owner", discord_id=5), _person(discord_id=999)):
        account = _Account([_Friend(8, "current")])
        ok, message, calls = _remove_by_hand(person, account, [_SystemAccount(1, "Owner")])
        assert not ok and "owner" in message
        assert calls == [] and account.removed == []


def test_someone_still_shared_with_is_removed_and_told():
    account = _Account([_Friend(7, "departed", "departed@example.com")])
    system = [_SystemAccount(1, "Owner"), _SystemAccount(7, "departed")]
    ok, message, calls = _remove_by_hand(_person(), account, system)
    assert ok, message
    assert account.removed == [7]
    assert ("removed DM", "departed") in calls and "delete row" in calls


# ===================================================================
# the daily inactivity removal takes off the account it measured
# ===================================================================

def _remove_inactive(user, plex_user_id, account):
    from plugins.user_mgmt import cog as module

    calls = []
    cog = _cog(module, account, [], calls)
    saved = module.owner_account
    module.owner_account = lambda config: account
    try:
        return asyncio.run(cog._remove_inactive_user(user, plex_user_id)), calls
    finally:
        module.owner_account = saved


def test_inactivity_removal_takes_off_the_measured_account_not_a_stale_email():
    """The stored email now belongs to another friend; the measured account is 55."""
    idle = _Friend(55, "river", "river@new.example.com")
    other = _Friend(66, "sam", "river@old.example.com")
    account = _Account([idle, other])
    user = _person(plex_username="river", plex_email="river@old.example.com", plex_user_id=55)
    removed, calls = _remove_inactive(user, 55, account)
    assert account.removed == [55], f"removed the wrong account: {account.removed}"
    assert removed is True
    assert ("farewell", "river") in calls


def test_the_stored_account_id_is_used_when_tautulli_has_none():
    account = _Account([_Friend(55, "river"), _Friend(66, "river.old")])
    user = _person(plex_username="river.old", plex_email=None, plex_user_id=55)
    removed, _ = _remove_inactive(user, 0, account)
    assert account.removed == [55] and removed is True


def test_an_account_no_longer_shared_with_is_never_matched_by_name():
    """Account 55 already left; its old name is now someone else's."""
    account = _Account([_Friend(66, "river")])
    user = _person(plex_username="river", plex_email=None, plex_user_id=55)
    removed, calls = _remove_inactive(user, 55, account)
    assert account.removed == [], "someone else was removed under a reused name"
    assert removed is False, "the row is kept, so the next pass looks again"
    assert calls == [], "no farewell for a removal that didn't happen"


def test_a_tautulli_id_that_disagrees_with_the_stored_one_removes_nobody():
    account = _Account([_Friend(55, "river"), _Friend(66, "river2")])
    user = _person(plex_username="river", plex_email=None, plex_user_id=55)
    removed, calls = _remove_inactive(user, 66, account)
    assert account.removed == [] and removed is False and calls == []


def test_without_any_account_id_the_stored_email_is_still_used():
    account = _Account([_Friend(55, "river", "river@example.com")])
    user = _person(plex_username="river", plex_email="river@example.com", plex_user_id=None)
    removed, _ = _remove_inactive(user, 0, account)
    assert account.removed == [55] and removed is True
