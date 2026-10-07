# path: portal/discord_actor.py
"""Buttons and boxes in Discord that do what the website does (tickets, the DM inbox)
call the same actions: these turn the presser into the person those actions expect,
and the website's refusal back into words for Discord."""
import json

import discord


def actor(interaction: discord.Interaction, *, admin: bool) -> dict:
    """The presser, as the website's actions know people."""
    u = interaction.user
    return {"user": {"id": str(u.id), "name": getattr(u, "display_name", None) or u.name, "via": "discord"},
            "member": True, "admin": admin, "discordId": str(u.id)}


def said(e: Exception, fallback: str) -> str:
    """The website's refusal, in its own words; `fallback` when it gave none."""
    try:
        return json.loads(getattr(e, "text", None)).get("error") or "That didn't work."
    except (TypeError, ValueError):
        return fallback
