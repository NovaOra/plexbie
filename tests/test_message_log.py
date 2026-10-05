# path: tests/test_message_log.py
"""Every message Plexbie sends a person is logged once, with how it got there."""
import asyncio
import pathlib
import tempfile
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401
import discord

from core import message_log


async def _init():
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{pathlib.Path(tempfile.mkdtemp()) / 'm.db'}")


class _User:
    def __init__(self, uid, name, closed=False):
        self.id, self.name, self.display_name, self.closed = uid, name, name, closed

    async def send(self, content=None, embed=None):
        if self.closed:
            class R:
                status, reason = 403, "Forbidden"
            raise discord.Forbidden(R(), "Cannot send messages to this user")


def test_dms_and_website_messages_are_kept_per_person():
    from core.admin_mirror import send_user_dm

    async def scenario():
        await _init()
        alex, marcus = _User(1, "Alex"), _User(2, "Marcus", closed=True)
        embed = discord.Embed(title="Request Approved!", description="**Dune** is on its way")
        embed.add_field(name="Next", value="Check <@1> your email")
        await send_user_dm(None, None, alex, context="approved", embed=embed)
        try:
            await send_user_dm(None, None, marcus, context="declined", content="Sorry")
        except discord.Forbidden:
            pass
        await message_log.record(channel="web", plex_name="grandpa_j", title="Ready", text="On Plex now")
        return await message_log.people(), await message_log.conversation("d1"), await message_log.conversation("d2")

    people, alex, marcus = asyncio.run(scenario())
    assert {p["name"]: (p["count"], p["failed"], p["via"]) for p in people} == {
        "Alex": (1, 0, ["discord"]), "Marcus": (1, 1, ["discord"]), "grandpa_j": (1, 0, ["web"])}
    assert alex[0]["title"] == "Request Approved!" and "Dune is on its way" in alex[0]["text"] and "<@" not in alex[0]["text"]
    assert marcus[0]["delivered"] is False and "closed" in marcus[0]["error"]


def test_old_messages_are_trimmed():
    async def scenario():
        await _init()
        await message_log.record(channel="discord", discord_id=1, text="new")
        later = datetime.now(timezone.utc) + timedelta(days=message_log.KEEP_DAYS + 1)
        dropped = await message_log.trim(now=later)
        return dropped, await message_log.people()

    dropped, people = asyncio.run(scenario())
    assert dropped == 1 and people == []


def test_what_people_send_plexbie_sits_in_the_same_conversation():
    from core.admin_mirror import send_user_dm

    class _Msg:
        def __init__(self, author, content, guild=None, files=()):
            self.author, self.content, self.guild = author, content, guild
            self.attachments = [type("A", (), {"filename": f})() for f in files]

    async def scenario():
        await _init()
        alex = _User(1, "Alex")
        await send_user_dm(None, None, alex, context="ticket reply", content="Is 4K OK?")
        await message_log.record_dm(_Msg(alex, "4K is great", files=["shot.png"]))
        await message_log.record_dm(_Msg(alex, "in a server", guild=object()))          # not a DM: not kept
        bot = _User(9, "Plexbie")
        bot.bot = True
        await message_log.record_dm(_Msg(bot, "from a bot"))                             # bots aren't kept
        await message_log.record_from_ticket({"who": "grandpa_j", "plex_name": "grandpa_j"}, text="Still stuck",
                                             title="Answer about Dune", source="web", context="ticket answer")
        return await message_log.people(), await message_log.conversation("d1"), await message_log.conversation("pgrandpa_j")

    people, alex, grandpa = asyncio.run(scenario())
    by = {p["name"]: p for p in people}
    assert (by["Alex"]["count"], by["Alex"]["received"]) == (1, 1) and by["Alex"]["last"]["direction"] == "in"
    assert [m["direction"] for m in alex] == ["out", "in"] and alex[1]["text"] == "4K is great\nAttached: shot.png"
    assert grandpa[0]["direction"] == "in" and grandpa[0]["channel"] == "web" and by["grandpa_j"]["received"] == 1


def test_nothing_plexbie_posts_pings_unless_it_says_so():
    """A member's ticket answer lands in the admin channel; '@everyone' in it mustn't ping."""
    import inspect
    import bot
    from portal import actions
    assert "allowed_mentions=discord.AllowedMentions.none()" in inspect.getsource(bot.Plexbie.__init__)
    src = inspect.getsource(actions.Actions._tell_admins_about_answer)
    assert "allowed_mentions=discord.AllowedMentions.none()" in src
