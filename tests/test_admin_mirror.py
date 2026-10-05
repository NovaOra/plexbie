# path: tests/test_admin_mirror.py
"""Admin-channel mirroring must never fail the operation it reports on.

Regression coverage for: _send_admin_receipt had no exception handling, so a
failure to post the receipt propagated out of send_user_dm. Callers read that as
"the DM failed" - user_invites.approve is not wrapped at all, so it aborted after
the Plex invite had already been sent and the role assigned.
"""
import asyncio

import discord

import conftest  # noqa: F401

from core.admin_mirror import send_user_dm


class _Config:
    admin_channel_id = 123456789


class _Services:
    config = _Config()


class _Channel:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send(self, **kwargs):
        if self.fail:
            raise discord.HTTPException(_Response(), "mirror boom")
        self.sent.append(kwargs)


class _Response:
    status = 403
    reason = "Forbidden"


class _Bot:
    def __init__(self, channel):
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel

    async def fetch_channel(self, channel_id):
        return self._channel


class _User:
    def __init__(self, fail=False):
        self.id = 42
        self.fail = fail
        self.received = []

    async def send(self, content=None, embed=None):
        if self.fail:
            raise discord.Forbidden(_Response(), "dms closed")
        self.received.append((content, embed))

    def __str__(self):
        return "TestUser#0001"


def _run(bot, user):
    return asyncio.run(
        send_user_dm(bot, _Services(), user, context="test", content="hello")
    )


# --- the regression ---

def test_mirror_failure_does_not_fail_a_delivered_dm():
    channel = _Channel(fail=True)
    user = _User()
    _run(_Bot(channel), user)          # must not raise
    assert user.received, "the DM itself should still have been delivered"


def test_successful_path_mirrors_once():
    channel = _Channel()
    user = _User()
    _run(_Bot(channel), user)
    assert len(user.received) == 1
    assert len(channel.sent) == 1
    assert "DM sent" in channel.sent[0]["content"]


# --- a genuinely failed DM must still raise, so callers can react ---

def test_failed_dm_still_raises():
    user = _User(fail=True)
    try:
        _run(_Bot(_Channel()), user)
    except discord.Forbidden:
        pass
    else:
        raise AssertionError("a DM that could not be delivered must propagate")


def test_failed_dm_is_reported_to_admins():
    channel = _Channel()
    user = _User(fail=True)
    try:
        _run(_Bot(channel), user)
    except discord.Forbidden:
        pass
    assert channel.sent, "admins should be told the DM failed"
    assert "DM failed" in channel.sent[0]["content"]


def test_failed_dm_and_failed_mirror_still_raises_the_dm_error():
    """The DM error is the one the caller needs; the mirror error is noise."""
    user = _User(fail=True)
    try:
        _run(_Bot(_Channel(fail=True)), user)
    except discord.Forbidden:
        pass
    else:
        raise AssertionError("expected the DM's Forbidden, not the mirror's error")


# --- missing configuration must not break delivery either ---

def test_unconfigured_admin_channel_does_not_fail_the_dm():
    class _NoChannelConfig:
        admin_channel_id = None

    class _NoChannelServices:
        config = _NoChannelConfig()

    user = _User()
    asyncio.run(
        send_user_dm(_Bot(None), _NoChannelServices(), user, context="test", content="hi")
    )
    assert user.received


def test_unresolvable_admin_channel_does_not_fail_the_dm():
    class _MissingBot:
        def get_channel(self, channel_id):
            return None

        async def fetch_channel(self, channel_id):
            raise discord.NotFound(_Response(), "gone")

    user = _User()
    asyncio.run(
        send_user_dm(_MissingBot(), _Services(), user, context="test", content="hi")
    )
    assert user.received
