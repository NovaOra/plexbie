# path: tests/test_plex_access.py
""""Does this person still have access?" is not what systemAccounts() answers.

systemAccounts() lists every account the server has ever seen. A user removed from
the share yesterday is still in it today - verified on the live server, where a
user removed via plex.tv on 2026-09-29 still appeared the next day. Orphan
detection built on it therefore never fires for the one case it exists for.

Current access lives on plex.tv, which is also where removeFriend acts. The owner
does not appear in their own share list, so they have to be added back.
"""
import ast
import asyncio
import pathlib

import conftest  # noqa: F401

ROOT = pathlib.Path(conftest.PROJECT_ROOT)


class _Account:
    def __init__(self, account_id, name):
        self.id = account_id
        self.name = name


def _cog(*, shares=None, shares_raise=False, creds=True, accounts=None):
    from plugins.user_mgmt import cog as module

    class FakeConfig:
        plex_token = "owner-token" if creds else None
        plex_username = None
        plex_password = None
        bot_owner_id = 999

    class FakePlex:
        def systemAccounts(self):
            return accounts if accounts is not None else [
                _Account(0, ""), _Account(1, "Owner"),
                _Account(7, "departed"), _Account(8, "current"),
            ]

    class FakeServices:
        config = FakeConfig()
        plex_server = FakePlex()

    cog = object.__new__(module.UserMgmtCog)
    cog.services = FakeServices()
    cog.bot = None

    def fake_fetch(config):
        if shares_raise:
            raise RuntimeError("plex.tv unreachable")
        return set(shares if shares is not None else {"current"})

    original = module._fetch_shared_usernames
    module._fetch_shared_usernames = fake_fetch
    return module, cog, original


def _access(**kwargs):
    from plugins.user_mgmt import cog as module

    mod, cog, original = _cog(**kwargs)
    try:
        return asyncio.run(cog._plex_users_with_access())
    finally:
        mod._fetch_shared_usernames = original


# ===================================================================
# who has access
# ===================================================================

def test_access_comes_from_the_share_list_not_system_accounts():
    """'departed' is in systemAccounts but not shared - it must not count."""
    access = _access()
    assert "current" in access
    assert "departed" not in access, (
        "a removed user is still in systemAccounts; access must come from plex.tv"
    )


def test_the_owner_is_included_even_though_they_are_not_shared_with_themselves():
    access = _access()
    assert "Owner" in access, (
        "the owner never appears in their own share list, so they would look like "
        "an orphan on every listing"
    )


def test_missing_credentials_returns_none_not_an_empty_set():
    """An empty set would mean 'nobody has access' and flag every tracked user."""
    assert _access(creds=False) is None


def test_an_unreachable_plex_tv_returns_none():
    assert _access(shares_raise=True) is None


def test_no_owner_account_is_tolerated():
    """systemAccounts without an id 1 must not crash the lookup."""
    access = _access(accounts=[_Account(5, "someone")])
    assert access == {"current"}


def test_blank_share_titles_are_dropped():
    """Exercises the real helper: the previous version asserted against the fake
    that replaced it, so it was testing nothing.
    """
    import plexapi.myplex
    from plugins.user_mgmt.cog import _fetch_shared_usernames

    class FakeUser:
        def __init__(self, title):
            self.title = title

    class FakeAccount:
        def __init__(self, *args, **kwargs):
            pass

        def users(self):
            return [FakeUser("real"), FakeUser(""), FakeUser(None)]

    original = plexapi.myplex.MyPlexAccount
    plexapi.myplex.MyPlexAccount = FakeAccount
    try:
        class Cfg:
            plex_token, plex_username, plex_password = "t", None, None
        names = _fetch_shared_usernames(Cfg())
    finally:
        plexapi.myplex.MyPlexAccount = original

    assert names == {"real"}, f"expected only real names, got {names!r}"


# ===================================================================
# orphan detection
# ===================================================================

def _orphan_flags(tracked, **kwargs):
    """Run the decision the listing makes, for the given tracked usernames."""
    from plugins.user_mgmt import cog as module

    mod, cog, original = _cog(**kwargs)
    try:
        with_access = asyncio.run(cog._plex_users_with_access())
    finally:
        mod._fetch_shared_usernames = original

    can_check = with_access is not None
    return {
        name: (can_check and name not in with_access) for name in tracked
    }, can_check


def test_a_removed_user_is_flagged_as_an_orphan():
    """The case that could never fire before: removed from plex.tv, still in
    systemAccounts, tracking row left behind.
    """
    flags, can_check = _orphan_flags(["departed", "current"])
    assert can_check
    assert flags["departed"] is True, (
        "a user whose access was removed must be flagged"
    )
    assert flags["current"] is False


def test_the_owner_is_never_flagged():
    flags, _ = _orphan_flags(["Owner"])
    assert flags["Owner"] is False


def test_nothing_is_flagged_when_access_cannot_be_determined():
    """Declining to judge beats mislabelling everyone as an orphan."""
    flags, can_check = _orphan_flags(["anyone", "else"], creds=False)
    assert can_check is False
    assert not any(flags.values())


# ===================================================================
# pattern: systemAccounts must not be used to mean "has access"
# ===================================================================

def _code(path):
    import io
    import tokenize

    pieces = []
    with open(path, "rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type != tokenize.COMMENT:
                pieces.append(token.string)
    return "\n".join(pieces)


def test_the_listing_no_longer_derives_access_from_system_accounts():
    # ast never sees comments, so the prose explaining why systemAccounts is
    # wrong cannot be mistaken for a use of it - which is how an equivalent test
    # in this project failed earlier.
    text = (ROOT / "plugins" / "user_mgmt" / "cog.py").read_text()
    tree = ast.parse(text)
    # the only systemAccounts callers left should be the owner lookup and the
    # account-hygiene listing, neither of which claims to know about access
    functions_using_it = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and inner.attr == "systemAccounts":
                functions_using_it.add(node.name)

    assert "list_tracked_users" not in functions_using_it, (
        "the orphan listing is deriving access from systemAccounts again"
    )
    assert "_plex_owner_name" in functions_using_it, (
        "expected the owner lookup to be the deliberate remaining use"
    )


def test_the_helper_documents_why_system_accounts_is_wrong():
    """The next person will reach for systemAccounts; the reason must be at hand."""
    from plugins.user_mgmt.cog import _fetch_shared_usernames

    doc = _fetch_shared_usernames.__doc__ or ""
    assert "systemAccounts" in doc
    assert "removed" in doc.lower()


def test_the_token_from_sign_in_is_enough_for_plex_tv():
    """Setup's "Sign in with Plex" saves the owner's token; no password is needed to invite."""
    import plexapi.myplex
    from core.plex_account import can_sign_in, owner_account
    seen = []

    class FakeAccount:
        def __init__(self, *args, **kwargs):
            seen.append((args, kwargs))

    class TokenOnly:
        plex_token, plex_username, plex_password = "owner-token", None, None

    class Nothing:
        plex_token, plex_username, plex_password = None, None, None

    original = plexapi.myplex.MyPlexAccount
    plexapi.myplex.MyPlexAccount = FakeAccount
    try:
        assert can_sign_in(TokenOnly()) and not can_sign_in(Nothing())
        owner_account(TokenOnly())
        try:
            owner_account(Nothing())
            raise AssertionError("expected LookupError")
        except LookupError:
            pass
    finally:
        plexapi.myplex.MyPlexAccount = original
    assert seen == [((), {"token": "owner-token"})]


def test_removing_someone_who_never_joined_plex_forgets_them_and_takes_back_the_invite():
    """nomail got the invite email but never accepted: they aren't one of Plex's
    accounts, so removal used to stop at "not found on server" and leave them listed."""
    import asyncio
    from contextlib import asynccontextmanager
    from core import plex_invites
    from plugins.user_mgmt import cog as module
    from plugins.user_mgmt.models import PlexUser

    person = PlexUser(id=7, plex_username="nomail", plex_email="nomail@example.com", discord_id=42)
    calls = []

    class Session:
        async def execute(self, stmt):
            calls.append("delete")

        async def commit(self):
            calls.append("commit")

    @asynccontextmanager
    async def get_session():
        yield Session()

    async def person_by_name(session, name):
        return person if name == "nomail" else None

    class Server:
        def systemAccounts(self):
            return []                      # not on the server

    cog = object.__new__(module.UserMgmtCog)
    cog.services = type("S", (), {"plex_server": Server(), "config": type("C", (), {"bot_owner_id": None})()})()

    async def remove_role(discord_id):
        calls.append(("role", discord_id))

    async def stats(name):
        raise AssertionError("no departure stats for someone who never had access")
    cog._remove_plex_role, cog._get_user_stats = remove_role, stats
    saved = module.get_session, module.person_by_name, plex_invites.cancel
    module.get_session, module.person_by_name = get_session, person_by_name
    plex_invites.cancel = lambda config, email: calls.append(("cancel", email)) or True
    try:
        ok, message = asyncio.run(cog.remove_plex_user("nomail", "Omar"))
    finally:
        module.get_session, module.person_by_name, plex_invites.cancel = saved
    assert ok, message
    assert ("cancel", "nomail@example.com") in calls and ("role", 42) in calls and "delete" in calls
    assert "forgot them and took back their invite" in message
