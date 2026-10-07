"""Which invite a new member used, worked out from the server's invite counts.

Discord doesn't say which invite someone joined with, so the tracker compares
each invite's use count with the last listing. Two cases need more than that:
an invite with a use limit is deleted by Discord on its last use, so that join
shows no bump at all; and two people joining close together must each be
matched against the listing the previous join left, not both against the same
old one.
"""
import asyncio
import pathlib
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import conftest  # noqa: F401
from helpers import HOME

from test_home_events import _init, _Invite, _InviteGuild, _Joiner
from test_home_guild import _config


def _invite(code, uses, max_uses=0):
    invite = _Invite(code, uses)
    invite.max_uses = max_uses
    return invite


def _cog(guild):
    from plugins.invite_tracker.cog import InviteTrackerCog

    class Bot:
        guilds = [guild]

        def get_guild(self, gid):
            return guild if gid == guild.id else None

    return InviteTrackerCog(Bot(), SimpleNamespace(config=_config(guild_id=guild.id)))


def _deleted(code, guild):
    """What on_invite_delete gets: only the code and where it was."""
    return SimpleNamespace(code=code, guild=guild)


async def _uses():
    from sqlalchemy import select
    from database.session import get_session
    from plugins.invite_tracker.models import InviteUse
    async with get_session() as session:
        rows = (await session.execute(select(InviteUse).order_by(InviteUse.id))).scalars().all()
    return [(r.joiner_id, r.invite_code) for r in rows]


def _run(scenario):
    async def go():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await scenario()
        return await _uses()
    return asyncio.run(go())


def test_a_single_use_invite_deleted_before_the_join_is_handled_is_still_credited():
    home = _InviteGuild(HOME, [_invite("keep", 3)])
    cog = _cog(home)
    cog.invite_cache[str(HOME)] = {"keep": _invite("keep", 3), "once": _invite("once", 0, max_uses=1)}

    async def scenario():
        await cog.on_invite_delete(_deleted("once", home))
        await cog.on_member_join(_Joiner(7, home))

    assert _run(scenario) == [("7", "once")]


def test_the_last_use_of_a_limited_invite_is_credited_before_its_delete_arrives():
    home = _InviteGuild(HOME, [_invite("keep", 3)])
    cog = _cog(home)
    cog.invite_cache[str(HOME)] = {"keep": _invite("keep", 3), "five": _invite("five", 4, max_uses=5)}

    async def scenario():
        await cog.on_member_join(_Joiner(7, home))
        await cog.on_invite_delete(_deleted("five", home))
        # A later join that moves nothing isn't pinned on the same invite again.
        await cog.on_member_join(_Joiner(8, home))

    assert _run(scenario) == [("7", "five")]


def test_an_invite_deleted_with_uses_to_spare_is_not_credited():
    home = _InviteGuild(HOME, [])
    cog = _cog(home)
    cog.invite_cache[str(HOME)] = {"spare": _invite("spare", 1, max_uses=5), "open": _invite("open", 2)}

    async def scenario():
        await cog.on_invite_delete(_deleted("spare", home))
        await cog.on_invite_delete(_deleted("open", home))
        await cog.on_member_join(_Joiner(7, home))

    assert _run(scenario) == []


def test_two_last_uses_at_once_are_not_guessed_between():
    home = _InviteGuild(HOME, [])
    cog = _cog(home)
    cog.invite_cache[str(HOME)] = {"a": _invite("a", 0, max_uses=1), "b": _invite("b", 0, max_uses=1)}

    async def scenario():
        await cog.on_member_join(_Joiner(7, home))

    assert _run(scenario) == []


def test_an_expired_single_use_invite_is_not_credited():
    # Discord drops an invite whose time ran out from the listing, with no
    # delete event; it wasn't used up, so a join that moves nothing stays unmatched.
    lapsed = _invite("lapsed", 0, max_uses=1)
    lapsed.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    # One from an invite event may carry only its age.
    aged = _invite("aged", 0, max_uses=1)
    aged.created_at, aged.max_age = datetime.now(timezone.utc) - timedelta(days=8), 7 * 24 * 3600
    for expired in (lapsed, aged):
        home = _InviteGuild(HOME, [])
        cog = _cog(home)
        cog.invite_cache[str(HOME)] = {expired.code: expired}

        async def scenario():
            await cog.on_member_join(_Joiner(7, home))

        assert _run(scenario) == [], expired.code


def test_an_expired_invite_does_not_crowd_out_the_one_really_used_up():
    lapsed = _invite("lapsed", 0, max_uses=1)
    lapsed.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    home = _InviteGuild(HOME, [])
    cog = _cog(home)
    cog.invite_cache[str(HOME)] = {"lapsed": lapsed, "once": _invite("once", 0, max_uses=1)}

    async def scenario():
        await cog.on_invite_delete(_deleted("once", home))
        await cog.on_member_join(_Joiner(7, home))

    assert _run(scenario) == [("7", "once")]


def test_a_new_invite_is_added_without_listing_the_others_again():
    # 7 has used "a", but their join isn't handled until after someone makes a
    # new invite: that must not take in a's new count before the join compares.
    home = _InviteGuild(HOME, [_invite("a", 2), _invite("new", 0)])
    cog = _cog(home)
    cog.invite_cache[str(HOME)] = {"a": _invite("a", 1)}

    async def scenario():
        await cog.on_invite_create(_Invite("new", 0, guild=home))
        assert home.listed == 0 and sorted(cog.invite_cache[str(HOME)]) == ["a", "new"]
        await cog.on_member_join(_Joiner(7, home))

    assert _run(scenario) == [("7", "a")]


def test_a_new_invite_before_the_first_listing_loads_them_all():
    home = _InviteGuild(HOME, [_invite("a", 1), _invite("new", 0)])
    cog = _cog(home)
    asyncio.run(cog.on_invite_create(_Invite("new", 0, guild=home)))
    assert home.listed == 1 and sorted(cog.invite_cache[str(HOME)]) == ["a", "new"]


class _BusyGuild(_InviteGuild):
    """Each listing shows the next state: someone else joins right after a listing."""

    def __init__(self, gid, states):
        super().__init__(gid)
        self.states = states

    async def invites(self):
        await asyncio.sleep(0)
        state = self.states[min(self.listed, len(self.states) - 1)]
        self.listed += 1
        return list(state)


def test_two_joins_close_together_are_each_credited_to_their_own_invite():
    # 7 joins with "a"; 8 joins with "b" just after the first listing is taken.
    home = _BusyGuild(HOME, [
        [_invite("a", 2), _invite("b", 1)],
        [_invite("a", 2), _invite("b", 2)],
    ])
    cog = _cog(home)
    cog.invite_cache[str(HOME)] = {"a": _invite("a", 1), "b": _invite("b", 1)}

    async def scenario():
        await asyncio.gather(cog.on_member_join(_Joiner(7, home)), cog.on_member_join(_Joiner(8, home)))

    assert sorted(_run(scenario)) == [("7", "a"), ("8", "b")]


def test_a_join_during_the_previous_ones_bookkeeping_is_not_lost():
    # 8 joins with "b" after 7's listing was compared: the next listing must not
    # be taken as the new baseline before 8's own join is matched.
    home = _BusyGuild(HOME, [
        [_invite("a", 2), _invite("b", 1)],
        [_invite("a", 2), _invite("b", 2)],
    ])
    cog = _cog(home)
    cog.invite_cache[str(HOME)] = {"a": _invite("a", 1), "b": _invite("b", 1)}

    async def scenario():
        await cog.on_member_join(_Joiner(7, home))
        await cog.on_member_join(_Joiner(8, home))

    assert _run(scenario) == [("7", "a"), ("8", "b")]


def test_listing_the_invites_keeps_them_in_memory_only():
    from sqlalchemy import select
    from database.session import get_session
    from plugins.invite_tracker.models import InviteTracker
    home = _InviteGuild(HOME, [_invite("a", 1), _invite("b", 0, max_uses=1)])
    cog = _cog(home)

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await cog.on_ready()
        async with get_session() as session:
            return (await session.execute(select(InviteTracker))).scalars().all()

    assert asyncio.run(scenario()) == []
    assert sorted(cog.invite_cache[str(HOME)]) == ["a", "b"]
