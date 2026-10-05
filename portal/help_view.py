# path: portal/help_view.py
"""The "Search by name" button on help requests Plexbie opens when a search by
ID found nothing (core/verified_search). It sits on the alert in the admin
channel; the help request's id is in the alert's footer, so the button keeps
working after a restart."""
import re
from typing import Optional

import discord

from core.logging import get_logger
from core.permissions import AdminActionView, single_flight

logger = get_logger(__name__)

CUSTOM_ID = "help_search_by_name"


def footer(hid: str) -> str:
    return f"Help {hid} · Search by name here, or on the website: Manage → Requests"


def help_id(message) -> Optional[str]:
    for embed in getattr(message, "embeds", None) or []:
        found = re.search(r"\bHelp ([0-9a-f]{12})\b", (embed.footer.text if embed.footer else "") or "")
        if found:
            return found.group(1)
    return None


class HelpByNameView(AdminActionView):
    """Admins only (AdminActionView), and one click at a time: it queues downloads."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Search by name", emoji="🔎", style=discord.ButtonStyle.primary, custom_id=CUSTOM_ID)
    @single_flight
    async def by_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        from portal import help as helpdesk
        hid = help_id(interaction.message)
        if not hid:
            return await interaction.response.send_message("This alert doesn't say which help request it is.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel

        async def tell(text: str) -> None:
            await channel.send(text)
        try:
            message = await helpdesk.name_search_for_help(interaction.client.services, hid,
                                                          interaction.user.display_name, tell=tell)
        except LookupError as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        button.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException as e:
            logger.info(f"Couldn't disable the Search by name button: {e}")
        await interaction.followup.send(message, ephemeral=True)
