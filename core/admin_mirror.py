"""Admin-channel mirroring for user-directed Plexbie DMs."""
from __future__ import annotations

import asyncio
from typing import Optional

import discord

from core.logging import get_logger
from core.discord_lookup import resolve_channel

logger = get_logger(__name__)


async def _get_admin_channel(bot, services):
    channel_id = getattr(services.config, "admin_channel_id", None)
    if not channel_id:
        logger.warning("ADMIN_CHANNEL_ID not configured - cannot mirror user DM to admin channel")
        return None

    try:
        return await resolve_channel(bot, channel_id)
    except Exception as e:
        logger.warning(f"Could not fetch admin channel {channel_id} for DM mirror: {e}")
        return None


def _copy_embed(embed: Optional[discord.Embed]) -> Optional[discord.Embed]:
    if embed is None:
        return None
    return discord.Embed.from_dict(embed.to_dict())


async def _send_admin_receipt(bot, services, *, header: str, content: Optional[str] = None, embed: Optional[discord.Embed] = None):
    """Mirror a DM to the admin channel. Never raises.

    Mirroring is an observability feature and must not be able to fail the
    operation it is reporting on. Previously an exception here propagated out of
    send_user_dm, so a failure to post the receipt was indistinguishable from the
    DM itself failing - and callers acted on that: user_invites.approve aborted
    after the Plex invite had already been sent and the role assigned.
    """
    try:
        channel = await _get_admin_channel(bot, services)
        if not channel:
            return

        message = header
        if content:
            message = f"{message}\n{content}"

        kwargs = {"content": message}
        copied = _copy_embed(embed)
        if copied is not None:
            kwargs["embed"] = copied

        await channel.send(**kwargs)
    except Exception as e:
        logger.warning(f"Could not mirror to the admin channel: {e}")


async def _log(user, *, context: str, content: Optional[str], embed, delivered: bool, error: Optional[str] = None,
               sent_by: Optional[str] = None):
    from core.message_log import embed_text, record
    await record(channel="discord", delivered=delivered, error=error, context=context, sent_by=sent_by,
                 discord_id=getattr(user, "id", None),
                 discord_name=getattr(user, "display_name", None) or getattr(user, "name", None) or str(user),
                 title=getattr(embed, "title", None) if embed is not None else None,
                 text="\n".join(x for x in (content or "", embed_text(embed)) if x))


_app_tasks: set = set()


async def _to_app(user, content: Optional[str], embed) -> None:
    """The same message on their phone, if they turned alerts on in the Plexbie app.
    Never raises: a DM's outcome mustn't depend on it."""
    try:
        from core import notify
        title = (getattr(embed, "title", None) if embed is not None else None) or "Plexbie"
        body = content or (getattr(embed, "description", None) if embed is not None else None) or ""
        await notify.push_app_to_discord(getattr(user, "id", None), title=notify.plain(str(title))[:120],
                                         body=notify.plain(str(body))[:300])
    except Exception as e:
        logger.info(f"App copy of a DM failed: {type(e).__name__}")


def _to_app_soon(user, content: Optional[str], embed) -> None:
    # In the background: the DM never waits on Expo's push service.
    task = asyncio.create_task(_to_app(user, content, embed))
    _app_tasks.add(task)
    task.add_done_callback(_app_tasks.discard)


async def send_user_dm(bot, services, user, *, context: str, content: Optional[str] = None,
                       embed: Optional[discord.Embed] = None, view=None, sent_by: Optional[str] = None,
                       own_fallback: bool = False):
    """DM someone and log it on Manage → Messages (delivered or not; the admin channel no
    longer gets a receipt for every DM). Raises if the DM didn't go. A copy goes to the
    Plexbie app on their phone either way, which helps most when their DMs are closed.
    `own_fallback`: the caller tells them another way when the DM doesn't go (a ticket
    reply), so the app copy goes only with a DM that arrived and they're never told twice."""
    if not own_fallback:
        _to_app_soon(user, content, embed)
    try:
        sent = await user.send(content=content, embed=embed, **({"view": view} if view is not None else {}))
    except Exception as e:
        await _log(user, context=context, content=content, embed=embed, delivered=False, sent_by=sent_by,
                   error="Their Discord DMs are closed" if isinstance(e, discord.Forbidden) else str(e))
        raise

    if own_fallback:
        _to_app_soon(user, content, embed)
    await _log(user, context=context, content=content, embed=embed, delivered=True, sent_by=sent_by)
    return sent


async def dm_user_id(bot, services, user_id, *, context: str, content: Optional[str] = None,
                     embed: Optional[discord.Embed] = None, view=None, sent_by: Optional[str] = None,
                     own_fallback: bool = False):
    """DM a Discord account by id: from the cache, else fetched. The sent message (truthy)
    if it went, else False; a closed DM or any failure is logged (on Manage → Messages),
    never raised."""
    if not (bot and user_id):
        return False
    try:
        uid = int(user_id)
        user = bot.get_user(uid) or await bot.fetch_user(uid)
        sent = await send_user_dm(bot, services, user, context=context, content=content, embed=embed, view=view,
                                  sent_by=sent_by, own_fallback=own_fallback)
        return sent or True
    except Exception as e:
        logger.info(f"Couldn't DM {user_id} ({context}): {e}")
        return False
