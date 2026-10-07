# path: tests/test_utc_times.py
"""Times are UTC, whatever zone the host runs in.

Every timestamp column holds UTC, but SQLite hands it back without a zone, and
Python reads a zone-less datetime as the host's local time. Unraid sets TZ by
default, so Discord's "Joined At" and "Last Watch" moved by the host's offset,
and the website's "since" times were wrong for every viewer outside UTC.
Stored strings come in three shapes (ISO with or without a zone, with a "Z",
or Unix seconds); one parser reads them all the same way.
"""
import asyncio
import contextlib
import os
import pathlib
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import conftest  # noqa: F401
from helpers import HOME, FakeInteraction, FakeMember, PermConfig

from test_home_events import _init

NOON = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


@contextlib.contextmanager
def _host_zone(spec: str):
    """Run as a host whose local time is `spec` (a POSIX TZ string, no zone files needed)."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = spec
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


# ------------------------------------------------------------------ parse_utc
def test_parse_utc_reads_every_stored_shape_as_the_same_moment():
    from utils.formatting import parse_utc

    for value in ("2026-01-15T12:00:00+00:00", "2026-01-15T12:00:00Z", "2026-01-15T12:00:00",
                  "2026-01-15T07:00:00-05:00", int(NOON.timestamp()), float(NOON.timestamp()),
                  NOON, NOON.replace(tzinfo=None)):
        got = parse_utc(value)
        assert got == NOON and got.tzinfo is not None, (value, got)


def test_parse_utc_gives_none_for_anything_that_isnt_a_time():
    from utils.formatting import parse_utc

    for value in (None, "", 0, "soon", "None", [], {}, True):
        assert parse_utc(value) is None, value


def test_parse_utc_does_not_depend_on_the_host_zone():
    from utils.formatting import parse_utc

    with _host_zone("XYZ+05"):
        assert parse_utc("2026-01-15T12:00:00").timestamp() == NOON.timestamp()


# ----------------------------------------------------------------- ensure_utc
def test_ensure_utc_marks_a_zone_less_time_as_utc_and_leaves_others_alone():
    from utils.formatting import ensure_utc

    assert ensure_utc(None) is None
    assert ensure_utc(NOON.replace(tzinfo=None)) == NOON
    west = NOON.astimezone(timezone(timedelta(hours=-5)))
    assert ensure_utc(west) is west
    with _host_zone("XYZ+05"):
        assert ensure_utc(NOON.replace(tzinfo=None)).timestamp() == NOON.timestamp()


def test_the_manage_page_shows_a_stored_last_watch_not_the_time_it_was_asked():
    # The People list passes the database's datetime straight in.
    from portal.data import _iso

    assert _iso(NOON.replace(tzinfo=None)) == NOON.isoformat()
    assert _iso("2026-01-15T12:00:00Z") == NOON.isoformat()
    assert datetime.fromisoformat(_iso(None)) > datetime.now(timezone.utc) - timedelta(minutes=5)


# ---------------------------------------------------------------- /who-invited
class _Followup:
    def __init__(self):
        self.embeds = []

    async def send(self, content=None, embed=None, ephemeral=False):
        self.embeds.append(embed)


def _who_invited(row):
    """/who-invited for a member whose join is `row`, answered as a host five hours west of UTC."""
    from plugins.invite_tracker.cog import InviteTrackerCog
    from plugins.invite_tracker.models import InviteUse
    from database.session import get_session

    cog = InviteTrackerCog(SimpleNamespace(guilds=[]), SimpleNamespace(config=PermConfig()))
    interaction = FakeInteraction(FakeMember(111), PermConfig())
    interaction.guild = SimpleNamespace(id=HOME, get_role=lambda rid: None)
    interaction.followup = _Followup()
    member = SimpleNamespace(id=7, mention="<@7>")

    async def go():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        async with get_session() as session:
            session.add(InviteUse(guild_id=str(HOME), joiner_id="7", joiner_name="Joiner", joined_at=NOON, **row))
            await session.commit()
        with _host_zone("XYZ+05"):
            await cog.who_invited.callback(cog, interaction, member)

    asyncio.run(go())
    (embed,) = interaction.followup.embeds
    return {f.name: f.value for f in embed.fields}


def test_who_invited_shows_the_join_in_utc_on_a_host_with_a_zone():
    fields = _who_invited({"invite_code": "abc", "inviter_id": "55", "inviter_name": "Sam"})
    assert fields["Joined At"] == f"<t:{int(NOON.timestamp())}:F>", fields
    assert fields["Invited By"] == "<@55>"


def test_who_invited_names_the_inviter_when_discord_gave_none():
    # A vanity URL or an invite whose creator left: no account to mention.
    fields = _who_invited({"invite_code": "vanity", "inviter_id": "0", "inviter_name": "Unknown"})
    assert fields["Invited By"] == "Unknown", fields


# ------------------------------------------------------ the website's joins
def test_the_websites_joins_carry_their_utc_offset():
    from core.config import Config
    from plugins.invite_tracker.models import InviteUse
    from database.session import get_session
    from helpers import FakeServices
    from test_portal import ADMIN, _Actions

    guild = SimpleNamespace(id=HOME, get_member=lambda uid: None, get_role=lambda rid: None, text_channels=[], me=None)

    class Bot:
        def get_guild(self, gid):
            return guild if gid == HOME else None

        def get_cog(self, name):
            return None

    actions = _Actions(FakeServices(Config(guild_id=HOME)))
    actions.bot = Bot()

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        async with get_session() as session:
            session.add(InviteUse(guild_id=str(HOME), invite_code="abc", inviter_id="55", inviter_name="Sam",
                                  joiner_id="7", joiner_name="Joiner", joined_at=NOON))
            await session.commit()
        return await actions.discord_overview(ADMIN)

    (join,) = asyncio.run(scenario())["joins"]
    assert datetime.fromisoformat(join["at"]) == NOON and join["at"].endswith("+00:00"), join
