# path: portal/inbox.py
"""Plexbie's DMs as a shared inbox for the admins, the way Modmail bots work.

- Someone DMs Plexbie: it's logged on Manage → Messages (core.message_log), the admins get
  a phone/browser alert, and it's posted in that person's thread under the admin channel.
  On their first DM in a while Plexbie answers that the admins have it (a setting).
- Admins answer from Manage → Messages (website or app), or in Discord with the thread's
  Reply button or /reply. Replies go as Plexbie, signed "— Name (admin)". Anything else
  typed in a thread stays between admins: Plexbie can't read it (no Message Content intent).
- "Add to their ticket" puts a DM on the person's open ticket as their answer; "Done"
  marks the conversation handled. Nothing is ever deleted from the history.

Threads live in the admin channel, which only admins can see, so an ordinary (public)
thread is private to them. Plexbie needs Create Public Threads and Send Messages in
Threads there; without them it posts in the admin channel itself (Health says so).
Every button and /reply re-checks that the presser is an admin.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands

from core.logging import get_logger
from core.permissions import deny, is_bot_admin
from database.kv_store import kv_get, kv_get_all, kv_set

logger = get_logger(__name__)

NAMESPACE = "dm_inbox"           # settings, and the last auto-reply/alert per person
THREADS = "dm_threads"           # person ("d<discord id>") -> thread id
AUTOREPLY_TEXT = "Thanks! The admins have your message and will reply here."
QUIET = timedelta(hours=12)      # no second auto-reply within this
ALERT_GAP = timedelta(minutes=10)  # one alert per person per burst of messages
EMAIL = re.compile(r"\s*[^@\s]+@[^@\s]+\.[^@\s]+\s*")   # /join-plex's email reply: that flow handles it
WHO = re.compile(r"[dp][\w .@+-]{1,120}")

NONE = discord.AllowedMentions.none()


def signed(text: str, admin: str) -> str:
    return f"{text.strip()}\n— {admin} (admin)"


async def settings() -> dict:
    s = await kv_get(NAMESPACE, "settings")
    return {"autoreply": True, **(s if isinstance(s, dict) else {})}


async def set_settings(**change) -> dict:
    s = {**(await settings()), **{k: v for k, v in change.items() if k in ("autoreply",)}}
    await kv_set(NAMESPACE, "settings", s)
    return s


async def _due(kind: str, who: str, gap: timedelta, now: datetime) -> bool:
    """True (and remembered) when `kind` wasn't done for `who` within `gap`."""
    key = f"{kind}:{who}"
    last = await kv_get(NAMESPACE, key)
    try:
        if last and now - datetime.fromisoformat(last) < gap:
            return False
    except (TypeError, ValueError):
        pass
    await kv_set(NAMESPACE, key, now.isoformat())
    return True


def _admin_channel(bot):
    from core.discord_lookup import admin_channel
    config = getattr(getattr(bot, "services", None), "config", None)
    return admin_channel(bot, config) if config else None


def threads_ok(bot) -> Optional[str]:
    """None when Plexbie can keep DM threads in the admin channel, else what's missing."""
    channel = _admin_channel(bot)
    guild = getattr(channel, "guild", None)
    if not channel or not guild or not guild.me:
        return None
    perms = channel.permissions_for(guild.me)
    missing = [n for n, ok in (("Create Public Threads", perms.create_public_threads),
                               ("Send Messages in Threads", perms.send_messages_in_threads)) if not ok]
    return ", ".join(missing) or None


async def thread_for(bot, who: str, name: str, *, create: bool = True):
    """The person's thread under the admin channel (made on first use), else the admin
    channel itself when threads aren't allowed, else None."""
    channel = _admin_channel(bot)
    if channel is None:
        return None
    tid = await kv_get(THREADS, who)
    if tid:
        try:
            thread = bot.get_channel(int(tid)) or await bot.fetch_channel(int(tid))
            if getattr(thread, "archived", False):
                await thread.edit(archived=False)
            return thread
        except Exception as e:
            logger.info(f"DM thread for {who} is gone ({type(e).__name__}); making a new one")
    if not create:
        return None
    if threads_ok(bot):
        return channel
    try:
        start = await channel.send(
            f"✉️ **{discord.utils.escape_markdown(name)}** messaged Plexbie. Their conversation is in this thread. "
            "Answer with **Reply** or `/reply`; anything else you type here stays between admins.",
            allowed_mentions=NONE)
        thread = await start.create_thread(name=f"✉️ {name}"[:100], auto_archive_duration=10080)
        await kv_set(THREADS, who, str(thread.id))
        return thread
    except discord.HTTPException as e:
        logger.warning(f"Couldn't make a DM thread for {who}: {e}")
        return channel


def view_for(who: str, key: Optional[str] = None, ticket: bool = False) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReplyButton(who))
    if key and ticket:
        view.add_item(TicketButton(key))
    view.add_item(DoneButton(who))
    return view


async def post(bot, who: str, name: str, text: str, *, view: Optional[discord.ui.View] = None) -> None:
    """A line in the person's thread. Never raises; never pings."""
    try:
        place = await thread_for(bot, who, name)
        if place is not None:
            await place.send(text[:2000], allowed_mentions=NONE, suppress_embeds=True, **({"view": view} if view else {}))
    except Exception as e:
        logger.info(f"Couldn't post in the DM thread for {who}: {type(e).__name__}: {e}")


async def open_ticket_for(ident: dict) -> Optional[dict]:
    """The person's most recently updated open ticket, if any."""
    from portal import help as helpdesk
    did, pid, pname = str(ident.get("discord_id") or ""), str(ident.get("plex_account_id") or ""), (ident.get("plex_name") or "").lower()
    best = None
    for hid, h in (await kv_get_all(helpdesk.NAMESPACE)).items():
        if not isinstance(h, dict) or h.get("status") != "open":
            continue
        if (did and str(h.get("discord_id") or "") == did) or (pid and str(h.get("plex_account_id") or "") == pid) \
                or (pname and (h.get("plex_name") or "").lower() == pname):
            thread = helpdesk.thread_of(h)
            at = (thread[-1].get("at") if thread else None) or h.get("created_at") or ""
            if best is None or at > best[0]:
                best = (at, {**h, "id": hid})
    return best[1] if best else None


async def on_dm(bot, message) -> None:
    """The on_message listener: log a DM to Plexbie, answer it once in a while, tell the admins."""
    from core import message_log
    key = await message_log.record_dm(message)
    if not key or EMAIL.fullmatch(message.content or ""):
        return
    author = message.author
    who, name = f"d{author.id}", getattr(author, "display_name", None) or author.name
    now = datetime.now(timezone.utc)
    try:
        if (await settings())["autoreply"] and await _due("autoreply", who, QUIET, now):
            from core.admin_mirror import send_user_dm
            await send_user_dm(bot, getattr(bot, "services", None), author, context="auto-reply", content=AUTOREPLY_TEXT)
    except Exception as e:
        logger.info(f"Auto-reply to {who} failed: {type(e).__name__}")
    ticket = await open_ticket_for({"discord_id": author.id})
    text = (await message_log.entry(key) or {}).get("text") or message.content or ""
    note = f"\n-# They have an open ticket on {ticket.get('title')}." if ticket else ""
    await post(bot, who, name, f"📨 **{discord.utils.escape_markdown(name)}**: {text}{note}",
               view=view_for(who, key, ticket=bool(ticket)))
    actions = getattr(bot, "portal_actions", None)
    if actions is not None and await _due("alert", who, ALERT_GAP, now):
        await actions.alert_admins_about_dm(who, name, text)


# ------------------------------------------------------------- buttons and /reply
def _admin(interaction: discord.Interaction) -> dict:
    """The admin pressing, as the website's actions know people."""
    u = interaction.user
    return {"user": {"id": str(u.id), "name": getattr(u, "display_name", None) or u.name, "via": "discord"},
            "member": True, "admin": True, "discordId": str(u.id)}


def _said(e: Exception) -> str:
    import json
    try:
        return json.loads(getattr(e, "text", "")).get("error") or "That didn't work."
    except (TypeError, ValueError):
        return "That didn't work. Try again from Manage → Messages."


async def reply_from_discord(interaction: discord.Interaction, who: str, text: str) -> None:
    actions = getattr(interaction.client, "portal_actions", None)
    if actions is None:
        return await interaction.response.send_message("Replies need the website running.", ephemeral=True)
    try:
        out = await actions.message_reply(_admin(interaction), who, {"text": text})
    except Exception as e:
        return await interaction.response.send_message(_said(e), ephemeral=True)
    await interaction.response.send_message(f"✅ {out.get('message') or 'Sent.'}", ephemeral=True)


class ReplyModal(discord.ui.Modal, title="Reply as Plexbie"):
    def __init__(self, who: str):
        super().__init__(timeout=900)
        self.who = who
        self.text = discord.ui.TextInput(label="Message (signed with your name)", style=discord.TextStyle.paragraph,
                                         max_length=1500, required=True)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction):
            return await deny(interaction)
        await reply_from_discord(interaction, self.who, str(self.text.value))


class ReplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"plexbie:dm:reply:(?P<who>d[0-9]{1,25})"):
    def __init__(self, who: str):
        super().__init__(discord.ui.Button(label="Reply", emoji="💬", style=discord.ButtonStyle.primary,
                                           custom_id=f"plexbie:dm:reply:{who}"))
        self.who = who

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["who"])

    async def callback(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction):
            return await deny(interaction)
        await interaction.response.send_modal(ReplyModal(self.who))


class TicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"plexbie:dm:ticket:(?P<key>[0-9T]{20,30}-[0-9a-f]{6})"):
    def __init__(self, key: str):
        super().__init__(discord.ui.Button(label="Add to their ticket", emoji="🛠️", style=discord.ButtonStyle.secondary,
                                           custom_id=f"plexbie:dm:ticket:{key}"))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction):
            return await deny(interaction)
        actions = getattr(interaction.client, "portal_actions", None)
        if actions is None:
            return await interaction.response.send_message("This needs the website running.", ephemeral=True)
        try:
            out = await actions.message_to_ticket(_admin(interaction), self.key)
        except Exception as e:
            return await interaction.response.send_message(_said(e), ephemeral=True)
        await interaction.response.send_message(f"🛠️ {out.get('message')}", ephemeral=True)


class DoneButton(discord.ui.DynamicItem[discord.ui.Button], template=r"plexbie:dm:done:(?P<who>d[0-9]{1,25})"):
    def __init__(self, who: str):
        super().__init__(discord.ui.Button(label="Done", emoji="✅", style=discord.ButtonStyle.secondary,
                                           custom_id=f"plexbie:dm:done:{who}"))
        self.who = who

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["who"])

    async def callback(self, interaction: discord.Interaction):
        if not is_bot_admin(interaction):
            return await deny(interaction)
        from core import message_log
        await message_log.mark_done(self.who, _admin(interaction)["user"]["name"])
        await interaction.response.send_message("✅ Marked done. It's still in Manage → Messages.", ephemeral=True)


@app_commands.command(name="reply", description="Reply as Plexbie to the person whose DM thread this is")
@app_commands.describe(message="What to send them (signed with your name)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)   # hidden from members; the admin role can be allowed in Integrations
async def reply_command(interaction: discord.Interaction, message: app_commands.Range[str, 1, 1500]):
    if not is_bot_admin(interaction):
        return await deny(interaction)
    channel_id = str(getattr(interaction.channel, "id", ""))
    who = next((w for w, tid in (await kv_get_all(THREADS)).items() if str(tid) == channel_id), None)
    if not who:
        return await interaction.response.send_message("Use /reply inside someone's ✉️ DM thread in the admin channel.",
                                                       ephemeral=True)
    await reply_from_discord(interaction, who, message)
