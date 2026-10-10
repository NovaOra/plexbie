# path: portal/ticket_view.py
"""Tickets from Discord: the member's DMs with Plexbie, and /ticket.

- The DM saying a request was approved carries "Something wrong? Open a ticket": pick
  what's wrong, add a line, and it's a ticket on Manage → Tickets, as if asked on the
  website. Once the request is on Plex, Plexbie edits that DM to take the button off.
- An admin's reply arrives as a DM with "Reply": the answer goes on the ticket's
  timeline (any ticket of theirs, by its id) and its owner (or every admin) is told.
- /ticket (plugins/media_requests) opens one that isn't on a request: about a film or
  show on Plex, picked from its own list as you type, or General. Like the website,
  it's for Plex members only (portal/auth decides who that is), then asks what's wrong.

Each button carries its request or ticket in its custom id (discord.py dynamic items),
so it keeps working after a restart. Who may press it is checked again on every press.
"""
import asyncio
import re
from typing import List, Optional

import discord
from discord import app_commands

from core.logging import get_logger
from portal.discord_actor import actor, said

logger = get_logger(__name__)

OPEN_ID = "plexbie:ticket:open:{key}"
REPLY_ID = "plexbie:ticket:reply:{hid}"

#: What a press says when the website refused without saying why.
NOT_SAID = "That didn't work. Try again in a moment, or use My requests on the website."


def open_view(key: str) -> discord.ui.View:
    """For the approval DM."""
    view = discord.ui.View(timeout=None)
    view.add_item(TicketOpenButton(str(key)))
    return view


def reply_view(hid: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(TicketReplyButton(hid))
    return view


def reply_embed(h: dict, text: str, by: Optional[str] = None, *, reply: bool = True) -> discord.Embed:
    """About your request: <title> (or your ticket on <title>, or just your ticket), then
    "Message from <admin>" and what they wrote."""
    from portal import help as helpdesk
    subject = helpdesk.subject_of(h)
    number = f"No. {int(h['slot']):04d} · " if h.get("slot") else ""
    heading = ("About your ticket" if subject == "general" else f"About your ticket on {helpdesk.named(h)}"
               if subject == "title" else f"About your request: {h.get('title') or 'your request'}")
    # Where else they can answer: a request's ticket is on My requests, any other on Home.
    page = "My requests" if subject == "request" else "Home, Your tickets"
    embed = discord.Embed(title=f"🛠️ {heading}"[:256],
                          description=(f"**Message from {discord.utils.escape_markdown(by)}**\n{text}" if by else text)[:4000],
                          color=discord.Color.from_rgb(255, 92, 147))
    embed.set_footer(text=f"{number}Reply with the button, or on the website: {page}" if reply else f"{number}On the website: {page}")
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
                "It's on Plex now. If something's wrong with it, use `/ticket`, or “Something wrong?” on the website.", ephemeral=True)
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
            out = await actions.ask_help(actor(interaction, admin=False), self.key, {"reason": self.reason, "note": str(self.note.value or ""), "source": "discord"})
        except Exception as e:
            return await interaction.response.send_message(said(e, NOT_SAID), ephemeral=True)
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
        actions = getattr(interaction.client, "portal_actions", None)
        if actions is None:
            return await interaction.response.send_message("Tickets need the website running. Ask an admin.", ephemeral=True)
        try:
            # By the ticket's id: any ticket of theirs, on a request, a title or general.
            out = await actions.ticket_reply(actor(interaction, admin=False), self.hid, {"text": str(self.text.value), "source": "discord"})
        except Exception as e:
            return await interaction.response.send_message(said(e, NOT_SAID), ephemeral=True)
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


async def mark_arrived(bot, tmdb_id, user_id=None, plex_id=None, media_type=None) -> int:
    """A requester was told their title is on Plex: their approved requests for it are
    marked arrived (so a stale "Open a ticket" press is refused) and the approval DM's
    button comes off. With `media_type`, a film never marks a show with the same TMDB
    id (or the other way round). Returns how many requests it marked."""
    from datetime import datetime, timezone
    from database.request_store import all_requests, set_fields
    marked = 0
    for key, rec in (await all_requests()).items():
        media = rec.get("media") or {}
        if str(media.get("id")) != str(tmdb_id) or rec.get("arrived_at") or rec.get("status") != "approved":
            continue
        kind = media.get("media_type") or rec.get("media_type")
        if media_type and kind and kind != media_type:
            continue
        mine = (user_id and str(rec.get("user_id") or "") == str(user_id)) or \
               (plex_id and str(rec.get("plex_account_id") or "") == str(plex_id))
        if not mine:
            continue
        await set_fields(int(key), arrived_at=datetime.now(timezone.utc).isoformat())
        await close_approval_dm(bot, rec, media.get("title") or media.get("name") or "It")
        marked += 1
    return marked


# ------------------------------------------------------------------ /ticket
#: /ticket's first choice: a ticket that isn't about one title.
GENERAL = "general"
#: How long the title list waits for Plex's list of films and shows (Discord allows 3
#: seconds). One still loading is there for the next letter typed.
LIST_WAIT = 2.0
#: Discord shows at most 25 choices.
MAX_CHOICES = 25
#: A picked title: a film or show by its TMDB id, or by Plex's ratingKey when Plex has no
#: TMDB match for it.
_PICKED = re.compile(r"^(?P<kind>movie|tv):(?:(?P<tmdb>\d{1,9})|plex:(?P<rk>\d{1,12}))$")

NOT_MEMBER = "Tickets are for people on the household's Plex. Ask to join it with `/join-plex`."
PICK_ONE = ("Pick a film or show from the list as you type: it lists what's on Plex. For anything else, "
            "pick **General**.")
SENT = "🛠️ Sent to the admins. You'll get a DM when they answer, and can reply to it there."
#: What /ticket says when the website refused without saying why.
NOT_OPENED = "That didn't work. Try again in a moment, or open a ticket on the website."


async def _plex_titles(actions) -> List[dict]:
    """Every film and show on Plex, newest first, from the website's own cached lists."""
    from portal import plex as plexdata

    async def one(kind: str) -> List[dict]:
        task = asyncio.ensure_future(actions.data._plex(plexdata.library, kind, key=f"plex:library:{kind}", ttl=600))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())   # a late failure isn't left unread
        try:
            return list(await asyncio.wait_for(asyncio.shield(task), LIST_WAIT))
        except Exception:
            return []
    movies, shows = await asyncio.gather(one("movie"), one("tv"))
    return [i for i in movies + shows if isinstance(i, dict) and i.get("title")]


def _choice(item: dict) -> app_commands.Choice:
    tmdb = str(item.get("tmdb") or "")
    value = f"{item['kind']}:{tmdb}" if tmdb.isdigit() else f"{item['kind']}:plex:{item.get('ratingKey')}"
    name = f"{item['title']}" + (f" ({item['year']})" if item.get("year") else "")
    return app_commands.Choice(name=f"{name} · {'Film' if item['kind'] == 'movie' else 'Show'}"[:100], value=value)


async def about_choices(interaction: discord.Interaction, current: str) -> List[app_commands.Choice]:
    """/ticket's `about` as it's typed: General first, then the films and shows on Plex
    with every word typed in their title (those starting with it first)."""
    out = [app_commands.Choice(name="General: not about one title", value=GENERAL)]
    actions = getattr(interaction.client, "portal_actions", None)
    if actions is None:
        return out
    words = (current or "").casefold().split()
    items = [i for i in await _plex_titles(actions) if all(w in i["title"].casefold() for w in words)]
    if words:
        typed = " ".join(words)
        items.sort(key=lambda i: (not i["title"].casefold().startswith(typed), i["title"].casefold()))
    return out + [_choice(i) for i in items[:MAX_CHOICES - 1]]


async def _target(actions, about: str) -> Optional[dict]:
    """What `about` names: {} for General, {kind, id} or {kind, ratingKey} for a title.
    Typed rather than picked, it's the one title on Plex with exactly that name. None:
    nothing on Plex, or more than one title, has it."""
    text = " ".join((about or "").split())
    if not text or text.casefold() in (GENERAL, "general: not about one title"):
        return {}
    picked = _PICKED.match(text)
    if picked:
        return {"kind": picked["kind"], "id": picked["tmdb"]} if picked["tmdb"] else {"kind": picked["kind"], "ratingKey": picked["rk"]}
    named = [i for i in await _plex_titles(actions) if i["title"].casefold() == text.casefold()]
    if len(named) != 1:
        return None
    tmdb = str(named[0].get("tmdb") or "")
    return {"kind": named[0]["kind"], **({"id": tmdb} if tmdb.isdigit() else {"ratingKey": str(named[0].get("ratingKey"))})}


async def start(interaction: discord.Interaction, about: str) -> None:
    """/ticket: who's asking, what about (it must be on Plex), then what's wrong."""
    actions = getattr(interaction.client, "portal_actions", None)
    auth = getattr(interaction.client, "portal_auth", None)
    if actions is None or auth is None:
        return await interaction.response.send_message("Tickets need the website running. Ask an admin.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await _ask(interaction, actions, auth, about)
    except Exception as e:
        logger.warning(f"/ticket failed for {interaction.user.id}: {type(e).__name__}: {e}")
        await interaction.followup.send(NOT_OPENED, ephemeral=True)


async def _ask(interaction: discord.Interaction, actions, auth, about: str) -> None:
    from aiohttp import web
    from portal import help as helpdesk
    u = interaction.user
    # The website's own view of them, as of now: roles, and the Plex account linked to them.
    person = await auth.describe({"id": str(u.id), "via": "discord", "name": getattr(u, "display_name", None) or u.name})
    if not person.get("member"):
        return await interaction.followup.send(NOT_MEMBER, ephemeral=True)
    target = await _target(actions, about)
    if target is None:
        return await interaction.followup.send(PICK_ONE, ephemeral=True)
    if target:
        try:
            found = await actions._on_plex(target["kind"], target.get("id", ""), target.get("ratingKey", ""))
        except web.HTTPException as e:
            return await interaction.followup.send(said(e, NOT_OPENED), ephemeral=True)
        except Exception as e:
            logger.info(f"/ticket couldn't check Plex for {target}: {type(e).__name__}: {e}")
            return await interaction.followup.send("Plexbie can't check Plex right now. Try again in a moment.", ephemeral=True)
        if await helpdesk.open_on_title(person, target["kind"], found["tmdb"], found["ratingKey"]):
            return await interaction.followup.send(
                "You already have a ticket open on this. Add to it with Reply on the admins' DM, or on the website.", ephemeral=True)
        question = f"What's wrong with **{discord.utils.escape_markdown(helpdesk.named(found))}**?"
    else:
        if await helpdesk.open_general(person) >= helpdesk.MAX_OPEN_GENERAL:
            return await interaction.followup.send(
                f"You have {helpdesk.MAX_OPEN_GENERAL} general tickets open already. Add to one of those.", ephemeral=True)
        question = "What's it about?"
    reasons = helpdesk.reasons_for("title" if target else "general", target.get("kind"))
    await interaction.followup.send(question, view=AboutPick(person, target, reasons, u.id), ephemeral=True)


class AboutPick(discord.ui.View):
    """Only the member who ran /ticket."""

    def __init__(self, person: dict, target: dict, reasons: dict, user_id: int):
        super().__init__(timeout=600)
        self.person, self.target, self.user_id = person, target, user_id
        from portal import help as helpdesk
        self.pick.options = [discord.SelectOption(label=label, value=code, description=helpdesk.REASON_HINTS.get(code))
                             for code, label in reasons.items()]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.select(placeholder="Pick what's wrong", min_values=1, max_values=1)
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(AboutModal(self.person, self.target, select.values[0]))


class AboutModal(discord.ui.Modal, title="Open a ticket"):
    def __init__(self, person: dict, target: dict, reason: str):
        super().__init__(timeout=900)
        self.person, self.target, self.reason = person, target, reason
        self.note = discord.ui.TextInput(label="What's wrong? (needed for “Something else”)" if reason == "other" else "What's wrong?",
                                         style=discord.TextStyle.paragraph, max_length=600, required=reason == "other",
                                         placeholder="It looks grey and washed out on my TV")
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction):
        actions = getattr(interaction.client, "portal_actions", None)
        if actions is None:
            return await interaction.response.send_message("Tickets need the website running. Ask an admin.", ephemeral=True)
        # Plexbie looks the file and their last play up first, which can take a few seconds.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await actions.open_ticket(self.person, {**self.target, "reason": self.reason, "note": str(self.note.value or ""),
                                                    "source": "discord"})
        except Exception as e:
            return await interaction.followup.send(said(e, NOT_OPENED), ephemeral=True)
        await interaction.followup.send(SENT, ephemeral=True)
