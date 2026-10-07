# path: tests/test_user_mgmt_never_watched.py
"""Members who have never watched anything go the same way as idle ones.

They are warned, then removed, on the same schedule and with the same
exemptions. Their clock counts from the latest of when Plexbie started tracking
them, when they lost a top-three exemption, and the first daily check that
judged never-watched members at all. That last date is saved once and never
moved, so nobody already on the share when it began gets less than a full
period. A tracked person the server is still shared with, whom Tautulli
doesn't list at all, counts as never watched. Tautulli not answering never
does, and neither does anyone who can't be told apart from an active watcher:
off the share, an invite not accepted yet, a name two Tautulli users share, or
someone whose history Tautulli doesn't keep.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401

from test_exemption import _Harness, _seen, _u

NEVER_WATCHED_SINCE = ("user_mgmt", "never_watched_since")


def _ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


def _never(name, discord_id, tracked_days=400, **kw):
    spec = {"plex_username": name, "discord_id": discord_id,
            "created_at": _ago(tracked_days), "last_watched": None}
    spec.update(kw)
    return spec


#: Three people with watch time, so the standings exist and nobody below is in them.
BOARD = [_u("a", 900), _u("b", 800), _u("c", 700)]
#: How Tautulli lists someone who has an account but has never played anything.
GHOST = {"friendly_name": "ghost", "duration": 0, "plays": 0, "last_seen": None}


class _Shared(_Harness):
    """With a plex.tv sign-in. `share` is who the server is shared with, {account id: name};
    `plex_down` makes plex.tv fail."""

    def __init__(self, *args, share=None, plex_down=False, **kw):
        super().__init__(*args, **kw)
        self.share = share or {}
        self.plex_down = plex_down

    def _make_cog(self):
        module, cog = super()._make_cog()
        cog.services.config.plex_token = "t"
        return module, cog

    def run(self, seed=True):
        import core.plex_account as plex_account
        harness = self

        def shared_accounts(config):
            if harness.plex_down:
                raise ConnectionError("plex.tv didn't answer")
            owner = {"id": 1, "title": "ServerOwner", "username": "ServerOwner", "email": "",
                     "owner": True, "thumb": None}
            return [owner] + [{"id": i, "title": n, "username": n, "email": "", "owner": False, "thumb": None}
                              for i, n in harness.share.items()]

        real = plex_account.shared_accounts
        plex_account.shared_accounts = shared_accounts
        try:
            return super().run(seed)
        finally:
            plex_account.shared_accounts = real


def _kv(db, write=None):
    """The saved first-check date for this database; `write` saves one first."""
    import database.session as session_module
    from database.kv_store import kv_get, kv_set

    async def scenario():
        if session_module.engine is not None:
            await session_module.engine.dispose()
        await session_module.init_database(f"sqlite:///{db}")
        if write is not None:
            await kv_set(*NEVER_WATCHED_SINCE, write.isoformat())
        value = await kv_get(*NEVER_WATCHED_SINCE)
        await session_module.engine.dispose()
        return value

    return asyncio.run(scenario())


def test_never_watched_past_the_period_is_warned_then_removed():
    h = _Harness(users=[_never("ghost", 1)], tautulli_rows=BOARD + [GHOST])
    _kv(h.db, write=_ago(40))
    rows = h.run()
    assert h.warned == ["ghost"], "40 days with no watch at all, but no warning"
    assert h.removed == [], "never removed on the pass that warns"
    assert rows["ghost"]["warning_sent"] == 1
    assert rows["ghost"]["days_inactive"] == 40

    h.run(seed=False)
    assert h.removed == ["ghost"], f"warned and still idle, but not removed: warned={h.warned}"


def test_never_watched_inside_the_period_is_left_alone():
    h = _Harness(users=[_never("ghost", 1)], tautulli_rows=BOARD + [GHOST])
    _kv(h.db, write=_ago(10))
    rows = h.run()
    assert h.warned == [] and h.removed == []
    assert rows["ghost"]["days_inactive"] == 10, "the clock must count from the first check, not stand at 0"


def test_the_clock_starts_when_tracking_started_if_that_is_later():
    h = _Harness(users=[_never("ghost", 1, tracked_days=5)], tautulli_rows=BOARD + [GHOST])
    _kv(h.db, write=_ago(40))
    rows = h.run()
    assert h.warned == [] and h.removed == []
    assert rows["ghost"]["days_inactive"] == 5


def test_never_watched_but_exempt_is_never_warned_or_removed():
    h = _Harness(
        users=[_never("kept", 1, never_remove=True),
               _never("owner", 999),                                  # BOT_OWNER_ID
               _never("dropped", 3, exemption_lost_at=_ago(5))],      # fresh period from losing it
        tautulli_rows=BOARD + [{**GHOST, "friendly_name": n} for n in ("kept", "owner", "dropped")],
    )
    _kv(h.db, write=_ago(40))
    rows = h.run()
    assert h.warned == [] and h.removed == []
    assert rows["dropped"]["days_inactive"] == 5


def test_a_tracked_person_tautulli_does_not_list_is_treated_the_same():
    h = _Shared(users=[_never("unlisted", 1, plex_user_id=501)], tautulli_rows=BOARD, share={501: "unlisted"})
    _kv(h.db, write=_ago(40))
    h.run()
    assert h.warned == ["unlisted"], "shared with, but Tautulli has no row for them: they have never watched"
    h.run(seed=False)
    assert h.removed == ["unlisted"]


def test_an_unlisted_person_inside_the_period_is_left_alone():
    h = _Shared(users=[_never("unlisted", 1, tracked_days=3, plex_user_id=501)], tautulli_rows=BOARD,
                share={501: "unlisted"})
    _kv(h.db, write=_ago(40))
    rows = h.run()
    assert h.warned == [] and h.removed == []
    assert rows["unlisted"]["days_inactive"] == 3


def test_an_unlisted_person_off_the_share_is_skipped():
    """Tautulli drops people who are no longer shared with; their row stays until an admin forgets it."""
    h = _Shared(users=[_never("gone", 1, plex_user_id=502)], tautulli_rows=BOARD, share={})
    _kv(h.db, write=_ago(40))
    for seed in (True, False):
        rows = h.run(seed=seed)
    assert h.warned == [] and h.removed == [], "already off the share, so no warning and nothing to remove"
    assert rows["gone"]["days_inactive"] == 0 and rows["gone"]["warning_sent"] == 0


def test_unlisted_people_are_skipped_without_plex_tv():
    """Without a sign-in, or with plex.tv down, nobody can say they're still on the share."""
    users = [_never("unlisted", 1, plex_user_id=501)]
    for h in (_Harness(users=users, tautulli_rows=BOARD),
              _Shared(users=users, tautulli_rows=BOARD, share={501: "unlisted"}, plex_down=True)):
        _kv(h.db, write=_ago(40))
        rows = h.run()
        h.run(seed=False)
        assert h.warned == [] and h.removed == []
        assert rows["unlisted"]["days_inactive"] == 0


def test_pending_invites_are_skipped_and_hold_back_no_one():
    """Invited but not accepted yet: no Plex account to remove, so never warned, never
    due, and however many there are, a real removal still goes ahead."""
    pending = [_never(f"invited{i}", 10 + i, plex_email=f"invited{i}@example.com") for i in range(6)]
    idle = {"plex_username": "idle", "discord_id": 2, "plex_user_id": 601, "warning_sent": True,
            "created_at": _ago(400), "last_watched": _ago(40)}
    board = [{**BOARD[0], "last_seen": _seen(0)}] + BOARD[1:]
    h = _Shared(users=pending + [idle], tautulli_rows=board + [{**_u("idle", 50, user_id=601), "last_seen": _seen(40)}],
                share={601: "idle"})
    _kv(h.db, write=_ago(40))
    rows = h.run()
    assert h.warned == [], "an invite nobody accepted yet isn't someone who never watched"
    assert h.removed == ["idle"], f"pending invites held back a real removal: alerts={h.alerts}"
    assert "Inactivity removals held back" not in h.alerts
    assert all(rows[f"invited{i}"]["warning_sent"] == 0 for i in range(6))


def test_a_name_two_tautulli_users_share_is_not_never_watched():
    """The name matches neither Tautulli user, but one of them may well be this person, watching."""
    busy = [{**_u("busy", 500, plays=9, user_id=701), "last_seen": _seen(0)},
            {**_u("Busy", 10, user_id=702), "last_seen": _seen(60)}]
    for h in (_Harness(users=[_never("busy", 1)], tautulli_rows=BOARD + busy),
              _Shared(users=[_never("busy", 1)], tautulli_rows=BOARD + busy, share={701: "busy", 702: "Busy"})):
        _kv(h.db, write=_ago(40))
        h.run()
        h.run(seed=False)
        assert h.warned == [] and h.removed == [], "an active watcher was judged as never watched"


def test_no_history_kept_is_not_never_watched():
    """Tautulli's Keep History off: their plays are never recorded, so no history proves nothing."""
    h = _Harness(users=[_never("private", 1)], tautulli_rows=BOARD + [{**GHOST, "friendly_name": "private",
                                                                          "keep_history": 0}])
    _kv(h.db, write=_ago(40))
    rows = h.run()
    h.run(seed=False)
    assert h.warned == [] and h.removed == []
    assert rows["private"]["days_inactive"] == 0


def test_tautulli_down_is_not_never_watched():
    h = _Harness(users=[_never("ghost", 1, warning_sent=True)], tautulli_rows=BOARD + [GHOST], tautulli_status=502)
    _kv(h.db, write=_ago(400))
    rows = h.run()
    assert h.warned == [] and h.removed == [], "an unreachable Tautulli must not remove anyone"
    assert rows["ghost"]["days_inactive"] == 0


def test_the_first_check_date_is_saved_once_and_never_moved():
    h = _Harness(users=[_never("ghost", 1)], tautulli_rows=BOARD + [GHOST])
    rows = h.run()
    assert h.warned == [], "tracked for 400 days, but never-watched members were never judged before today"
    assert rows["ghost"]["days_inactive"] == 0
    first = _kv(h.db)
    assert first, "the first check's date wasn't saved"
    assert abs(datetime.fromisoformat(first) - datetime.now(timezone.utc)) < timedelta(minutes=5)

    earlier = _ago(12)
    _kv(h.db, write=earlier)
    rows = h.run(seed=False)
    assert _kv(h.db) == earlier.isoformat(), "a later pass moved the saved date"
    assert rows["ghost"]["days_inactive"] == 12


def test_the_website_counts_down_to_removal():
    from helpers import FakeServices
    from core.config import Config
    from portal.admin import Admin
    from portal.data import Data
    import database.session as session_module

    class Shares(Admin):
        async def _shared(self):
            return {"ghost", "unlisted"}

        async def _owner(self):
            return "ServerOwner"

    h = _Shared(users=[_never("ghost", 1), _never("unlisted", 2, plex_user_id=501)], tautulli_rows=BOARD + [GHOST],
                share={501: "unlisted"})
    _kv(h.db, write=_ago(10))
    h.run()

    async def scenario():
        if session_module.engine is not None:
            await session_module.engine.dispose()
        await session_module.init_database(f"sqlite:///{h.db}")
        out = await Shares(Data(FakeServices(Config()))).people()
        await session_module.engine.dispose()
        return out

    rows = {p["plexName"]: p for p in asyncio.run(scenario())}
    for name in ("ghost", "unlisted"):
        assert rows[name]["daysIdle"] == 10, rows[name]
        assert rows[name]["removalIn"] == 20, f"{name}: removalIn stuck at {rows[name]['removalIn']}"
