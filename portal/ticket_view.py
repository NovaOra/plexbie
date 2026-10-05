# path: portal/ticket_view.py
"""Tickets from Discord, all in the member's DMs with Plexbie.

- The DM saying a request was approved carries "Something wrong? Open a ticket": pick
  what's wrong, add a line, and it's a ticket on Manage → Tickets, as if asked on the
  website. Once the request is on Plex, Plexbie edits that DM to take the button off.
- An admin's reply arrives as a DM with "Reply": the answer goes on the ticket's
  timeline and its owner (or every admin) is told.

Each button carries its request or ticket in its custom id (discord.py dynamic items),
so it keeps working after a restart. Who may press it is checked again on every press.
"""
import json
from typing import Optional

import discord

from core.logging import get_logger

logger = get_logger(__name__)

OPEN_ID = "plexbie:ticket:open:{key}"
REPLY_ID = "plexbie:ticket:reply:{hid}"


def _person(interaction: discord.Interaction) -> dict:
    """The presser, as the website's actions know people."""
    u = interaction.user
    return {"user": {"id": str(u.id), "name": getattr(u, "display_name", None) or u.name, "via": "discord"},
            "member": True, "admin": False, "discordId": str(u.id)}


def _said(e: Exception) -> str:
    """The website's refusal, in its own words."""
    text = getattr(e, "text", None)
    try:
        return json.loads(text).get("error") or "That didn't work."
    except (TypeError, ValueError):
        return "That didn't work. Try again in a moment, or use My requests on the website."


def open_view(key: str) -> discord.ui.View:
    """For the approval DM."""
    view = discord.ui.View(timeout=None)
    view.add_item(TicketOpenButton(str(key)))
    return view


def reply_view(hid: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TicketReplyButton(hid))
    return view


def reply_embed(h: dict, text: str) -> discord.Embed:
    number = f"No. {int(h['slot']):04d} · " if h.get("slot") else ""
    embed = discord.Embed(title=f"🛠️ About your request: {h.get('title') or 'your request'}", description=text[:4000],
                          color=discord.Color.from_rgb(255, 92, 147))
    embed.set_footer(text=f"{number}Reply with the button, or on the website: My requests")
    return embed


class TicketOpenButton(discord.ui.DynamicItem[discord.ui.Button], template=r"plexbie:ticket:open:(?P<key>[0-9]+)"):
    def __init__(self, key: str):
        super().__init__(discord.ui.Button(label="Something wrong? Open a ticket", emoji="🛠️",
                                           style=discord.ButtonStyle.secondary, custom_id=OPEN_ID.format(key=key)))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["key"])

    async def callback(self, interaction: discord.Interaction):
        from database.request_store import get_request
        from portal import help as helpdesk
        rec = await get_request(int(self.key))
        if not rec or str(rec.get("user_id") or "") != str(interaction.user.id):
            return await interaction.response.send_message("That button isn't for you.", ephemeral=True)
        if rec.get("arrived_at"):
            return await interaction.response.send_message(
                "It's on Plex now. If something's wrong with it, use “Something wrong?” on the website.", ephemeral=True)
        if await helpdesk.open_for({self.key}):
            return await interaction.response.send_message(
                "You already have a ticket on this one. The admins will reply here; answer with the Reply button.", ephemeral=True)
        media = rec.get("media") or {}
        title = media.get("title") or media.get("name") or "your request"
        await interaction.response.send_message(f"What's wrong with **{title}**?", view=ReasonPick(self.key, interaction.user.id),
                                                ephemeral=True)


class ReasonPick(discord.ui.View):
    """Only the member who pressed "Open a ticket" (it's their request)."""

    def __init__(self, key: str, user_id: int):
        super().__init__(timeout=600)
        self.key, self.user_id = key, user_id
        from portal import help as helpdesk
        self.pick.options = [discord.SelectOption(label=label, value=code) for code, label in helpdesk.REASONS.items()]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.select(placeholder="Pick what's wrong", min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(TicketModal(self.key, select.values[0]))


class TicketModal(discord.ui.Modal, title="Open a ticket"):
    def __init__(self, key: str, reason: str):
        super().__init__(timeout=900)
        self.key, self.reason = key, reason
        self.note = discord.ui.TextInput(label="Anything else? (needed for “Something else”)" if reason == "other" else "Anything else?",
                                         style=discord.TextStyle.paragraph, max_length=600, required=reason == "other",
                                         placeholder="It's been at 0% since this morning")
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        actions = getattr(interaction.client, "portal_actions", None)
        if actions is None:
            return await interaction.response.send_message("Tickets need the website running. Ask an admin.", ephemeral=True)
        try:
            out = await actions.ask_help(_person(interaction), self.key, {"reason": self.reason, "note": str(self.note.value or ""), "source": "discord"})
        except Exception as e:
            return await interaction.response.send_message(_said(e), ephemeral=True)
        await interaction.response.send_message(f"🛠️ {out.get('message') or 'Sent.'} You'll hear back here.", ephemeral=True)


class TicketReplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"plexbie:ticket:reply:(?P<hid>[0-9a-f]{12})"):
    def __init__(self, hid: str):
        super().__init__(discord.ui.Button(label="Reply", emoji="💬", style=discord.ButtonStyle.primary,
                                           custom_id=REPLY_ID.format(hid=hid)))
        self.hid = hid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["hid"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReplyModal(self.hid))


class ReplyModal(discord.ui.Modal, title="Your answer"):
    def __init__(self, hid: str):
        super().__init__(timeout=900)
        self.hid = hid
        self.text = discord.ui.TextInput(label="Answer", style=discord.TextStyle.paragraph, max_length=1200, required=True)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction):
        from database.kv_store import kv_get
        from portal import help as helpdesk
        actions = getattr(interaction.client, "portal_actions", None)
        h: Optional[dict] = await kv_get(helpdesk.NAMESPACE, self.hid)
        if actions is None or not isinstance(h, dict):
            return await interaction.response.send_message("That ticket can't be found.", ephemeral=True)
        try:
            out = await actions.member_reply(_person(interaction), h.get("request") or "", {"text": str(self.text.value), "source": "discord"})
        except Exception as e:
            return await interaction.response.send_message(_said(e), ephemeral=True)
        await interaction.response.send_message(f"💬 {out.get('message') or 'Sent.'}", ephemeral=True)


async def close_approval_dm(bot, rec: dict, title: str) -> None:
    """The request is on Plex: the approval DM loses its ticket button (and says so)."""
    where = rec.get("approval_dm") or {}
    if not (bot and where.get("channel") and where.get("message")):
        return
    try:
        channel = bot.get_channel(int(where["channel"])) or await bot.fetch_channel(int(where["channel"]))
        # Its text was kept when it was sent: no fetch just to edit it.
        await channel.get_partial_message(int(where["message"])).edit(
            content=f"{where.get('text') or ''}\n🎬 **{title}** is on Plex now.".strip(), view=None)
    except Exception as e:
        logger.info(f"Couldn't take the ticket button off the approval DM for {title}: {e}")


async def mark_arrived(bot, tmdb_id, user_id=None, plex_id=None) -> int:
    """A requester was told their title is on Plex: their approved requests for it are
    marked arrived (so a stale "Open a ticket" press is refused) and the approval DM's
    button comes off. Returns how many requests it marked."""
    from datetime import datetime, timezone
    from database.request_store import all_requests, set_fields
    marked = 0
    for key, rec in (await all_requests()).items():
        media = rec.get("media") or {}
        if str(media.get("id")) != str(tmdb_id) or rec.get("arrived_at") or rec.get("status") != "approved":
            continue
        mine = (user_id and str(rec.get("user_id") or "") == str(user_id)) or \
               (plex_id and str(rec.get("plex_account_id") or "") == str(plex_id))
        if not mine:
            continue
        await set_fields(int(key), arrived_at=datetime.now(timezone.utc).isoformat())
        await close_approval_dm(bot, rec, media.get("title") or media.get("name") or "It")
        marked += 1
    return marked
