# path: plugins/user_invites/cog.py
"""User invitation system matching original Python bot workflow"""
from database.kv_store import kv_get, kv_get_all, kv_set, kv_set_many, kv_update
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.blocking import run_blocking
from core.plex_account import can_sign_in, owner_account
from core.logging import get_logger
from core.permissions import AdminActionView, single_flight
from core.services import BotServices
from core.admin_mirror import send_user_dm
from core.discord_lookup import admin_channel as find_admin_channel, home_guild

logger = get_logger(__name__)

# Storage file
# INVITES_FILE removed - now using database kv_store
INVITES_NAMESPACE = "plex_invites"
# Admin message id -> the request it represents, so a persistent view can recover
# its state after a restart. Deliberately a SEPARATE namespace: user_mgmt's
# auto_link_users iterates plex_invites and reads every key as a Discord user id,
# so message ids must never land there.
INVITE_MESSAGES_NAMESPACE = "plex_invite_messages"
#: Join requests from people who signed in on the website with Plex and have no
#: Discord account, keyed by Plex account id (portal.auth reads it too).
WEB_JOINS_NAMESPACE = "web_plex_joins"


def share_is_active(account, email: str) -> bool:
    """Blocking: whether plex.tv already shares with `email` (no invitation waiting
    to be accepted), rather than having sent them an invite."""
    email = (email or "").strip().lower()
    if any((getattr(i, "email", "") or "").lower() == email
           for i in account.pendingInvites(includeSent=True, includeReceived=False)):
        return False
    for user in account.users():
        if (user.email or "").lower() == email:
            return any(not getattr(s, "pending", False) for s in getattr(user, "servers", None) or [])
    return False


class PlexInviteApprovalView(AdminActionView):
    """Admin approval buttons for Plex invites.

    Admin-gated: approving grants real Plex library access via inviteFriend and
    assigns the Plex member role, so it must never dispatch to a non-admin.
    """
    def __init__(
        self,
        user_id: int = None,
        email: str = None,
        services: BotServices = None,
    ):
        # Every argument is optional so setup() can register this view with no
        # arguments (bot.add_view(PlexInviteApprovalView())). State is recovered
        # per-interaction by _ensure_loaded().
        super().__init__(timeout=None)
        self.user_id = user_id
        self.email = email
        self.services = services
        # Set for someone who asked on the website by signing in with Plex: their
        # real Plex name, and no Discord account (user_id stays None).
        self.plex_name = None
        self.plex_account_id = None
        #: Set by _send_plex_invite: plex.tv shared straight away (someone coming
        #: back, say), so there's no invitation email to wait for.
        self.already_on = False
        #: The Plex account id behind the email, when plex.tv knows it (it may not,
        #: for someone who hasn't made a Plex account yet).
        self.found_plex_id = None

    async def _load(self, message_id: int, bot) -> bool:
        """Restore the request behind an admin message. True when it can be acted on."""
        if self.email and self.services:
            return True
        record = await kv_get(INVITE_MESSAGES_NAMESPACE, str(message_id))
        if not record:
            logger.error(f"No stored invite request for message {message_id}")
            return False
        self.user_id = record.get("user_id")
        self.email = record.get("email")
        self.plex_name = record.get("plex_name")
        self.plex_account_id = record.get("plex_account_id")
        self.services = bot.services
        return bool(self.email)

    async def _ensure_loaded(self, interaction: discord.Interaction) -> bool:
        """Populate state from storage when this view came from a restart.

        A persistent view registered at startup has no per-request state, so it
        is looked up by the admin message id. Previously this view was never
        registered at all and its buttons carried no custom_id, so after any
        restart - including every deploy - clicking Approve returned "This
        interaction failed" and the request became silently unactionable.
        """
        if await self._load(interaction.message.id, interaction.client):
            return True
        await interaction.followup.send(
            "❌ Could not find this request's data. Ask the user to run "
            "`/join-plex` again.",
            ephemeral=True,
        )
        return False

    async def _status(self) -> Optional[str]:
        """The stored status of this request: pending, approved or denied."""
        if self.user_id:
            record = await kv_get(INVITES_NAMESPACE, str(self.user_id))
        elif self.plex_account_id:
            record = await kv_get(WEB_JOINS_NAMESPACE, str(self.plex_account_id))
        else:
            record = None
        return (record or {}).get("status")

    async def _set_status(self, status: str) -> None:
        if self.user_id:
            await kv_update(INVITES_NAMESPACE, str(self.user_id), status=status)
        elif self.plex_account_id:
            await kv_update(WEB_JOINS_NAMESPACE, str(self.plex_account_id), status=status)

    async def _approve_core(self, bot, guild) -> Dict[str, Any]:
        """Invite to Plex, give the member role, start tracking, tell them.

        Shared by the Discord button and the website. Discord steps are skipped
        for someone who asked by signing in with Plex and has no Discord account.
        """
        success, plex_username = await self._send_plex_invite()
        if not success:
            return {"ok": False, "message": "Failed to send Plex invite. Please check logs."}
        if self.plex_name:
            plex_username = self.plex_name   # known for certain, not guessed from the email

        member = guild.get_member(self.user_id) if (guild and self.user_id) else None
        warning = None
        if member and self.services.config.plex_member_role_id:
            role = guild.get_role(int(self.services.config.plex_member_role_id))
            if role:
                # The invite is out already: a role Discord won't let us give must not
                # stop the rest (tracking, "approved", telling them).
                try:
                    await member.add_roles(role, reason="Plex access approved")
                    logger.info(f"Added Plex member role to {member.name}")
                except discord.HTTPException as e:
                    from core.role_order import fix_text
                    warning = (f"The Plex invite went out, but Discord wouldn't let Plexbie give "
                               f"{member.display_name} the {role.name} role. "
                               + fix_text([role.name], guild.me.display_name if guild.me else "Plexbie")
                               + f" Then give them {role.name} by hand.")
                    logger.warning(f"Could not give {role.name} to {member.name}: {e}")

        # Add to user tracking system. Without a resolvable member we cannot
        # record the Discord side of the mapping, and a silent miss here means
        # the user keeps Plex access forever without inactivity tracking.
        plex_id = self.found_plex_id or self.plex_account_id
        if plex_username and member:
            note = await self._add_to_user_tracking(member, plex_username, plex_id)
            warning = " ".join(w for w in (warning, note) if w) or None
        elif plex_username and not self.user_id:
            note = await self._add_to_user_tracking(None, plex_username, plex_id)
            warning = " ".join(w for w in (warning, note) if w) or None
        elif plex_username:
            logger.error(
                f"Invited {self.email} to Plex but Discord member {self.user_id} "
                f"is not in the guild - NOT tracked for inactivity, link manually"
            )

        await self._set_status("approved")

        user = bot.get_user(self.user_id) if self.user_id else None
        if user:
            if self.already_on:
                embed = discord.Embed(
                    title="✅ You're on Plex!",
                    description=f"Your request has been approved, and your Plex account (**{self.email}**) "
                                f"already has the libraries. There's no invitation to accept: open Plex and start watching.",
                    color=discord.Color.green()
                )
            else:
                embed = discord.Embed(
                    title="✅ Request Approved!",
                    description=f"Your request has been approved! A Plex invitation has been sent to **{self.email}**.\n\n"
                               f"Check your email and accept the invitation to access the Plex server.",
                    color=discord.Color.green()
                )
                embed.add_field(
                    name="Next Steps",
                    value="1. Check your email for the Plex invitation\n"
                          "2. Click the link and create/login to your Plex account\n"
                          "3. Start watching!",
                    inline=False
                )
            await send_user_dm(bot, self.services, user, context=f"join-plex approved ({self.email})", embed=embed)
        summary = (f"Plex access is on for {self.email} straight away (no invitation to accept)."
                   if self.already_on else f"Plex invitation sent to {self.email}.")
        return {"ok": True, "message": summary + (f" {warning}" if warning else ""), "warning": warning,
                "summary": summary}

    async def _deny_core(self, bot) -> None:
        await self._set_status("denied")
        user = bot.get_user(self.user_id) if self.user_id else None
        if user:
            embed = discord.Embed(
                title="❌ Request Denied",
                description="Sorry, your request for Plex access was not approved at this time.",
                color=discord.Color.red()
            )
            await send_user_dm(bot, self.services, user, context=f"join-plex denied ({self.email})", embed=embed)

    @discord.ui.button(
        label="Approve & Send Invite",
        style=discord.ButtonStyle.success,
        custom_id="plex_invite_approve",
    )
    @single_flight
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if not await self._ensure_loaded(interaction):
            return

        result = await self._approve_core(interaction.client, interaction.guild)
        if result["ok"]:
            await interaction.message.edit(
                content=f"✅ **Approved by {interaction.user.name}** - {result['summary']}",
                view=None
            )
            await interaction.followup.send(result.get("warning") or "Invite sent successfully!", ephemeral=True)
        else:
            await interaction.followup.send(result["message"], ephemeral=True)

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        custom_id="plex_invite_deny",
    )
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if not await self._ensure_loaded(interaction):
            return

        await interaction.message.edit(
            content=f"❌ **Denied by {interaction.user.name}**",
            view=None
        )
        await self._deny_core(interaction.client)

    async def _send_plex_invite(self) -> tuple[bool, str]:
        """Send Plex invitation - returns (success, plex_username)"""
        if not self.services.plex_server:
            logger.error("Plex server not configured")
            return False, ""

        try:
            if not can_sign_in(self.services.config):
                logger.error("Can't invite: no plex.tv sign-in (PLEX_TOKEN or PLEX_USERNAME/PLEX_PASSWORD)")
                return False, ""

            # Signing in to plex.tv and inviteFriend() are both round-trips and
            # blocking; this runs from a button callback where a stalled loop
            # delays every other interaction.
            account = await run_blocking(owner_account, self.services.config)

            sections = await run_blocking(self.services.plex_server.library.sections)
            await run_blocking(
                account.inviteFriend,
                user=self.email,
                server=self.services.plex_server,
                sections=sections,
                allowSync=False,
            )

            # Their real Plex account if the email has one; otherwise a placeholder
            # name from the email until they accept (reconcile then fills it in).
            plex_username = self.email.split('@')[0]
            try:
                found = await run_blocking(account.user, self.email)
                self.found_plex_id = getattr(found, "id", None)
                plex_username = getattr(found, "username", None) or getattr(found, "title", None) or plex_username
            except Exception:
                pass
            # plex.tv sometimes shares at once rather than inviting (someone
            # removed and coming back): then there's no email to tell them about.
            try:
                self.already_on = await run_blocking(share_is_active, account, self.email)
            except Exception as e:
                logger.info(f"Couldn't tell whether {self.email}'s share is waiting on an invite: {type(e).__name__}")
                self.already_on = False

            logger.info(f"Successfully sent Plex invite to {self.email}")
            return True, plex_username

        except Exception as e:
            message = str(e)
            if "already sharing this server with" in message:
                try:
                    # account.user() is another plex.tv lookup. account may be
                    # unbound if MyPlexAccount() itself raised, which the outer
                    # except also covers.
                    existing_user = await run_blocking(account.user, self.email)
                except Exception:
                    existing_user = None

                plex_username = (
                    getattr(existing_user, "username", None)
                    or getattr(existing_user, "title", None)
                    or self.email.split('@')[0]
                )
                self.found_plex_id = getattr(existing_user, "id", None)
                logger.info(
                    f"Plex access already exists for {self.email}; restoring Discord access for {plex_username}"
                )
                self.already_on = True
                return True, plex_username

            logger.error(f"Failed to send Plex invite: {e}")
            return False, ""

    async def _add_to_user_tracking(self, member: Optional[discord.Member], plex_username: str,
                                    plex_id=None) -> Optional[str]:
        """Track this person for inactivity (member is None for Plex-only joiners).

        Matched by Plex account id, then by the email, then by their own Discord
        row - never by name: the email someone types isn't verified, and a name
        guessed from it ("jane@...") can be someone else's. A row already linked to
        a different Discord member is never re-linked; the admin is told instead.
        Returns that note, or None.
        """
        try:
            from sqlalchemy import select
            from database.session import get_session
            from plugins.user_mgmt.models import PlexUser

            email = (self.email or "").strip().lower()
            plex_id = int(plex_id) if plex_id and str(plex_id).isdigit() else None
            async with get_session() as session:
                rows = (await session.execute(select(PlexUser))).scalars().all()
                by_id = next((r for r in rows if plex_id and r.plex_user_id == plex_id), None)
                by_email = next((r for r in rows if email and (r.plex_email or "").lower() == email), None)
                mine = next((r for r in rows if member and r.discord_id == member.id), None)
                row = by_id or by_email or mine
                note = None
                if row is None:
                    taken = {r.plex_username.lower() for r in rows}
                    name = plex_username if plex_username.lower() not in taken else (email or plex_username)
                    if name.lower() in taken:
                        return (f"Plexbie couldn't start tracking {self.email}: the name {plex_username} belongs "
                                f"to someone else. Match them on the People page.")
                    session.add(PlexUser(discord_id=member.id if member else None,
                                         discord_username=str(member) if member else None,
                                         plex_username=name, plex_email=email or None, plex_user_id=plex_id))
                    logger.info(f"Started tracking new user {name}")
                else:
                    if member and row.discord_id and row.discord_id != member.id:
                        note = (f"{row.plex_username} is already linked to another Discord member, so Plexbie "
                                f"didn't link {member.display_name} to it. Check the People page.")
                        logger.warning(f"Not re-linking {row.plex_username} (Discord {row.discord_id}) to {member.id}")
                    elif member and mine is not None and mine is not row:
                        note = (f"{member.display_name} is already tracked as {mine.plex_username}; Plexbie didn't "
                                f"move them to {row.plex_username}. Check the People page.")
                    elif member:
                        row.discord_id, row.discord_username = member.id, str(member)
                    if plex_id and not row.plex_user_id and by_id is None:
                        row.plex_user_id = plex_id
                    if email and not row.plex_email:
                        row.plex_email = email
                    logger.info(f"Updated tracking for existing user {row.plex_username}")
                await session.commit()
                return note
        except Exception as e:
            logger.error(f"Error adding user to tracking: {e}", exc_info=True)
            return None


class UserInvitesCog(commands.Cog):
    """Plex user invitation system with email collection via DM"""
    
    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        # Discord ids with a /join-plex flow currently awaiting a DM reply.
        self._in_progress: set = set()

    async def cog_load(self):
        """Register the persistent approval view.

        Must be here, not in setup(): core.plugin_manager instantiates the cog
        class and calls bot.add_cog directly, so a module-level setup() is never
        invoked by this bot. add_cog does trigger cog_load, and it also fires if
        the plugin is ever loaded as a normal discord.py extension.
        """
        self.bot.add_view(PlexInviteApprovalView())
        logger.info("✅ Registered persistent Plex invite approval view")
        
    
    @app_commands.command(name="join-plex", description="Request access to the Plex server")
    @app_commands.guild_only()
    async def join_plex(self, interaction: discord.Interaction):
        """Request Plex access with email collection via DM"""
        logger.info(f"User {interaction.user.name} ({interaction.user.id}) invoked /join-plex")

        # Check if user already has role
        if self.services.config.plex_member_role_id:
            member = interaction.guild.get_member(interaction.user.id)
            role = interaction.guild.get_role(int(self.services.config.plex_member_role_id))
            if member and role and role in member.roles:
                await interaction.response.send_message(
                    "You already have access to the Plex server!",
                    ephemeral=True
                )
                return
        
        # One flow per user at a time. The wait_for check below matches any DM from
        # this user, so two concurrent flows would both be resolved by a single
        # reply - producing two admin approval messages, two saved requests, and
        # potentially two Plex invites for one request.
        if interaction.user.id in self._in_progress:
            await interaction.response.send_message(
                "You already have a request in progress - check your DMs and reply "
                "there with your email address.",
                ephemeral=True,
            )
            return

        # Defer rather than replying now: the DM is attempted first, so the reply
        # can tell the truth about whether it actually arrived. Previously the user
        # was told "Check your DMs!" before any DM was sent, and a blocked DM was
        # only logged - leaving them waiting for a message that never came.
        await interaction.response.defer(ephemeral=True)
        self._in_progress.add(interaction.user.id)

        try:
            # Send DM requesting email
            embed = discord.Embed(
                title="Plex Server Access Request",
                description="To request access to the Plex server, please reply with your email address.\n\n"
                           "**This email will be used to send you a Plex invitation.**",
                color=discord.Color.blue()
            )
            embed.add_field(
                name="Important",
                value="Use the same email associated with your Plex account "
                      "(or the one you want to use for Plex).",
                inline=False
            )
            
            await send_user_dm(self.bot, self.services, interaction.user, context="join-plex email collection prompt", embed=embed)

            # The DM is confirmed sent, so this is now truthful.
            await interaction.followup.send("Check your DMs!", ephemeral=True)
            
            # Wait for email response
            def check(m):
                return m.author.id == interaction.user.id and isinstance(m.channel, discord.DMChannel)
            
            try:
                msg = await self.bot.wait_for('message', timeout=300, check=check)
            except asyncio.TimeoutError:
                await send_user_dm(self.bot, self.services, interaction.user, context="join-plex request timed out", content="Request timed out. Please use `/join-plex` again to start over.")
                return
            
            email = msg.content.strip()

            # Enhanced email validation
            from utils.validators import validate_email
            if not validate_email(email):
                await send_user_dm(self.bot, self.services, interaction.user, context="join-plex invalid email", content="That doesn't look like a valid email address. Please use `/join-plex` again and provide a valid email.")
                return
            
            await send_user_dm(self.bot, self.services, interaction.user, context="join-plex request submitted", content="Email received! Your request has been sent to the admin for approval.")
            
            # Save the request BEFORE notifying admins. _send_to_admin records the
            # message id against this record, and previously ran first - so its
            # kv_get found nothing, the message id was silently dropped, and
            # _save_request then overwrote the record without it.
            await self._save_request(interaction.user.id, email)

            # Send to admin channel
            await self._send_to_admin(interaction, email)
            
        except discord.Forbidden:
            logger.warning(f"Could not DM user {interaction.user.id} for /join-plex")
            await interaction.followup.send(
                "❌ I could not send you a DM. Enable **Allow direct messages from "
                "server members** in your Privacy Settings for this server, then "
                "run `/join-plex` again.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Join-plex error for user {interaction.user.id}: {e}", exc_info=True)
            try:
                await interaction.followup.send(
                    "❌ Something went wrong starting your request. Please try again.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
        finally:
            # Always release the guard, or the user could never retry.
            self._in_progress.discard(interaction.user.id)
    
    async def _send_to_admin(self, interaction: discord.Interaction, email: str):
        """Send invite request to admin channel"""
        await post_join_request(self.bot, self.services, interaction.user, email)

    async def _save_request(self, user_id: int, email: str):
        """Save invite request"""
        await save_join_request(user_id, email)


async def save_join_request(user_id: int, email: str) -> None:
    """Record that a member asked to join Plex. Shared by /join-plex and the website."""
    await kv_set(INVITES_NAMESPACE, str(user_id), {
        "email": email,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "pending"
    })


async def post_join_request(bot, services, user, email: str, *, plex_name: Optional[str] = None,
                            plex_account_id: Optional[str] = None) -> Optional[int]:
    """Post the Approve/Deny card for a Plex access request.

    Shared by /join-plex and the website, so an admin sees one kind of join
    request. `user` is None for someone who asked on the website by signing in
    with Plex; then plex_name/plex_account_id identify them. Returns the approval
    message id, or None when the admin channel is unset or invisible (logged).
    """
    if not services.config.admin_channel_id:
        logger.error("Admin channel not configured")
        return None

    admin_channel = find_admin_channel(bot, services.config)
    if not admin_channel:
        logger.error(f"Admin channel {services.config.admin_channel_id} not found")
        return None

    who = user.name if user else f"{plex_name} (signed in with Plex)"
    embed = discord.Embed(
        title="New Plex Access Request",
        description=f"**{who}** has requested access to the Plex server.",
        color=discord.Color.blue()
    )
    if user:
        embed.add_field(name="Discord User", value=user.mention, inline=True)
    else:
        embed.add_field(name="Plex account", value=plex_name or "unknown", inline=True)
    embed.add_field(name="Email Address", value=email, inline=True)
    embed.timestamp = datetime.now(timezone.utc)

    view = PlexInviteApprovalView(user.id if user else None, email, services)
    view.plex_name, view.plex_account_id = plex_name, plex_account_id

    message = await admin_channel.send(embed=embed, view=view)

    # Record the message -> request mapping so the persistent view can recover
    # its state after a restart. Written unconditionally rather than as an
    # update to an existing record, which is what previously failed silently.
    await kv_set(INVITE_MESSAGES_NAMESPACE, str(message.id), {
        "user_id": user.id if user else None,
        "email": email,
        "plex_name": plex_name,
        "plex_account_id": plex_account_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    # Update saved request with message ID
    namespace, key = (INVITES_NAMESPACE, str(user.id)) if user else (WEB_JOINS_NAMESPACE, str(plex_account_id))
    await kv_update(namespace, key, message_id=message.id)
    from core import notify
    name = (getattr(user, "display_name", None) or user.name) if user else plex_name or "Someone"
    notify.alert_admins_soon(bot, services.config, title=f"{name} wants to join Plex", body="Approve or decline it on Manage.",
                             url="/manage?tab=joins", tag=f"join-{message.id}")
    return message.id


async def join_with_invite_link(bot, services, *, plex_name: str, plex_account_id: str, email: str,
                                user_token: str, label: str, created_by: str) -> Dict[str, Any]:
    """Share the server with someone who opened an admin's invite link.

    The admin approved them by making the link, so this goes straight to
    inviteFriend, then uses the person's own sign-in (held only for the length
    of this call, never stored) to accept the share, so they can watch at once
    instead of waiting for Plex's email. If accepting fails they still have the
    invite; Plex emails it as usual. Returns {"ok", "accepted"}.
    """
    from plexapi.myplex import MyPlexAccount

    cfg = services.config
    if not (can_sign_in(cfg) and services.plex_server):
        logger.error("Invite link used but Plexbie can't sign in to plex.tv or reach Plex")
        return {"ok": False, "accepted": False}
    try:
        owner = await run_blocking(owner_account, cfg)
        sections = await run_blocking(services.plex_server.library.sections)
        await run_blocking(owner.inviteFriend, user=plex_name, server=services.plex_server,
                           sections=sections, allowSync=False)
    except Exception as e:
        logger.error(f"Invite link: could not share Plex with {plex_name}: {type(e).__name__}: {e}")
        return {"ok": False, "accepted": False}

    accepted = False
    try:
        guest = await run_blocking(MyPlexAccount, token=user_token)
        await run_blocking(guest.acceptInvite, owner.username)
        accepted = True
    except Exception as e:
        logger.warning(f"Invite link: shared with {plex_name} but could not accept for them ({type(e).__name__}); Plex will email them")

    view = PlexInviteApprovalView(email=email or None, services=services)
    view.plex_name, view.plex_account_id = plex_name, plex_account_id
    await view._add_to_user_tracking(None, plex_name, plex_account_id)
    await kv_set(WEB_JOINS_NAMESPACE, str(plex_account_id), {
        "plex_name": plex_name,
        "email": email,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "approved",
        "via": "invite link",
        "invite_label": label,
        "invited_by": created_by,
    })

    channel = find_admin_channel(bot, cfg)
    if channel:
        try:
            await channel.send(
                f"🎟️ **{plex_name}** joined Plex with the invite link “{label}” made by {created_by}"
                f"{'' if accepted else ' (they still need to accept Plex’s email)'}."
            )
        except Exception as e:
            logger.warning(f"Invite link: could not post to the admin channel: {e}")
    logger.info(f"Invite link: {plex_name} joined Plex (accepted for them: {accepted})")
    return {"ok": True, "accepted": accepted}


_JOIN_DECISION_LOCKS: Dict[int, asyncio.Lock] = {}


async def decide_join_request(bot, services, message_id: int, approve: bool, actor: str) -> Dict[str, Any]:
    """Approve or deny a join request from the website, as the Discord buttons do.

    The stored status, not the Discord card, says whether it's still open, so a
    click in Discord and one on the website can't both send an invite.
    """
    lock = _JOIN_DECISION_LOCKS.setdefault(int(message_id), asyncio.Lock())
    async with lock:
        view = PlexInviteApprovalView()
        if not await view._load(message_id, bot):
            return {"ok": False, "message": "Could not find that join request."}
        status = await view._status()
        if status not in (None, "pending"):
            return {"ok": False, "message": f"Already {status}."}
        guild = home_guild(bot, services.config)
        channel = find_admin_channel(bot, services.config)
        card = channel.get_partial_message(int(message_id)) if channel else None

        async def close(content: str) -> None:
            if card is None:
                return
            try:
                await card.edit(content=content, view=None)
            except Exception as e:
                logger.warning(f"Could not update join card {message_id}: {e}")

        if approve:
            result = await view._approve_core(bot, guild)
            if result["ok"]:
                await close(f"✅ **Approved by {actor} on the website** - {result['summary']}")
            return result
        await close(f"❌ **Denied by {actor} on the website**")
        await view._deny_core(bot)
        return {"ok": True, "message": "Denied."}


async def correct_invite_email(old: str, new: str) -> Dict[str, Any]:
    """An admin fixed a joiner's email: point Plexbie's own records at the new one.

    The join request (Discord or website) and the tracking row both hold the
    email, and the tracking row's Plex name was guessed from it. Returns
    {"discord_id", "name"} of whoever it was for, when known, so they can be told.
    """
    from sqlalchemy import func, select
    from database.session import get_session
    from plugins.user_mgmt.models import PlexUser

    old_l, found = old.strip().lower(), {"discord_id": None, "name": None}
    for namespace in (INVITES_NAMESPACE, WEB_JOINS_NAMESPACE):
        changed = {}
        for key, rec in (await kv_get_all(namespace)).items():
            if isinstance(rec, dict) and (rec.get("email") or "").strip().lower() == old_l:
                changed[key] = {**rec, "email": new}
                if namespace == INVITES_NAMESPACE and str(key).isdigit():
                    found["discord_id"] = int(key)
                found["name"] = found["name"] or rec.get("username") or rec.get("plex_name")
        if changed:
            await kv_set_many(namespace, changed)
    async with get_session() as session:
        row = (await session.execute(select(PlexUser).where(func.lower(PlexUser.plex_email) == old_l))).scalars().first()
        if row:
            if row.plex_username == old.split("@")[0]:
                row.plex_username = new.split("@")[0]      # it was only ever a guess from the email
            row.plex_email = new
            found["discord_id"] = found["discord_id"] or row.discord_id
            found["name"] = found["name"] or row.discord_username or row.plex_username
            await session.commit()
    return found
