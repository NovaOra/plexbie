# path: tests/test_admin_mirror.py
"""send_user_dm: the DM, logged on Manage → Messages, and nothing in the admin channel.

(It used to copy every DM there as a receipt; a failure to post the receipt once made
user_invites.approve abort after the Plex invite had already gone. The receipts are gone:
Manage → Messages and the DM threads are the record now.)
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


# --- every DM is logged on Manage → Messages; the admin channel gets no receipts ---

def test_a_delivered_dm_posts_nothing_in_the_admin_channel():
    channel = _Channel()
    user = _User()
    _run(_Bot(channel), user)
    assert len(user.received) == 1 and channel.sent == []


# --- a genuinely failed DM must still raise, so callers can react ---

def test_failed_dm_still_raises_and_posts_nothing():
    channel = _Channel()
    user = _User(fail=True)
    try:
        _run(_Bot(channel), user)
    except discord.Forbidden:
        pass
    else:
        raise AssertionError("a DM that could not be delivered must propagate")
    assert channel.sent == []


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
