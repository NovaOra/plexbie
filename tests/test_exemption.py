# path: tests/test_exemption.py
"""Top-three watch-time exemption from inactivity removal.

The rule, as specified: while you are in the top three by watch time you are not
on the chop block. The moment you drop out, your timer starts - a full fresh
period, even if you were already long past the removal threshold - and you are
told why.

The dangerous failure mode is not "someone stays too long". It is a Tautulli
hiccup being read as "nobody is in the top three", which would start every user's
clock at once. There is a test for exactly that.
"""
import asyncio
import json
import pathlib
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import conftest  # noqa: F401

from utils.standings import (
    DEFAULT_TOP_WATCHERS,
    load_aliases,
    merge_aliased_users,
    standings,
    top_watchers,
)


def _u(name, duration, plays=1, user_id=None):
    return {"friendly_name": name, "duration": duration, "plays": plays,
            "user_id": user_id}


# ===================================================================
# the standings helper
# ===================================================================

def test_standings_orders_by_time_and_drops_zero_totals():
    rows = [_u("a", 100), _u("b", 300), _u("never", 0)]
    assert standings(rows) == [("b", 300), ("a", 100)]


def test_aliased_rows_are_combined():
    """Two Tautulli names for one person must not split their total."""
    rows = [_u("Sammy", 100), _u("Sam.Rivera", 300)]
    aliases = {"Sammy": "Sam.Rivera"}
    assert standings(rows, aliases) == [("Sam.Rivera", 400)]


def test_watch_party_credits_are_added_and_alias_resolved():
    """Credits are keyed by Plex username, which may differ from the Tautulli name."""
    rows = [_u("Sam.Rivera", 300)]
    aliases = {"Sammy": "Sam.Rivera"}
    credits = {"Sammy": 60}
    assert standings(rows, aliases, credits) == [("Sam.Rivera", 360)]


def test_credits_alone_can_put_someone_on_the_board():
    assert standings([], None, {"solo": 500}) == [("solo", 500)]


def test_ties_break_deterministically():
    """The cut-off for exemption can fall inside a tie."""
    rows = [_u("zeta", 100), _u("alpha", 100)]
    first = top_watchers(rows, limit=1)
    assert first == ["alpha"]
    assert top_watchers(rows, limit=1) == first, "ordering must be stable"


def test_top_watchers_respects_the_limit():
    rows = [_u(n, i * 10) for i, n in enumerate("abcdef", start=1)]
    assert top_watchers(rows, limit=3) == ["f", "e", "d"]


def test_fewer_people_than_the_limit_means_everyone_qualifies():
    """You cannot be outside the top three of two people."""
    rows = [_u("a", 10), _u("b", 20)]
    assert set(top_watchers(rows, limit=3)) == {"a", "b"}


def test_nobody_with_watch_time_means_no_exemptions():
    assert top_watchers([_u("a", 0)]) == []


def test_default_limit_is_three():
    assert DEFAULT_TOP_WATCHERS == 3


def test_load_aliases_tolerates_a_missing_or_corrupt_file():
    tmp = pathlib.Path(tempfile.mkdtemp())
    assert load_aliases(tmp / "absent.json") == {}
    bad = tmp / "bad.json"
    bad.write_text("{ not json")
    assert load_aliases(bad) == {}
    good = tmp / "good.json"
    good.write_text(json.dumps({"aliases": {"x": "y"}}))
    assert load_aliases(good) == {"x": "y"}


def test_merge_without_aliases_is_a_passthrough():
    rows = [_u("a", 1)]
    assert merge_aliased_users(rows, None) == rows


# ===================================================================
# the migration
# ===================================================================

def test_migration_adds_the_columns_to_a_legacy_table():
    """create_all does not alter existing tables - that is how
    watch_party_credits ended up with two spellings of the same field.
    """
    import database.session as session_module

    path = str(pathlib.Path(tempfile.mkdtemp()) / "legacy.db")
    with sqlite3.connect(path) as con:
        con.execute("""
            CREATE TABLE plex_users (
                id INTEGER NOT NULL PRIMARY KEY,
                discord_id INTEGER,
                discord_username VARCHAR(255),
                plex_username VARCHAR(255) NOT NULL,
                plex_email VARCHAR(255),
                plex_user_id INTEGER,
                last_watched DATETIME,
                days_inactive INTEGER NOT NULL DEFAULT 0,
                warning_sent BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                total_watch_time INTEGER,
                total_plays INTEGER
            )""")
        con.execute(
            "INSERT INTO plex_users (id, plex_username, created_at, updated_at) "
            "VALUES (1, 'existing', '2026-01-01', '2026-01-01')")

    async def scenario():
        if session_module.engine is not None:
            await session_module.engine.dispose()
        await session_module.init_database(f"sqlite:///{path}")
        await session_module.engine.dispose()

    asyncio.run(scenario())

    with sqlite3.connect(path) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info('plex_users')")}
        assert "is_top_watcher" in cols, "migration did not add is_top_watcher"
        assert "exemption_lost_at" in cols, "migration did not add exemption_lost_at"
        # the pre-existing row survives, with the default applied
        row = con.execute(
            "SELECT plex_username, is_top_watcher, exemption_lost_at "
            "FROM plex_users WHERE id=1").fetchone()
        assert row == ("existing", 0, None)


def test_migration_is_idempotent():
    import database.session as session_module

    path = str(pathlib.Path(tempfile.mkdtemp()) / "twice.db")

    async def scenario():
        for _ in range(2):
            if session_module.engine is not None:
                await session_module.engine.dispose()
            await session_module.init_database(f"sqlite:///{path}")
        await session_module.engine.dispose()

    asyncio.run(scenario())
    with sqlite3.connect(path) as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info('plex_users')")]
    assert cols.count("is_top_watcher") == 1


# ===================================================================
# the rule, end to end
# ===================================================================

class _Harness:
    """A UserMgmtCog wired to a real temporary database and fake Tautulli."""

    def __init__(self, users, tautulli_rows, aliases=None, credits=None,
                 tautulli_status=200, plex_owner=None, owner_account_id=None,
                 removal_works=True):
        self.users = users
        self.tautulli_rows = tautulli_rows
        self.aliases = aliases or {}
        self.credits = credits or {}
        self.tautulli_status = tautulli_status
        self.plex_owner = plex_owner                # the Plex server owner's account name
        self.owner_account_id = owner_account_id    # the owner's Plex account id, as plex.tv said
        self.warning_reaches = True                 # whether a warning gets through by any route
        self.removal_works = removal_works          # whether Plex agrees to a removal
        self.warned = []
        self.removed = []
        self.exemption_lost = []
        self.alerts = []
        self.alert_texts = []                       # (admin channel text, phone alert text)
        self.db = str(pathlib.Path(tempfile.mkdtemp()) / "exempt.db")

    def _make_cog(self):
        from plugins.user_mgmt import cog as module

        harness = self

        from core.clients import ServiceError

        class FakeTautulli:
            configured = True

            async def users_table(self, **params):
                if harness.tautulli_status != 200:
                    raise ServiceError(f"Tautulli answered HTTP {harness.tautulli_status}", harness.tautulli_status)
                return harness.tautulli_rows

        class FakeConfig:
            tautulli_url = "http://tautulli.local"
            tautulli_token = "t"
            bot_owner_id = 999
            inactivity_warning_days = 25
            inactivity_removal_days = 30

        class FakePlex:
            def systemAccounts(self):
                return [SimpleNamespace(id=0, name=""), SimpleNamespace(id=1, name=harness.plex_owner)]

        class FakeServices:
            config = FakeConfig()
            tautulli = FakeTautulli()
            plex_server = FakePlex() if harness.plex_owner else None

        cog = object.__new__(module.UserMgmtCog)
        cog.services = FakeServices()
        cog.bot = None
        cog._load_watch_aliases = lambda: harness.aliases

        async def warn(user):
            harness.warned.append(user.plex_username)
            return harness.warning_reaches

        async def lost(user):
            harness.exemption_lost.append(user.plex_username)

        async def remove(user, plex_user_id):
            harness.removed.append(user.plex_username)
            return harness.removal_works

        async def alert(title, text, **kw):
            harness.alerts.append(title)
            harness.alert_texts.append((text, kw.get("push")))

        cog._send_warning_dm = warn
        cog._send_exemption_lost_dm = lost
        cog._remove_inactive_user = remove
        cog._alert_admins = alert
        return module, cog

    def run(self, seed=True):
        """One daily pass. seed=False runs another pass over the rows already saved."""
        import database.session as session_module
        from plugins.user_mgmt.models import PlexUser
        from plugins.watch_party.models import WatchPartyCredit

        module, cog = self._make_cog()

        async def scenario():
            if session_module.engine is not None:
                await session_module.engine.dispose()
            await session_module.init_database(f"sqlite:///{self.db}")
            if not seed:
                await module.UserMgmtCog.check_inactive_users.coro(cog)
                await session_module.engine.dispose()
                return
            if self.owner_account_id:
                from database.kv_store import kv_set
                from portal.auth import OWNER_ID
                await kv_set(*OWNER_ID, self.owner_account_id)
            async with session_module.get_session() as session:
                for spec in self.users:
                    session.add(PlexUser(**spec))
                for name, total in self.credits.items():
                    session.add(WatchPartyCredit(
                        discord_id=0, plex_username=name, total_duration=total))
                await session.commit()

            await module.UserMgmtCog.check_inactive_users.coro(cog)
            await session_module.engine.dispose()

        asyncio.run(scenario())
        with sqlite3.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            return {r["plex_username"]: dict(r)
                    for r in con.execute("SELECT * FROM plex_users")}


def _seen(days_ago):
    return int((datetime.now(timezone.utc) - timedelta(days=days_ago)).timestamp())


def _user(name, discord_id, days_idle, **kw):
    spec = {
        "plex_username": name,
        "discord_id": discord_id,
        "created_at": datetime.now(timezone.utc) - timedelta(days=400),
        "last_watched": datetime.now(timezone.utc) - timedelta(days=days_idle),
    }
    spec.update(kw)
    return spec


def test_a_top_three_user_is_never_warned_or_removed():
    """200 days idle, but top of the board."""
    h = _Harness(
        users=[_user("whale", 1, 200)],
        tautulli_rows=[_u("whale", 100_000), _u("b", 50), _u("c", 40), _u("d", 30)],
    )
    rows = h.run()
    assert h.warned == [] and h.removed == []
    assert rows["whale"]["is_top_watcher"] == 1
    assert rows["whale"]["exemption_lost_at"] is None


def test_a_user_outside_the_top_three_is_still_enforced():
    """The exemption must not switch enforcement off for everyone."""
    h = _Harness(
        users=[_user("idler", 1, 40, warning_sent=True)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("idler", 10)],
    )
    h.run()
    assert h.removed == ["idler"], f"expected removal, got warned={h.warned}"


def test_dropping_out_starts_the_clock_and_sends_one_message():
    h = _Harness(
        users=[_user("faller", 1, 200, is_top_watcher=True)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("faller", 10)],
    )
    rows = h.run()
    assert h.exemption_lost == ["faller"], "the user was not told they lost it"
    assert h.removed == [], "must not be removed on the pass it drops out"
    assert h.warned == [], "must not be warned on that pass either"
    assert rows["faller"]["exemption_lost_at"] is not None
    assert rows["faller"]["is_top_watcher"] == 0
    assert rows["faller"]["warning_sent"] == 0
    assert rows["faller"]["days_inactive"] == 0, (
        "the clock must restart from now, not from the last watch"
    )


def test_someone_already_past_the_threshold_gets_a_full_fresh_period():
    """The explicitly requested case: 30+ days idle, then booted off the top three."""
    h = _Harness(
        users=[_user("veteran", 1, 365, is_top_watcher=True, warning_sent=True)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("veteran", 5)],
    )
    rows = h.run()
    assert h.exemption_lost == ["veteran"]
    assert h.removed == [], "365 days idle, but the fresh period must protect them"
    assert rows["veteran"]["days_inactive"] == 0
    assert rows["veteran"]["warning_sent"] == 0, "the stale warning must be cleared"


def test_the_clock_then_runs_from_losing_the_exemption():
    """Having lost it 40 days ago, they are removable even though the last watch
    was far older still.
    """
    lost = datetime.now(timezone.utc) - timedelta(days=40)
    h = _Harness(
        users=[_user("expired", 1, 365, is_top_watcher=False,
                     exemption_lost_at=lost, warning_sent=True)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("expired", 5)],
    )
    h.run()
    assert h.removed == ["expired"], f"warned={h.warned} removed={h.removed}"


def test_still_protected_partway_through_the_fresh_period():
    lost = datetime.now(timezone.utc) - timedelta(days=10)
    h = _Harness(
        users=[_user("recent", 1, 365, is_top_watcher=False,
                     exemption_lost_at=lost)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("recent", 5)],
    )
    h.run()
    assert h.removed == [] and h.warned == [], "only 10 of 30 days have passed"


def test_re_entering_the_top_three_clears_the_lost_state():
    lost = datetime.now(timezone.utc) - timedelta(days=10)
    h = _Harness(
        users=[_user("returner", 1, 200, is_top_watcher=False,
                     exemption_lost_at=lost, warning_sent=True)],
        tautulli_rows=[_u("returner", 5000), _u("b", 10), _u("c", 9), _u("d", 8)],
    )
    rows = h.run()
    assert rows["returner"]["exemption_lost_at"] is None
    assert rows["returner"]["is_top_watcher"] == 1
    assert rows["returner"]["warning_sent"] == 0
    assert h.exemption_lost == [] and h.removed == []


def test_losing_it_twice_notifies_twice_but_only_on_transitions():
    """No message while they simply remain outside the top three."""
    h = _Harness(
        users=[_user("steady", 1, 200, is_top_watcher=False,
                     exemption_lost_at=datetime.now(timezone.utc))],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("steady", 10)],
    )
    h.run()
    assert h.exemption_lost == [], "already out; this is not a transition"


def test_the_owner_is_exempt_regardless_of_rank():
    """Removing the account the bot authenticates with would be nonsense."""
    h = _Harness(
        users=[_user("owner", 999, 400, warning_sent=True)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("owner", 1)],
    )
    rows = h.run()
    assert h.removed == [] and h.warned == []
    assert rows["owner"]["is_top_watcher"] == 1


def test_the_plex_owner_is_exempt_without_a_linked_discord_account():
    """The owner's row needn't carry BOT_OWNER_ID: nobody linked it, or someone else set the bot up."""
    h = _Harness(
        users=[_user("plexowner", None, 400, warning_sent=True)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("plexowner", 1)],
        plex_owner="PlexOwner",
    )
    h.run()
    assert h.removed == [] and h.warned == [], f"removed={h.removed} warned={h.warned}"


def test_the_plex_owner_is_found_by_account_id_too():
    """Tautulli and Plexbie can call the owner something else than their Plex account name."""
    row = _user("Server Owner", None, 400, warning_sent=True, plex_user_id=777)
    h = _Harness(
        users=[row, _user("idler", 2, 40, warning_sent=True)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700),
                       _u("owner-alias", 1, user_id=777), _u("idler", 10)],
        owner_account_id="777",
    )
    h.run()
    assert h.removed == ["idler"], f"removed={h.removed} warned={h.warned}"


def test_the_plex_owner_stays_exempt_while_plex_is_unreachable():
    """Once found, the owner is remembered: a pass that can't ask Plex mustn't tell them
    they're out of the top three, or restart their clock."""
    h = _Harness(
        users=[_user("plexowner", None, 400)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("plexowner", 1)],
        plex_owner="PlexOwner",
    )
    h.run()
    h.plex_owner = None
    rows = h.run(seed=False)
    assert h.exemption_lost == [] and h.warned == [] and h.removed == []
    assert rows["plexowner"]["is_top_watcher"] == 1


def test_the_plex_owner_is_found_through_plex_tv():
    """plex.tv names the owner's account on every pass, with no website sign-in needed.
    Plexbie and Tautulli call them something else, so only the account id ties them."""
    import core.plex_account as plex_account

    class SignedIn(_Harness):
        def _make_cog(self):
            module, cog = super()._make_cog()
            cog.services.config.plex_token = "owner-token"
            return module, cog

    def owner_only(config):
        return [{"id": 777, "title": "realowner", "username": "realowner", "email": "",
                 "owner": True, "thumb": None}]

    h = SignedIn(
        users=[_user("Server Owner", None, 400, warning_sent=True)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("Server Owner", 1, user_id=777)],
    )
    saved = plex_account.shared_accounts
    plex_account.shared_accounts = owner_only
    try:
        h.run()
    finally:
        plex_account.shared_accounts = saved
    assert h.removed == [] and h.warned == [], f"removed={h.removed} warned={h.warned}"


def test_no_standings_means_nobody_s_clock_starts():
    """A Tautulli outage must not start every user's timer at once.

    This is the failure mode worth protecting against: treating "cannot compute
    standings" as "nobody is exempt" would drop every top watcher out on the same
    pass and restart all their clocks.
    """
    h = _Harness(
        users=[_user("whale", 1, 200, is_top_watcher=True)],
        tautulli_rows=[_u("whale", 0), _u("b", 0)],   # nobody has any watch time
    )
    rows = h.run()
    assert h.exemption_lost == [], "an outage must not look like losing exemption"
    assert h.removed == [] and h.warned == []
    assert rows["whale"]["is_top_watcher"] == 1, "state must be left untouched"
    assert rows["whale"]["exemption_lost_at"] is None


def test_aliased_identities_count_as_one_for_the_exemption():
    """Split totals could rank someone out of an exemption they have earned."""
    h = _Harness(
        users=[_user("Sammy", 1, 200)],
        tautulli_rows=[_u("Sammy", 60), _u("Sam.Rivera", 500),
                       _u("a", 400), _u("b", 300), _u("c", 200)],
        aliases={"Sammy": "Sam.Rivera"},
    )
    rows = h.run()
    assert h.removed == [] and h.warned == []
    assert rows["Sammy"]["is_top_watcher"] == 1, (
        "560s combined should rank first; split it would be 60s and outside the top 3"
    )


# ===================================================================
# the inactivity clock always runs from something
# ===================================================================

def test_a_newly_tracked_user_gets_a_finite_period_not_a_permanent_pass():
    """Tautulli history predating the tracking row must not exempt forever.

    The guard for re-approved users used to `continue` with days_inactive = 0.
    Because Tautulli's last_seen only moves forward when someone watches, that
    condition never stopped being true - so a user added after a long absence was
    never warned and never removed. The clock now counts from when tracking
    started.
    """
    created = datetime.now(timezone.utc) - timedelta(days=5)
    h = _Harness(
        users=[{
            "plex_username": "fresh",
            "discord_id": 1,
            "created_at": created,
            "last_watched": None,
        }],
        # last watched 91 days ago, well before the tracking row existed
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700),
                       {"friendly_name": "fresh", "duration": 10,
                        "last_seen": _seen(91)}],
    )
    rows = h.run()
    assert h.removed == [], "5 days into a 30-day period; not yet removable"
    assert h.warned == []
    assert rows["fresh"]["days_inactive"] == 5, (
        f"expected 5 days counted from tracking start, got "
        f"{rows['fresh']['days_inactive']} - 0 would mean a permanent pass"
    )


def test_that_period_does_eventually_expire():
    created = datetime.now(timezone.utc) - timedelta(days=40)
    h = _Harness(
        users=[{
            "plex_username": "lapsed",
            "discord_id": 1,
            "created_at": created,
            "last_watched": None,
            "warning_sent": True,
        }],
        tautulli_rows=[{**_u("a", 900), "last_seen": _seen(1)}, _u("b", 800), _u("c", 700),
                       {"friendly_name": "lapsed", "duration": 10,
                        "last_seen": _seen(200)}],
    )
    h.run()
    assert h.removed == ["lapsed"], (
        f"40 days after tracking started, removal is due; got warned={h.warned}"
    )


def test_never_remove_is_never_warned_or_removed():
    """The admin's "Never remove" switch, for family shared directly in Plex."""
    h = _Harness(
        users=[_user("grandma", 1, 400, warning_sent=True, never_remove=True), _user("idler", 2, 40, warning_sent=True)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("grandma", 1), _u("idler", 10)],
    )
    h.run()
    assert "grandma" not in h.removed and "grandma" not in h.warned
    assert h.removed == ["idler"], "the switch only protects the person it's on"


def test_people_are_found_in_tautulli_by_their_plex_account_id():
    """A row named after the Plex account ("Morgan Hale") with Tautulli showing another name."""
    row = _user("Morgan Hale", 7, 40, warning_sent=True, plex_user_id=4242)
    other = dict(_u("morgan_h", 10))
    other["user_id"] = 4242
    h = _Harness(users=[row], tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), other])
    h.run()
    assert h.removed == ["Morgan Hale"], "matched by id, so the inactivity check applies to her"


def test_a_rename_in_tautulli_keeps_the_top_three_protection():
    """Standings are keyed by Tautulli's name; the row is found by account id, so it still counts."""
    row = _user("river975", 9, 40, warning_sent=True, plex_user_id=975)
    renamed = _u("River Alt", 1000, user_id=975)
    h = _Harness(users=[row], tautulli_rows=[renamed, _u("b", 800), _u("c", 700), _u("d", 10)])
    h.run()
    assert "river975" not in h.removed and "river975" not in h.warned


# ===================================================================
# brakes: stale Tautulli history, and too many removals in one pass
# ===================================================================

def _seen_row(name, duration, days_ago):
    return {**_u(name, duration), "last_seen": _seen(days_ago)}


def test_stale_tautulli_history_warns_and_removes_nobody():
    """Tautulli still answers, but nothing has been recorded for 10 days.

    Every last_seen freezes when Tautulli stops collecting (a broken Plex
    connection, a changed server address), so everyone drifts towards removal.
    """
    h = _Harness(
        users=[_user("idler", 1, 40, warning_sent=True), _user("drifter", 2, 26)],
        tautulli_rows=[_seen_row("a", 900, 10), _seen_row("b", 800, 12), _seen_row("c", 700, 15),
                       _seen_row("idler", 10, 40), _seen_row("drifter", 10, 26)],
    )
    rows = h.run()
    assert h.removed == [] and h.warned == [], f"removed={h.removed} warned={h.warned}"
    assert h.alerts, "the admins must hear that the check paused"
    assert rows["idler"]["days_inactive"] == 40, "the activity figures are still saved"
    assert rows["drifter"]["warning_sent"] == 0, "a warning that wasn't sent must not be recorded"


def test_a_recent_play_by_anyone_keeps_the_check_running():
    h = _Harness(
        users=[_user("idler", 1, 40, warning_sent=True), _user("drifter", 2, 26)],
        tautulli_rows=[_seen_row("a", 900, 1), _seen_row("b", 800, 12), _seen_row("c", 700, 15),
                       _seen_row("idler", 10, 40), _seen_row("drifter", 10, 26)],
    )
    h.run()
    assert h.removed == ["idler"] and h.warned == ["drifter"]
    assert h.alerts == []


def test_a_removal_that_went_through_drops_the_row():
    h = _Harness(
        users=[_user("idler", 1, 40, warning_sent=True)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("idler", 10)],
    )
    rows = h.run()
    assert h.removed == ["idler"] and "idler" not in rows


def test_a_failed_removal_keeps_the_row_with_todays_numbers():
    """The row is the only record that a retry is owed, and the next pass reads it."""
    h = _Harness(
        users=[_user("idler", 1, 40, warning_sent=True, days_inactive=12)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700), _u("idler", 10)],
        removal_works=False,
    )
    rows = h.run()
    assert h.removed == ["idler"], "the removal was never tried"
    assert "idler" in rows, "the row went while they still have Plex access"
    assert rows["idler"]["days_inactive"] == 40, "the activity figures from this pass were dropped"


def test_removing_too_many_at_once_removes_nobody():
    """Five of five tracked people due in one pass looks like a fault, not a household."""
    names = ["p1", "p2", "p3", "p4", "p5"]
    h = _Harness(
        users=[_user(n, i, 40, warning_sent=True) for i, n in enumerate(names, start=1)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700)] + [_u(n, 10) for n in names],
    )
    rows = h.run()
    assert h.removed == [], f"removed {h.removed} in one pass"
    assert h.alerts, "the admins must hear why nobody was removed"
    assert set(rows) == set(names), "every row is kept"


def test_up_to_three_removals_go_ahead_in_a_small_household():
    names = ["p1", "p2", "p3"]
    h = _Harness(
        users=[_user(n, i, 40, warning_sent=True) for i, n in enumerate(names, start=1)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700)] + [_u(n, 10) for n in names],
    )
    h.run()
    assert sorted(h.removed) == names
    assert h.alerts == []


def test_a_quarter_of_a_larger_household_can_go_in_one_pass():
    idle = ["p1", "p2", "p3", "p4"]
    active = [f"q{i}" for i in range(12)]
    h = _Harness(
        users=([_user(n, i, 40, warning_sent=True) for i, n in enumerate(idle, start=1)]
               + [_user(n, 100 + i, 1) for i, n in enumerate(active)]),
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700)] + [_u(n, 10) for n in idle + active],
    )
    h.run()
    assert sorted(h.removed) == idle, "4 of 16 is a quarter: allowed"


def test_a_name_match_for_another_account_is_never_measured():
    """The row is account 55; Tautulli's "river" is account 66, who has been idle."""
    row = _user("river", 9, 1, plex_user_id=55, warning_sent=True)
    other = {**_u("river", 10, user_id=66), "last_seen": _seen(40)}
    h = _Harness(users=[row], tautulli_rows=[_seen_row("a", 900, 1), _u("b", 800), _u("c", 700), other])
    h.run()
    assert h.removed == [] and h.warned == [], "account 66's inactivity removed account 55's person"


def test_people_already_off_the_share_do_not_count_towards_the_limit():
    """Five due, but two of their accounts were taken off the share in Plex already."""
    import core.plex_account as plex_account

    names = ["p1", "p2", "p3", "p4", "p5"]

    class SignedIn(_Harness):
        def _make_cog(self):
            module, cog = super()._make_cog()
            cog.services.config.plex_token = "owner-token"
            return module, cog

    def no_shares(config):
        raise LookupError("offline")

    h = SignedIn(
        users=[_user(n, i, 40, warning_sent=True, plex_user_id=i) for i, n in enumerate(names, start=1)],
        tautulli_rows=[_u("a", 900), _u("b", 800), _u("c", 700)]
                      + [_u(n, 10, user_id=i) for i, n in enumerate(names, start=1)],
    )
    from plugins.user_mgmt import cog as module
    saved = module._fetch_shared_account_ids, plex_account.shared_accounts
    module._fetch_shared_account_ids = lambda config: {"1", "2", "3"}
    plex_account.shared_accounts = no_shares
    try:
        h.run()
    finally:
        module._fetch_shared_account_ids, plex_account.shared_accounts = saved
    assert h.alerts == [], "three real removals are within the limit"
    assert {"p1", "p2", "p3"} <= set(h.removed)


# ===================================================================
# the warning, then the removal
# ===================================================================

def _board(*rows):
    """Three busy people with a recent play, so nobody below is exempt and history isn't stale."""
    return [_seen_row("a", 900, 1), _seen_row("b", 800, 1), _seen_row("c", 700, 1), *rows]


def test_idle_past_the_warning_threshold_is_warned_not_removed():
    h = _Harness(users=[_user("drifter", 1, 26)], tautulli_rows=_board(_seen_row("drifter", 10, 26)))
    rows = h.run()
    assert h.warned == ["drifter"] and h.removed == []
    assert rows["drifter"]["warning_sent"] == 1, "the warning must be saved, or it repeats every day"
    assert rows["drifter"]["days_inactive"] == 26


def test_idle_past_removal_but_never_warned_is_warned_first():
    """A warning nobody had a chance to act on is not a warning."""
    h = _Harness(users=[_user("lapsed", 1, 40)], tautulli_rows=_board(_seen_row("lapsed", 10, 40)))
    rows = h.run()
    assert h.warned == ["lapsed"]
    assert h.removed == [], "removed on the same pass that warned"
    assert rows["lapsed"]["warning_sent"] == 1


def test_the_removal_comes_on_a_later_pass_than_the_warning():
    h = _Harness(users=[_user("lapsed", 1, 40)], tautulli_rows=_board(_seen_row("lapsed", 10, 40)))
    h.run()
    assert h.warned == ["lapsed"] and h.removed == []
    h.run(seed=False)
    assert h.warned == ["lapsed"], "warned twice"
    assert h.removed == ["lapsed"]


def test_watching_after_a_warning_clears_it():
    h = _Harness(users=[_user("returner", 1, 26, warning_sent=True)],
                 tautulli_rows=_board(_seen_row("returner", 10, 3)))
    rows = h.run()
    assert h.warned == [] and h.removed == []
    assert rows["returner"]["warning_sent"] == 0, "a stale warning would let the next lapse remove them unwarned"
    assert rows["returner"]["days_inactive"] == 3


def test_no_new_history_still_warns_from_the_last_known_watch():
    """Tautulli has no last_seen for them, but Plexbie saw them watch 26 days ago."""
    h = _Harness(users=[_user("quiet", 1, 26)], tautulli_rows=_board(_u("quiet", 10)))
    rows = h.run()
    assert h.warned == ["quiet"] and h.removed == []
    assert rows["quiet"]["warning_sent"] == 1


def test_a_warning_that_reached_nobody_tells_the_admins():
    """Closed DMs, no alerts, no email: the warning still counts, so they aren't warned daily
    and never removed, but the admins hear that they weren't told."""
    h = _Harness(users=[_user("closed", 1, 26)], tautulli_rows=_board(_seen_row("closed", 10, 26)))
    h.warning_reaches = False
    rows = h.run()
    assert h.warned == ["closed"]
    assert rows["closed"]["warning_sent"] == 1
    assert h.alerts == ["Inactivity warning not delivered"], h.alerts


def test_a_long_list_of_unwarned_people_is_shortened():
    idle = [f"m{i:02}" for i in range(20)]
    h = _Harness(users=[_user(n, 10 + i, 26) for i, n in enumerate(idle)],
                 tautulli_rows=_board(*[_seen_row(n, 10, 26) for n in idle]))
    h.warning_reaches = False
    h.run()
    text, push = h.alert_texts[0]
    assert "and 5 more" in text and "m19" not in text
    assert "20 people" in push and "m00" not in push


def test_a_warning_that_got_through_tells_the_admins_nothing():
    h = _Harness(users=[_user("open", 1, 26)], tautulli_rows=_board(_seen_row("open", 10, 26)))
    h.run()
    assert h.warned == ["open"] and h.alerts == []


def _warning_cog(dm_fails, other_route):
    """The real _send_warning_dm, with Discord and notify_member faked. Returns (cog, calls, undo)."""
    import core.notify as notify
    from plugins.user_mgmt import cog as module

    calls = {"dm": [], "notify": []}

    async def fake_send_user_dm(bot, services, user, **kw):
        calls["dm"].append(kw)
        if dm_fails:
            raise RuntimeError("Cannot send messages to this user")

    async def fake_notify_member(services, **kw):
        calls["notify"].append(kw)
        return other_route

    class FakeBot:
        async def fetch_user(self, uid):
            return SimpleNamespace(id=uid)

    class FakeConfig:
        inactivity_warning_days = 25
        inactivity_removal_days = 30

    cog = object.__new__(module.UserMgmtCog)
    cog.bot = FakeBot()
    cog.services = SimpleNamespace(config=FakeConfig())
    saved = module.send_user_dm, notify.notify_member
    module.send_user_dm, notify.notify_member = fake_send_user_dm, fake_notify_member

    def undo():
        module.send_user_dm, notify.notify_member = saved
    return cog, calls, undo


def _member(**kw):
    spec = {"plex_username": "sam", "discord_id": 42, "discord_username": "sam#1", "plex_email": "sam@example.com"}
    spec.update(kw)
    return SimpleNamespace(**spec)


def test_a_delivered_warning_dm_needs_no_other_route():
    cog, calls, undo = _warning_cog(dm_fails=False, other_route="none")
    try:
        assert asyncio.run(cog._send_warning_dm(_member())) is True
    finally:
        undo()
    assert len(calls["dm"]) == 1 and calls["notify"] == []


def test_a_closed_dm_falls_back_to_an_alert_or_email():
    cog, calls, undo = _warning_cog(dm_fails=True, other_route="email")
    try:
        assert asyncio.run(cog._send_warning_dm(_member())) is True
    finally:
        undo()
    assert calls["dm"][0].get("own_fallback") is True, "the app copy would arrive twice"
    sent = calls["notify"][0]
    assert sent["discord_id"] == "42" and sent["plex_name"] == "sam" and sent["email"] == "sam@example.com"
    assert "30" not in sent["title"] and "5 days" in sent["body"]


def test_a_warning_with_no_route_at_all_reports_failure():
    cog, calls, undo = _warning_cog(dm_fails=True, other_route="none")
    try:
        assert asyncio.run(cog._send_warning_dm(_member())) is False
    finally:
        undo()
    assert len(calls["notify"]) == 1


def test_a_member_without_discord_is_warned_another_way():
    cog, calls, undo = _warning_cog(dm_fails=False, other_route="push")
    try:
        assert asyncio.run(cog._send_warning_dm(_member(discord_id=None))) is True
    finally:
        undo()
    assert calls["dm"] == [] and len(calls["notify"]) == 1
