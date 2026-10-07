"""User management plugin with automatic inactivity tracking"""
import asyncio
from datetime import datetime, timezone
from database.kv_store import kv_set_many, kv_get_all
from typing import Optional, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from plexapi.exceptions import NotFound as PlexNotFound
from sqlalchemy import delete, func, select, update

from core.blocking import run_blocking
from core.clients import USERS_TABLE_PAGE, USERS_TABLE_PAGES
from core.plex_account import can_sign_in, owner_account
from core.permissions import AdminOnlyView, require_admin
from core.logging import get_logger
from core.services import BotServices
from core.admin_mirror import send_user_dm
from utils.embeds import truncate_field
from utils.formatting import ensure_utc
from utils.views import disable_and_refresh, reply_failure
from utils.standings import load_aliases, resolve_alias, top_watchers
from database.session import get_session
from plugins.watch_party.models import WatchPartyCredit
from .models import PlexUser
from core.discord_lookup import home_guild
from database.people import person_by_name

logger = get_logger(__name__)

# Invites file location (shared with user_invites plugin)
# INVITES_FILE removed - now using database kv_store
INVITES_NAMESPACE = "plex_invites"

# Discord allows 25 fields per embed.
MAX_LISTED_USERS = 25

# The inactivity check's brakes. When the newest play Tautulli knows of, by anyone,
# is older than this, Tautulli has most likely stopped recording, and every
# last_seen is frozen: the pass warns and removes nobody until plays show up again.
STALE_HISTORY_DAYS = 7
# One pass never removes more than this many people, or this share of everyone
# tracked if that is more. Beyond it, the pass removes nobody and asks the admins.
MAX_REMOVALS_PER_PASS = 3
MAX_REMOVALS_SHARE = 0.25
# The Plex server owner as last learned ({"id", "name"}), for passes where neither
# plex.tv nor the Plex server can be asked: their exemption mustn't come and go.
PLEX_OWNER = ("user_mgmt", "plex_owner")
# The first daily check that judged people who have never watched anything. Their
# clock never counts from before it, so those already on the share then got a full
# period. Saved once, never moved.
NEVER_WATCHED_SINCE = ("user_mgmt", "never_watched_since")


async def reconcile_accounts(accounts) -> list:
    """Point tracking rows at the Plex accounts they really are, and track the rest.

    A row starts from a guess: the Plex name taken from the email someone asked to
    join with. Once they accept, plex.tv knows the real account, so each row is
    matched by Plex account id, then by email, and takes that account's name,
    email and id. Without this a wrong-then-corrected email, or a Plex name that
    differs from the email, leaves the row "no longer on Plex" while the person
    watches away, and a guessed name can even collide with someone else's.
    Returns a line per change.
    """
    by_id = {a["id"]: a for a in accounts if a.get("id")}
    by_email = {a["email"].lower(): a for a in accounts if a.get("email")}
    by_title = {a["title"]: a for a in accounts}
    changes = []
    async with get_session() as session:
        rows = (await session.execute(select(PlexUser))).scalars().all()
        taken = {r.plex_username.lower(): r for r in rows}
        # Each Plex account belongs to one row. Sign-in finds a person by account id,
        # so an id written onto a second row would sign its holder in as that row.
        holder = {r.plex_user_id: r for r in rows if r.plex_user_id}
        for row in rows:
            account = by_id.get(row.plex_user_id) or by_email.get((row.plex_email or "").lower())
            if account is None and not row.plex_user_id:
                # The exact name, only for a row nobody is attached to yet: a Plex
                # username is the account holder's to choose, so a name match on a
                # Discord-linked row would let anyone who renames themselves become
                # that person. Those are left for an admin to match on People.
                guess = by_title.get(row.plex_username)
                if guess is not None and row.discord_id:
                    logger.info(f"{row.plex_username} has the same name as a Plex account but is linked to "
                                f"Discord; not matching by name (Manage → People can)")
                elif guess is not None:
                    account = guess
            if not account or account.get("owner"):
                continue
            if row.plex_user_id and row.plex_user_id != account["id"]:
                logger.warning(f"Not moving {row.plex_username} from Plex account {row.plex_user_id} to "
                               f"{account['id']} by email; match them on People if that's right")
                continue
            if holder.get(account["id"]) not in (None, row):
                logger.warning(f"Not matching {row.plex_username} to Plex account {account['title']}: "
                               f"{holder[account['id']].plex_username} already is that account")
                continue
            name, email = account["title"], account["email"] or row.plex_email
            if (row.plex_username, row.plex_email, row.plex_user_id) == (name, email, account["id"]):
                continue
            other = taken.get(name.lower())
            if other is not None and other is not row:
                logger.warning(f"Not renaming {row.plex_username} to {name}: another tracked row already has that name")
                continue
            if row.plex_username != name:
                changes.append(f"{row.plex_username} is Plex account {name}")
                taken.pop(row.plex_username.lower(), None)
                taken[name.lower()] = row
            row.plex_username, row.plex_email, row.plex_user_id = name, email, account["id"]
            holder[account["id"]] = row
        # Everyone else the server is shared with is tracked too, however they got
        # access (a website invite, or shared in Plex directly): the inactivity check
        # covers the whole household, not only people who asked through Discord.
        # Tracking starts now, so each gets the full period before any warning.
        matched_ids = {r.plex_user_id for r in rows if r.plex_user_id}
        for account in accounts:
            if account.get("owner") or account["id"] in matched_ids or account["title"].lower() in taken:
                continue
            row = PlexUser(plex_username=account["title"], plex_email=account["email"] or None,
                           plex_user_id=account["id"])
            session.add(row)
            taken[account["title"].lower()] = row
            changes.append(f"now tracking {account['title']} (on Plex without asking through Discord)")
        await session.commit()
    for line in changes:
        logger.info(f"Learned: {line}")
    return changes


async def rename_person(services, plex_name: str, new_name: str) -> str:
    """The name shown for someone everywhere: Plexbie's pages and boards, and Tautulli's
    friendly name (which the leaderboards show). Plex itself is untouched: an owner
    can't rename someone else's Plex account. Returns what happened in Tautulli."""
    new_name = " ".join(new_name.split())[:40]
    if not new_name:
        raise ValueError("Give them a name.")
    async with get_session() as session:
        rows = (await session.execute(select(PlexUser))).scalars().all()
        row = next((r for r in rows if r.plex_username == plex_name), None)
        if row is None:
            raise LookupError("No one tracked by that name")
        if any(r is not row and (r.display_name or r.plex_username).lower() == new_name.lower() for r in rows):
            raise ValueError(f"Someone is already called {new_name}.")
        row.display_name = None if new_name == row.plex_username else new_name
        plex_id = row.plex_user_id
        await session.commit()
    tautulli = services.tautulli
    if not (tautulli.configured and plex_id):
        return "Tautulli isn't connected, so only Plexbie shows the new name."
    try:
        # edit_user resets anything it isn't given, so carry the current settings over.
        current = await tautulli.call("get_user", user_id=plex_id) or {}
        await tautulli.call("edit_user", user_id=plex_id, friendly_name=new_name,
                            custom_thumb=current.get("custom_thumb") or "",
                            keep_history=int(current.get("keep_history", 1) or 0),
                            allow_guest=int(current.get("allow_guest", 0) or 0))
        return "Tautulli shows the new name too."
    except Exception as e:
        logger.warning(f"Renamed {plex_name} in Plexbie, but Tautulli didn't take it: {e}")
        return "Tautulli didn't take the new name, so only Plexbie shows it for now."


async def set_never_remove(plex_name: str, keep: bool) -> None:
    """An admin's "Never remove" switch for one tracked person."""
    async with get_session() as session:
        row = await person_by_name(session, plex_name)
        if row is None:
            raise LookupError("No one tracked by that name")
        row.never_remove = bool(keep)
        if keep:
            row.warning_sent = False        # a pending warning no longer means anything
        await session.commit()


async def match_account(plex_name: str, account: dict) -> str:
    """An admin said which Plex account a tracking row is: remember it."""
    async with get_session() as session:
        rows = (await session.execute(select(PlexUser))).scalars().all()
        row = next((r for r in rows if r.plex_username == plex_name), None)
        if row is None:
            raise LookupError("No one tracked by that name")
        if any(r is not row and (r.plex_username.lower() == account["title"].lower() or r.plex_user_id == account["id"])
               for r in rows):
            raise ValueError(f"{account['title']} is already someone else")
        row.plex_username, row.plex_email, row.plex_user_id = account["title"], account["email"] or row.plex_email, account["id"]
        await session.commit()
    logger.info(f"Matched {plex_name} to Plex account {account['title']}")
    return account["title"]


def _add_departure_fields(embed: discord.Embed, stats: Optional[Dict[str, Any]]) -> None:
    """The stats and "come back any time" fields every goodbye DM ends with."""
    if stats:
        lines = []
        if stats.get('total_plays'):
            lines.append(f"**Total Plays:** {stats['total_plays']:,}")
        if stats.get('total_time'):
            lines.append(f"**Total Watch Time:** {stats['total_time'] // 3600:,} hours")
        if stats.get('last_watched'):
            lines.append(f"**Last Watched:** {stats['last_watched']}")
        if stats.get('favorite_media'):
            lines.append(f"**Most Watched:** {stats['favorite_media']}")
        if lines:
            embed.add_field(name="📊 Your Stats", value=truncate_field("\n".join(lines)), inline=False)
    embed.add_field(
        name="Want to come back?",
        value=(
            "You're still welcome in the Discord! If you'd like to rejoin Plex in the future, "
            "just use the `/join-plex` command to request access again."
        ),
        inline=False
    )


def _by_unique_name(tautulli_users) -> Dict[str, dict]:
    """Tautulli users by lower-cased friendly_name and Plex login. A name two users
    answer to matches neither: measuring the wrong one could remove the wrong person."""
    found: Dict[str, dict] = {}
    shared = set()
    for u in tautulli_users:
        for name in {(u.get('friendly_name') or '').lower(), (u.get('username') or '').lower()} - {''}:
            other = found.setdefault(name, u)
            if other is not u and (other.get('user_id') != u.get('user_id') or not u.get('user_id')):
                shared.add(name)
    for name in sorted(shared):
        logger.warning(f"Two Tautulli users are called {name!r}; neither is matched by that name")
        del found[name]
    return found


def _fetch_shared_usernames(config) -> set:
    """Blocking: the names that currently have access to the server.

    This is NOT systemAccounts(). That endpoint lists every account the server has
    ever seen, including ones whose access was removed - a user taken off the share
    yesterday is still in it today, verified. Anything using it to answer "does
    this person still have access" gets the wrong answer, and always in the
    direction of saying yes.

    Current access lives on plex.tv, which is also where removeFriend acts. Note
    the owner does not appear in their own share list, so callers must add them.
    """
    from core.plex_account import owner_account

    account = owner_account(config)
    return {user.title for user in account.users() if user.title}


def _shared_friend(account, plex_user_id=None, title=None):
    """Blocking: the account on the share that is this person, or None.

    By Plex account id when it is known, otherwise by exact Plex name. Never by a
    stored email, and never by name once the id is known: those can be stale, and
    a stale one can belong to someone else by now.
    """
    users = account.users()
    if plex_user_id:
        return next((u for u in users if str(u.id) == str(plex_user_id)), None)
    if title:
        return next((u for u in users if u.title == title), None)
    return None


def _is_shared_with(config, plex_user_id=None, title=None) -> bool:
    """Blocking: whether this person is on the share right now (see _shared_friend)."""
    return _shared_friend(owner_account(config), plex_user_id, title) is not None


def _fetch_shared_account_ids(config) -> set:
    """Blocking: the Plex account ids currently on the share (the owner isn't one)."""
    return {str(user.id) for user in owner_account(config).users() if user.id}


def _remove_friend(config, plex_user_id=None, title=None) -> bool:
    """Blocking: take one account off the share, found as _shared_friend finds it.

    False when nobody matching is shared with any more.
    """
    account = owner_account(config)
    friend = _shared_friend(account, plex_user_id, title)
    if friend is None:
        return False
    account.removeFriend(friend)
    return True


class UserMgmtCog(commands.Cog):
    """User management with automatic inactivity removal"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        self.bot = bot
        self.services = services
        self._link_task: Optional[asyncio.Task] = None

    async def cog_load(self):
        from webhooks.tautulli_handler import recently_live
        if await recently_live():
            self.events_connected()
        # Started last: a load that fails before this point leaves nothing running.
        self.check_inactive_users.start()
        self.auto_link_users.start()

    def events_connected(self) -> None:
        """Tautulli is sending playback events: a viewer Plexbie doesn't know yet
        starts linking at once (on_playback), so the safety-net poll runs hourly."""
        self.auto_link_users.change_interval(hours=1)

    async def on_playback(self, username: str, email: str = "", user_id: str = "", login: str = "") -> None:
        """Someone pressed play (Tautulli event).

        Warned for not watching? They hear right away that they're all set, instead
        of at the next daily check. Not known yet? Account linking runs shortly,
        once Tautulli has the new account on record.
        """
        # Plexbie stores Tautulli's display name; the Plex login is tried too.
        names = {n.strip().lower() for n in (username, login) if n and n.strip()}
        if not names:
            return
        async with get_session() as session:
            row = (await session.execute(
                select(PlexUser).where(func.lower(PlexUser.plex_username).in_(names))
            )).scalars().first()
            if row is None:
                if self._link_task is None or self._link_task.done():
                    self._link_task = asyncio.create_task(self._link_soon())
                return
            if not row.warning_sent:
                return
            row.warning_sent = False
            row.days_inactive = 0
            await session.commit()
            user = row
        logger.info(f"{user.plex_username} watched after an inactivity warning: warning cleared")
        title, body = "You're all set", "Thanks for watching! Your Plex access is safe, so there's nothing else to do."
        if not user.discord_id:
            await self._notify_without_discord(user, title, body, f"inactivity warning cleared for {user.plex_username}")
            return
        await self._dm_tracked(user, f"inactivity warning cleared for {user.plex_username}", "warning-cleared note",
                               content=f"✅ **{title}!** {body}")

    async def _link_soon(self) -> None:
        await asyncio.sleep(30)
        await self.auto_link_users()

    def cog_unload(self):
        """Cleanup tasks on unload"""
        self.check_inactive_users.cancel()
        self.auto_link_users.cancel()

    @staticmethod
    def _load_watch_aliases():
        """Blocking: the alias map used to group watch time by person."""
        return load_aliases()

    async def _plex_users_with_access(self):
        """Names that currently have access, or None if it cannot be determined.

        Returning None matters: the caller must then decline to judge, rather than
        treat an empty answer as "nobody has access" and flag every tracked user
        as an orphan. Needs a plex.tv sign-in (core/plex_account), because current
        access lives on plex.tv.
        """
        config = self.services.config
        if not can_sign_in(config):
            return None
        try:
            shared = await run_blocking(_fetch_shared_usernames, config)
        except Exception as e:
            logger.warning(f"Could not read Plex shares, skipping orphan check: {e}")
            return None

        # The owner is never in their own share list.
        owner = await self._plex_owner_name()
        if owner:
            shared.add(owner)
        return shared

    async def _plex_owner_name(self):
        """The server owner's account name, which is systemAccounts id 1."""
        if not self.services.plex_server:
            return None
        try:
            accounts = await run_blocking(self.services.plex_server.systemAccounts)
        except Exception as e:
            logger.debug(f"Could not read Plex system accounts: {e}")
            return None
        for account in accounts:
            if account.id == 1:
                return account.name
        return None

    def _is_permanently_exempt(self, user: PlexUser) -> bool:
        """The server owner is never a removal candidate.

        Their Plex account is the one the bot authenticates *with*; removing it
        would be nonsense, and removeFriend would fail against the owner anyway.
        Identified by BOT_OWNER_ID rather than by rank, so it holds even when the
        owner has not watched anything for months.
        """
        owner_id = self.services.config.bot_owner_id
        return bool(owner_id and user.discord_id == owner_id)

    async def _plex_owner(self, account=None):
        """(account name, Plex account id) of the server owner, either None if unknown.
        `account` is the owner's entry from shared_accounts when plex.tv answered;
        otherwise the Plex server's own name for them and the id a website sign-in
        remembered. Whatever is found is remembered, and stands in when nothing is."""
        from database.kv_store import kv_get, kv_set
        if account:
            name, account_id = account.get("title") or None, account.get("id")
        else:
            name = await self._plex_owner_name()
            try:
                from portal.auth import OWNER_ID
                account_id = await kv_get(*OWNER_ID)
            except Exception as e:
                logger.debug(f"Could not read the remembered Plex owner: {e}")
                account_id = None
        try:
            known = await kv_get(*PLEX_OWNER) or {}
            found = {"id": str(account_id) if account_id else known.get("id"), "name": name or known.get("name")}
            if found != {"id": known.get("id"), "name": known.get("name")}:
                await kv_set(*PLEX_OWNER, found)
            name, account_id = found["name"], found["id"]
        except Exception as e:
            logger.debug(f"Could not remember the Plex owner: {e}")
        return name, account_id

    async def _never_watched_since(self, now: datetime) -> Optional[datetime]:
        """NEVER_WATCHED_SINCE, saved as `now` on the first pass that asks. None if it
        can't be read or saved: never-watched people are then not judged this pass."""
        from database.kv_store import kv_get, kv_set
        try:
            since = await kv_get(*NEVER_WATCHED_SINCE)
            if since:
                return ensure_utc(datetime.fromisoformat(since))
            await kv_set(*NEVER_WATCHED_SINCE, now.isoformat())
            return now
        except Exception as e:
            logger.warning(f"Could not read when never-watched people were first checked, skipping them: {e}")
            return None

    @staticmethod
    def _is_plex_owner(user: PlexUser, tautulli_user: dict, owner_name, owner_account_id) -> bool:
        """The Plex owner's own row, which needn't be linked to BOT_OWNER_ID (nobody
        linked it, or another Discord account set the bot up). removeFriend fails
        against the owner, so without this they'd be warned and fail removal daily."""
        if owner_account_id and str(owner_account_id) in {str(user.plex_user_id or ""),
                                                          str(tautulli_user.get('user_id') or "")}:
            return True
        return bool(owner_name and user.plex_username.lower() == owner_name.lower())

    @tasks.loop(hours=24)
    async def check_inactive_users(self):
        """Check all users for inactivity every 24 hours"""
        if not self.services.tautulli.configured:
            logger.warning("Tautulli not configured, skipping inactivity check")
            return

        try:
            logger.info("Starting daily inactivity check...")
            # First learn which Plex account each tracked row really is (see reconcile_accounts).
            owner_account_entry = None
            # Plex account ids on the share right now, when plex.tv could be asked.
            on_share = None
            if can_sign_in(self.services.config):
                try:
                    from core.plex_account import shared_accounts
                    accounts = await run_blocking(shared_accounts, self.services.config)
                    owner_account_entry = next((a for a in accounts if a.get("owner")), None)
                    on_share = {str(a["id"]) for a in accounts if a.get("id") and not a.get("owner")}
                    await reconcile_accounts(accounts)
                except Exception as e:
                    logger.info(f"Couldn't match tracked people to Plex accounts: {e}")

            # Get user data from Tautulli (uses same usernames as our database)
            tautulli_users = await self.services.tautulli.users_table()

            # Build lookup by friendly_name or Plex login (case-insensitive)
            tautulli_by_name = _by_unique_name(tautulli_users)
            # Tautulli's user_id is the Plex account id: the sure match once a row knows
            # its account (reconcile_accounts), whatever either side calls the person.
            tautulli_by_id = {str(u.get('user_id')): u for u in tautulli_users if u.get('user_id')}

            # Standings, for the top-watcher exemption. Computed from the response
            # already in hand plus watch-party credits, through the same helper the
            # leaderboard uses, so the two cannot disagree about who is in the top
            # three. Aliases matter here: a person whose Plex name and Tautulli
            # friendly_name differ would otherwise be split into two partial totals
            # and could be ranked out of an exemption they have earned.
            aliases = await run_blocking(self._load_watch_aliases)
            async with get_session() as session:
                credit_rows = await session.execute(
                    select(WatchPartyCredit.plex_username, WatchPartyCredit.total_duration)
                )
                credits = {name: total for name, total in credit_rows.all()}

            top = top_watchers(tautulli_users, aliases, credits)
            if not top:
                # No standings means we cannot tell who is exempt. Treating that as
                # "nobody is exempt" would start every user's clock at once on a
                # Tautulli hiccup, so do nothing at all this pass.
                logger.warning(
                    "Inactivity check skipped: no watch-time standings available, "
                    "so top-watcher exemptions cannot be determined"
                )
                return
            logger.info(f"Top {len(top)} watchers (exempt from removal): {', '.join(top)}")

            # Tautulli can keep answering after it has stopped recording plays (a
            # broken Plex connection or token, a changed server address). Every
            # last_seen then freezes and the whole household drifts towards
            # removal, so judge nobody until a recent play shows it is recording.
            newest_seen = max((int(u.get('last_seen') or 0) for u in tautulli_users), default=0)
            history_stale = bool(newest_seen) and (
                datetime.now(timezone.utc) - datetime.fromtimestamp(newest_seen, tz=timezone.utc)
            ).days > STALE_HISTORY_DAYS


            # Three phases, so that no Discord DM, plex.tv login or Tautulli call
            # happens while a database transaction is open.
            #
            # This used to be one `async with get_session()` wrapped around the
            # whole loop, including _send_warning_dm and _remove_inactive_user -
            # the latter logs into plex.tv, calls removeFriend, sends a DM and
            # edits a Discord role. The journal mode on this database is `delete`,
            # not WAL, so the read transaction opened by the SELECT below holds a
            # SHARED lock for its whole lifetime: every other plugin's kv_set
            # would queue behind it and, after the 5s busy timeout, fail with
            # "database is locked". With a handful of users the window was short
            # enough that it never actually bit; it grows with the user count.

            # Phase 1: read, then close. expire_on_commit=False, so the loaded
            # column values stay readable on the detached objects below.
            async with get_session() as session:
                result = await session.execute(select(PlexUser))
                tracked_users = result.scalars().all()

            def persisted_fields(user: PlexUser):
                """The columns this check is allowed to change."""
                return (user.last_watched, user.days_inactive, user.warning_sent,
                        user.is_top_watcher, user.exemption_lost_at)

            # Snapshot up front so the write phase can tell what changed, rather
            # than relying on every assignment site below to remember to flag
            # itself. Note this only actually skips the rows that take one of the
            # `continue` paths without assigning anything: on the main branch
            # last_watched is reassigned from Tautulli as a tz-aware datetime while
            # the column reads back naive, and those never compare equal, so such a
            # row is always rewritten. That is correct, just not a saving.
            before = {user.id: persisted_fields(user) for user in tracked_users}

            owner_name, owner_account_id = await self._plex_owner(owner_account_entry)
            never_watched_since = await self._never_watched_since(datetime.now(timezone.utc))
            # Tautulli's table stops at USERS_TABLE_PAGES pages; someone missing from a
            # cut-off table may well have watched.
            table_complete = len(tautulli_users) < USERS_TABLE_PAGE * USERS_TABLE_PAGES

            # Phase 2: decide. No transaction, no network - the field assignments
            # here are made on detached objects and persisted in phase 4.
            now = datetime.now(timezone.utc)
            to_warn = []
            to_remove = []
            to_notify_exemption_lost = []

            for tracked_user in tracked_users:
                unlisted = False
                # Find corresponding Tautulli user
                tautulli_user = tautulli_by_id.get(str(tracked_user.plex_user_id or ""))
                if not tautulli_user:
                    by_name = tautulli_by_name.get(tracked_user.plex_username.lower())
                    # A row that knows its account only takes a name match for that
                    # same account: the name can belong to someone else by now, and
                    # measuring them could remove the wrong person.
                    other_id = (by_name or {}).get('user_id')
                    if tracked_user.plex_user_id and other_id and str(other_id) != str(tracked_user.plex_user_id):
                        logger.warning(
                            f"Tracked user {tracked_user.plex_username} is Plex account {tracked_user.plex_user_id}, "
                            f"but Tautulli's {tracked_user.plex_username} is account {other_id} - skipping"
                        )
                        continue
                    tautulli_user = by_name

                if not tautulli_user:
                    # Tautulli has no row for this Plex account, which the share still
                    # has: they have never played anything, and are judged like anyone
                    # who never watched (below). Anyone else is skipped: off the share,
                    # an invite not accepted yet, no account known (a name two Tautulli
                    # users share, say), plex.tv not answering, or a cut-off table.
                    account_id = str(tracked_user.plex_user_id or "")
                    if not (table_complete and on_share is not None and account_id in on_share):
                        logger.warning(f"Tracked user {tracked_user.plex_username} not found in Tautulli - skipping (may be new)")
                        continue
                    logger.info(f"Tracked user {tracked_user.plex_username} not found in Tautulli; counting as never watched")
                    tautulli_user, unlisted = {}, True

                # ---- top-watcher exemption -------------------------------------
                # Decided before any warning or removal, and recorded, so that
                # *losing* the exemption can be detected on the pass it happens
                # rather than merely observing that someone is outside the top three.
                # The standings are keyed by Tautulli's name for each person, so look them
                # up by that (found above by account id), not by Plexbie's name for them:
                # a rename in Tautulli or Plex must not cost anyone their exemption.
                primary = resolve_alias(tautulli_user.get('friendly_name') or tracked_user.plex_username, aliases)
                exempt = (primary in top or self._is_permanently_exempt(tracked_user)
                          or self._is_plex_owner(tracked_user, tautulli_user, owner_name, owner_account_id))
                just_lost_exemption = bool(tracked_user.is_top_watcher) and not exempt

                if exempt:
                    if not tracked_user.is_top_watcher:
                        logger.info(
                            f"User {tracked_user.plex_username} is now exempt from removal "
                            f"(top {len(top)} watch time)"
                        )
                    tracked_user.is_top_watcher = True
                    tracked_user.exemption_lost_at = None
                    # Not on the chop block, so any pending warning is void. Without
                    # this, re-entering the top three would leave warning_sent set
                    # and the next drop-out would remove them with no fresh warning.
                    tracked_user.warning_sent = False
                elif just_lost_exemption:
                    tracked_user.is_top_watcher = False
                    tracked_user.exemption_lost_at = now
                    tracked_user.warning_sent = False
                    to_notify_exemption_lost.append(tracked_user)
                    logger.info(
                        f"User {tracked_user.plex_username} dropped out of the top "
                        f"{len(top)}; inactivity clock restarts now"
                    )

                # A user who is exempt, or who lost it on this very pass, is not a
                # warning or removal candidate. Activity figures are still updated
                # below so the admin views stay truthful.
                skip_enforcement = exempt or just_lost_exemption or bool(tracked_user.never_remove)

                # Get last watched from Tautulli
                last_played = tautulli_user.get('last_seen')

                if last_played:
                    # Convert Unix timestamp to datetime
                    last_watched = datetime.fromtimestamp(int(last_played), tz=timezone.utc)
                    created_at = ensure_utc(tracked_user.created_at)

                    # The clock counts from the latest of three dates, and it must
                    # always count from *something* - see below.
                    baseline = last_watched

                    # Tautulli keeps history from before this tracking entry
                    # existed, so a re-approved or newly-tracked user can have a
                    # last_seen from months ago. Counting from that would remove
                    # them on their first pass. This used to `continue` with
                    # days_inactive = 0 instead, which meant such a user was
                    # exempt *permanently*: last_seen only moves forward when they
                    # watch, so the condition never stopped being true. Counting
                    # from when tracking started gives them a real, finite period.
                    if created_at > baseline:
                        logger.info(
                            f"User {tracked_user.plex_username}: Tautulli last_seen "
                            f"{last_watched.date()} predates tracking start "
                            f"{created_at.date()}; counting from tracking start"
                        )
                        baseline = created_at

                    # Losing a top-three exemption restarts the clock, which is what
                    # grants a full fresh period even to someone already long idle.
                    lost_at = ensure_utc(tracked_user.exemption_lost_at)
                    if lost_at is not None and lost_at > baseline:
                        baseline = lost_at

                    days_since = (now - baseline).days

                    # Update tracked user. last_watched stays the real viewing date;
                    # days_inactive is measured from the baseline, because that is
                    # the number the warning and removal thresholds act on.
                    tracked_user.last_watched = last_watched
                    tracked_user.days_inactive = days_since

                    # Reset warning flag if user became active again. Use the
                    # configured threshold, not a hardcoded 25, so the reset and
                    # the warning below cannot desynchronize.
                    if days_since < self.services.config.inactivity_warning_days:
                        tracked_user.warning_sent = False

                    if skip_enforcement:
                        logger.info(
                            f"User {tracked_user.plex_username}: {days_since} days "
                            f"inactive (exempt from removal)"
                        )
                        continue

                    logger.info(f"User {tracked_user.plex_username}: {days_since} days inactive")

                    # Warning uses >= not ==: this loop runs every 24h and restarts
                    # with the bot, so an exact-day match is skipped whenever a
                    # pass is missed, and the user would then hit the removal
                    # threshold having never been warned.
                    if days_since >= self.services.config.inactivity_warning_days and not tracked_user.warning_sent:
                        to_warn.append(tracked_user)
                        # Deliberately do not remove on the same pass that warns,
                        # even if already past the removal threshold: a warning
                        # nobody had a chance to act on is not a warning.
                        continue

                    # Removal, only ever after a warning was delivered.
                    if days_since >= self.services.config.inactivity_removal_days:
                        to_remove.append((tracked_user, tautulli_user.get('user_id', 0)))
                        continue

                else:
                    # No play in Tautulli's history: count from the last watch Plexbie saw.
                    baseline = ensure_utc(tracked_user.last_watched)
                    if baseline is None or unlisted:
                        # Never watched anything (or not in Tautulli at all): the clock
                        # runs from when tracking started, but never from before
                        # never-watched people were first judged (NEVER_WATCHED_SINCE).
                        if never_watched_since is None:
                            continue
                        if tautulli_user.get('keep_history') in (0, "0"):
                            # Tautulli records none of their plays, so no history isn't
                            # no watching.
                            logger.info(f"User {tracked_user.plex_username}: Tautulli doesn't keep their "
                                        f"history, so not judged as never watched")
                            tracked_user.days_inactive = 0
                            continue
                        baseline = max(d for d in (baseline, ensure_utc(tracked_user.created_at),
                                                   never_watched_since) if d is not None)
                    lost_at = ensure_utc(tracked_user.exemption_lost_at)
                    if lost_at is not None and lost_at > baseline:
                        baseline = lost_at
                    days_since = (now - baseline).days
                    tracked_user.days_inactive = days_since

                    if skip_enforcement:
                        logger.info(
                            f"User {tracked_user.plex_username}: {days_since} days "
                            f"inactive (exempt from removal)"
                        )
                        continue

                    logger.info(f"User {tracked_user.plex_username}: {days_since} days inactive")

                    # Same warn-then-remove sequencing as the branch above.
                    if days_since >= self.services.config.inactivity_warning_days and not tracked_user.warning_sent:
                        to_warn.append(tracked_user)
                        continue

                    if days_since >= self.services.config.inactivity_removal_days:
                        to_remove.append((tracked_user, tautulli_user.get('user_id', 0)))
                        continue

            if history_stale and (to_warn or to_remove):
                newest = datetime.fromtimestamp(newest_seen, tz=timezone.utc).date()
                logger.warning(
                    f"Inactivity check paused: Tautulli's newest play is from {newest}, so its history "
                    f"looks stale. Not warning {len(to_warn)} or removing {len(to_remove)} this pass"
                )
                await self._alert_admins(
                    "Inactivity check paused",
                    f"Tautulli hasn't recorded a play by anyone since {newest}, so Plexbie warned and "
                    f"removed nobody today ({len(to_warn)} warning(s) and {len(to_remove)} removal(s) "
                    "held back). Check that Tautulli is still connected to Plex. Once it records plays "
                    "again, the daily check carries on by itself.",
                    push=f"Tautulli hasn't recorded a play since {newest}, so nobody was warned or removed.",
                    url="/manage?tab=health", tag="inactivity-stale")
                to_warn, to_remove = [], []

            removal_limit = max(MAX_REMOVALS_PER_PASS, int(len(tracked_users) * MAX_REMOVALS_SHARE))
            due_rows = to_remove
            if len(to_remove) > removal_limit and can_sign_in(self.services.config):
                # Rows whose account is already off the share remove nobody (they are
                # kept until an admin forgets them), so they don't count: otherwise a
                # few of them would hold back every real removal, every day.
                try:
                    shared_ids = await run_blocking(_fetch_shared_account_ids, self.services.config)
                    due_rows = [(user, uid) for user, uid in to_remove
                                if not (user.plex_user_id or uid) or str(user.plex_user_id or uid) in shared_ids]
                except Exception as e:
                    logger.info(f"Couldn't read Plex shares before the removal limit: {e}")
            if len(due_rows) > removal_limit:
                due = [user.plex_username for user, _ in due_rows]
                logger.warning(
                    f"Inactivity check would remove {len(due)} of {len(tracked_users)} people in one "
                    f"pass (limit {removal_limit}); removing nobody: {', '.join(due)}"
                )
                names = discord.utils.escape_mentions(
                    ", ".join(due[:15]) + (f" and {len(due) - 15} more" if len(due) > 15 else ""))
                await self._alert_admins(
                    "Inactivity removals held back",
                    f"The daily check would have removed {len(due)} of {len(tracked_users)} people "
                    f"from Plex in one go, more than the {removal_limit} it removes by itself, so it "
                    f"removed nobody: {names}. If they really are inactive, remove them in Manage → "
                    "People or with /remove-user. If not, check that Tautulli is recording plays.",
                    push=f"{len(due)} people were due for removal at once, so nobody was removed. Have a look.",
                    url="/manage?tab=people", tag="inactivity-cap")
                to_remove = []

            # Phase 3: the slow part, with nothing held open.
            for tracked_user in to_notify_exemption_lost:
                await self._send_exemption_lost_dm(tracked_user)

            unwarned = []
            for tracked_user in to_warn:
                if not await self._send_warning_dm(tracked_user):
                    unwarned.append(tracked_user.plex_username)
                # Set unconditionally, as before: only marking on success would mean
                # a user nothing reaches is warned every day and never removed. The
                # admins hear about those instead, below.
                tracked_user.warning_sent = True
            if unwarned:
                cfg = self.services.config
                logger.warning(f"Inactivity warning reached nobody: {', '.join(unwarned)}")
                names = discord.utils.escape_mentions(
                    ", ".join(unwarned[:15]) + (f" and {len(unwarned) - 15} more" if len(unwarned) > 15 else ""))
                await self._alert_admins(
                    "Inactivity warning not delivered",
                    f"Plexbie couldn't warn {names} that they'll lose Plex access for not watching: "
                    "no Discord DM, phone alert or email reached them (Manage → Messages says why). "
                    "Unless they watch something, they're removed once they reach "
                    f"{cfg.inactivity_removal_days} days, on tomorrow's check at the earliest. Tell them "
                    "yourself, or turn on Never remove in Manage → People.",
                    push=(f"Couldn't warn {unwarned[0]} about inactivity removal. Tell them yourself?"
                          if len(unwarned) == 1 else
                          f"Couldn't warn {len(unwarned)} people about inactivity removal. Tell them yourself?"),
                    url="/manage?tab=people", tag="inactivity-undelivered")

            removed_ids = set()
            for tracked_user, plex_user_id in to_remove:
                if await self._remove_inactive_user(tracked_user, plex_user_id):
                    removed_ids.add(tracked_user.id)

            # Phase 4: one short write. A failed removal deliberately keeps its row
            # and still persists the updated activity fields, so the next pass
            # retries with current numbers.
            async with get_session() as session:
                for tracked_user in tracked_users:
                    if tracked_user.id in removed_ids:
                        await session.execute(
                            delete(PlexUser).where(PlexUser.id == tracked_user.id)
                        )
                    elif persisted_fields(tracked_user) != before[tracked_user.id]:
                        await session.execute(
                            update(PlexUser)
                            .where(PlexUser.id == tracked_user.id)
                            .values(
                                last_watched=tracked_user.last_watched,
                                days_inactive=tracked_user.days_inactive,
                                warning_sent=tracked_user.warning_sent,
                                is_top_watcher=tracked_user.is_top_watcher,
                                exemption_lost_at=tracked_user.exemption_lost_at,
                            )
                        )
                await session.commit()

            logger.info("Daily inactivity check completed")

        except Exception as e:
            logger.error(f"Error during inactivity check: {e}", exc_info=True)

    async def _alert_admins(self, title: str, text: str, *, push: str, url: str, tag: str) -> None:
        """Tell the admins the daily check held something back: the admin channel and their phones."""
        from core.admin_mirror import _send_admin_receipt
        from core.notify import alert_admins_soon
        await _send_admin_receipt(self.bot, self.services, header=f"⚠️ **{title}**", content=text)
        alert_admins_soon(self.bot, self.services.config, title=title, body=push, url=url, tag=tag)

    async def _link_invite(self, discord_id: int, tautulli_user: dict, invite_email: str):
        """Attach a Discord join request to the Plex account that accepted it.

        The account is the Tautulli user's id (its Plex account id), never a name:
        names are the account holder's to choose. A row already linked to another
        Discord member is never re-linked; admins are told instead, since the email
        typed when asking is unverified. Returns (status, plex name), or None to try
        again later.
        """
        try:
            uid = int(tautulli_user.get("user_id"))
        except (TypeError, ValueError):
            return None
        name = (tautulli_user.get("username") or tautulli_user.get("friendly_name") or "").strip()
        async with get_session() as session:
            rows = (await session.execute(select(PlexUser))).scalars().all()
            by_account = next((r for r in rows if r.plex_user_id == uid), None)
            mine = next((r for r in rows if r.discord_id == discord_id), None)
            if by_account is not None and by_account.discord_id not in (None, discord_id):
                logger.warning(f"Not linking Discord {discord_id} to Plex account {by_account.plex_username}: "
                               f"it's already linked to Discord {by_account.discord_id}")
                await self._tell_admins_link_conflict(discord_id, by_account)
                return ("conflict", by_account.plex_username)
            if mine is not None and mine.plex_user_id not in (None, uid):
                logger.warning(f"Not linking Discord {discord_id} to Plex account {uid}: their row is "
                               f"Plex account {mine.plex_user_id}")
                return ("conflict", mine.plex_username)
            discord_name = None
            try:
                guild = home_guild(self.bot, self.services.config)
                member = guild.get_member(discord_id) if guild else None
                if member:
                    discord_name = member.nick or member.name
            except (AttributeError, discord.NotFound) as e:
                logger.debug(f"Could not get Discord member info: {e}")
            if by_account is not None:
                if mine is not None and mine is not by_account:
                    # The placeholder made when they asked (no Plex account yet) gives
                    # way to the row for the account they accepted with.
                    await session.delete(mine)
                    await session.flush()
                by_account.discord_id = discord_id
                by_account.discord_username = discord_name or by_account.discord_username
                row = by_account
            elif mine is not None:
                mine.plex_user_id = uid
                if name and not any(r is not mine and r.plex_username.lower() == name.lower() for r in rows):
                    mine.plex_username = name
                row = mine
            else:
                if not name or any(r.plex_username.lower() == name.lower() for r in rows):
                    return None
                row = PlexUser(discord_id=discord_id, discord_username=discord_name, plex_username=name,
                               plex_user_id=uid)
                session.add(row)
            row.plex_email = row.plex_email or invite_email
            await session.commit()
            logger.info(f"Auto-linked Plex account {row.plex_username} to Discord ID {discord_id}")
            return ("linked", row.plex_username)

    async def _tell_admins_link_conflict(self, discord_id: int, row) -> None:
        from core.admin_mirror import _send_admin_receipt

        def who(did) -> str:
            guild = home_guild(self.bot, self.services.config)
            member = guild.get_member(int(did)) if guild else None
            return f"{member.display_name} ({did})" if member else f"Discord member {did}"
        try:
            await _send_admin_receipt(
                self.bot, self.services, header="Not linked automatically",
                content=(f"{who(discord_id)} asked to join with an email whose Plex account ({row.plex_username}) "
                         f"is already linked to {who(row.discord_id)}. Plexbie left both as they are. If it's "
                         f"right, link them in Manage → People."))
        except Exception as e:
            logger.info(f"Couldn't tell admins about a link conflict: {e}")

    @tasks.loop(minutes=5)
    async def auto_link_users(self):
        """Check for pending invites and auto-link when users appear on Plex/Tautulli"""
        if not self.services.tautulli.configured:
            return

        try:
            # Load pending invites from database
            invites = await kv_get_all(INVITES_NAMESPACE)
            if not invites:
                return

            # Only contact Tautulli if something is actually waiting to be linked.
            # Invite records are never removed once they link, so the steady state is
            # a namespace where every entry is already 'linked' - and this loop was
            # fetching the entire Tautulli user table every 5 minutes (288 times a
            # day) only to skip every row it got back.
            # Only requests an admin approved (the Plex invite went out): one still
            # waiting for approval isn't on Plex yet, whatever Tautulli remembers.
            pending = {
                discord_id_str: invite_data
                for discord_id_str, invite_data in invites.items()
                if invite_data.get('status') == 'approved' and invite_data.get('email')
            }
            if not pending:
                return

            # Get current Tautulli users with emails
            tautulli_users = await self.services.tautulli.users()

            # Build email -> Tautulli user mapping (case-insensitive)
            email_to_tautulli = {}
            for user in tautulli_users:
                # Tautulli keeps people after they're removed from Plex (is_active 0):
                # someone removed and asking again isn't "back" until they accept.
                if user.get('email') and str(user.get('is_active', 1)) not in ('0', 'False', 'false'):
                    email_to_tautulli[user['email'].lower()] = user

            # Check each pending invite. The 'linked' and missing-email cases are
            # already excluded by the `pending` filter above.
            linked_keys = []
            for discord_id_str, invite_data in list(pending.items()):
                invite_email = invite_data['email'].lower()

                # Check if this email exists in Tautulli
                if invite_email not in email_to_tautulli:
                    continue
                tautulli_user = email_to_tautulli[invite_email]
                discord_id = int(discord_id_str)
                outcome = await self._link_invite(discord_id, tautulli_user, invite_email)
                if outcome is None:
                    continue
                invites[discord_id_str]['status'] = outcome[0]
                invites[discord_id_str]['plex_username'] = outcome[1]
                linked_keys.append(discord_id_str)

            # Write back only the invites that changed, in one transaction. This
            # used to rewrite every invite in the namespace whenever a single one
            # linked - a separate transaction and fsync each, almost all of them
            # writing back bytes that were already there.
            if linked_keys:
                await kv_set_many(
                    INVITES_NAMESPACE,
                    {key: invites[key] for key in linked_keys},
                )
                logger.info(f"Auto-linked {len(linked_keys)} user(s)")

        except Exception as e:
            logger.error(f"Error in auto_link_users: {e}", exc_info=True)

    @auto_link_users.before_loop
    async def before_auto_link_users(self):
        """Wait for bot to be ready"""
        await self.bot.wait_until_ready()

    async def _notify_without_discord(self, user: PlexUser, title: str, body: str, context: str) -> str:
        """For members who joined without Discord, or whose DMs are closed: a phone alert
        from plexbie.com, else email. Returns notify_member's "push", "email" or "none"."""
        from core.notify import notify_member
        return await notify_member(self.services, title=title, body=body, context=context,
                                   plex_name=user.plex_username, email=user.plex_email,
                                   discord_id=str(user.discord_id) if user.discord_id else None)

    async def _dm_tracked(self, user: PlexUser, context: str, what: str, *, embed=None, content=None,
                          own_fallback: bool = False) -> bool:
        """DM a tracked member who has Discord; a closed DM or a failure is logged, never raised.
        True if the DM went. `own_fallback` as for send_user_dm."""
        try:
            discord_user = await self.bot.fetch_user(user.discord_id)
            await send_user_dm(self.bot, self.services, discord_user, context=context, embed=embed, content=content,
                               own_fallback=own_fallback)
            logger.info(f"Sent {what} to {user.plex_username} (Discord: {user.discord_username})")
            return True
        except discord.Forbidden:
            logger.warning(f"Cannot DM user {user.discord_username} - DMs are disabled")
        except Exception as e:
            logger.error(f"Error sending {what} to {user.plex_username}: {e}")
        return False

    async def _send_warning_dm(self, user: PlexUser) -> bool:
        """Send 25-day inactivity warning to user. By DM, else (no Discord, closed DMs) by
        phone alert or email. True if it reached them by any of those."""
        cfg = self.services.config
        context = f"25-day inactivity warning for {user.plex_username}"
        title = "Watch something to keep your Plex access"
        body = (f"You haven't watched anything on the household Plex in {cfg.inactivity_warning_days} days. "
                f"Watch anything in the next {cfg.inactivity_removal_days - cfg.inactivity_warning_days} days "
                f"and your access stays; otherwise it's removed to make room.")
        if not user.discord_id:
            return await self._notify_without_discord(user, title, body, context) != "none"

        embed = discord.Embed(
            title="⚠️ Plex Inactivity Warning",
            description=f"Hey there! We noticed you haven't watched anything on the Plex server in **{self.services.config.inactivity_warning_days} days**.",
            color=discord.Color.orange()
        )

        embed.add_field(
            name="What happens next?",
            value=(
                f"If you remain inactive for **{self.services.config.inactivity_removal_days - self.services.config.inactivity_warning_days} more days** ({self.services.config.inactivity_removal_days} days total), "
                "you'll be automatically removed from the Plex server to make room for active members."
            ),
            inline=False
        )

        embed.add_field(
            name="Want to stay?",
            value="Simply watch something on Plex to reset your inactivity timer!",
            inline=False
        )

        embed.set_footer(text="This is an automated message from Plexbie")

        # own_fallback: the phone alert below is the fallback, so the app copy that
        # send_user_dm makes goes only with a DM that arrived, never twice.
        if await self._dm_tracked(user, context, "25-day warning", embed=embed, own_fallback=True):
            return True
        return await self._notify_without_discord(user, title, body, context) != "none"

    async def _send_exemption_lost_dm(self, user: PlexUser):
        """Tell a user their top-three exemption has ended and the clock restarts.

        Sent on the pass they drop out, before any warning, and deliberately even
        when they are already long past the removal threshold - that case is the
        whole point of the message: they were shielded by their watch time, and now
        they are not, so they get a full fresh period rather than an immediate
        removal.
        """
        if not user.discord_id:
            await self._notify_without_discord(
                user, "You're out of the top three",
                "Your watch time no longer keeps your Plex access safe, so the inactivity clock starts again "
                f"from today. Watch something at least every {self.services.config.inactivity_removal_days} days to keep it.",
                f"top-three exemption lost for {user.plex_username}")
            return

        removal_days = self.services.config.inactivity_removal_days
        warning_days = self.services.config.inactivity_warning_days

        embed = discord.Embed(
            title="📉 You're no longer exempt from inactivity removal",
            description=(
                "You've dropped out of the **top 3** watch time on the Plex "
                "server, so the inactivity exemption that came with it no "
                "longer applies."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="Your timer starts now",
            value=(
                f"You have a fresh **{removal_days} days** from today, however "
                f"long it has been since you last watched something. "
                f"We'll warn you at **{warning_days} days** if you're heading "
                f"towards removal."
            ),
            inline=False,
        )
        embed.add_field(
            name="Want the exemption back?",
            value=(
                "Watch enough to climb back into the top 3 and you're off the "
                "chop block again."
            ),
            inline=False,
        )
        embed.set_footer(text="This is an automated message from Plexbie")

        await self._dm_tracked(user, f"top-three exemption lost for {user.plex_username}", "exemption-lost DM", embed=embed)

    async def _remove_inactive_user(self, user: PlexUser, plex_user_id: int) -> bool:
        """Remove user from Plex and send farewell. True if the row should be deleted.

        The caller deletes the row in its own short transaction. Doing it here
        required an open session to be threaded through plex.tv logins and Discord
        DMs, which is exactly the lock window this avoids.
        """
        try:
            # Collect stats up front (needs the account to still exist in Tautulli),
            # but do not announce anything until the removal actually succeeds -
            # otherwise a failed removal DMs a farewell on every daily pass.
            stats = await self._get_user_stats(user.plex_username)

            # Remove from Plex
            if not can_sign_in(self.services.config):
                logger.error(
                    f"Cannot remove {user.plex_username} from Plex: no plex.tv sign-in (PLEX_TOKEN "
                    f"or PLEX_USERNAME/PLEX_PASSWORD). Keeping tracking row so this retries."
                )
                return False

            try:
                # Both plex.tv calls are blocking; this runs inside the daily
                # loop, once per user being removed. The account taken off is the
                # one whose inactivity was measured (Tautulli's user_id is the Plex
                # account id), not whoever the stored email or name points at now.
                if user.plex_user_id and plex_user_id and str(plex_user_id) != str(user.plex_user_id):
                    logger.warning(
                        f"{user.plex_username} is Plex account {user.plex_user_id}, but the inactivity measured "
                        f"was account {plex_user_id}; removing nobody and keeping tracking row."
                    )
                    return False
                account_id = user.plex_user_id or plex_user_id
                if account_id:
                    if not await run_blocking(_remove_friend, self.services.config, account_id):
                        logger.warning(
                            f"{user.plex_username} (Plex account {account_id}) isn't shared with any more; "
                            f"keeping tracking row. Remove them with /remove-user or Manage → People "
                            f"to forget them."
                        )
                        return False
                else:
                    account = await run_blocking(owner_account, self.services.config)
                    friend_key = user.plex_email or user.plex_username
                    await run_blocking(account.removeFriend, friend_key)
                logger.info(f"Removed {user.plex_username} from Plex server")
            except Exception as e:
                # Do NOT fall through to the database delete. Dropping the row
                # after a failed removal leaves the user with Plex access forever
                # and no record to retry against.
                logger.error(
                    f"Error removing {user.plex_username} from Plex: {e} - "
                    f"keeping tracking row so the next pass retries",
                    exc_info=True
                )
                return False

            # Removal succeeded - now notify and clean up Discord state.
            if user.discord_id:
                await self._send_farewell_dm(user, stats)
                await self._remove_plex_role(user.discord_id)
            else:
                await self._notify_without_discord(
                    user, "Your Plex access has ended",
                    f"Nothing was watched on the household Plex for {self.services.config.inactivity_removal_days} days, "
                    "so your access was removed to make room. Thanks for watching! If you'd like back in, "
                    "ask whoever invited you for a new invite link.",
                    f"inactivity removal farewell for {user.plex_username}")

            # Signal the caller to drop the tracking row.
            logger.info(f"Removing {user.plex_username} from tracking database")
            return True

        except Exception as e:
            logger.error(f"Error removing inactive user {user.plex_username}: {e}", exc_info=True)
            return False

    async def _send_farewell_dm(self, user: PlexUser, stats: Dict[str, Any]):
        """Send farewell message with user stats"""
        if not user.discord_id:
            return

        embed = discord.Embed(
            title="👋 Farewell from Plex",
            description=(
                f"Thank you for being part of our Plex household, **{user.discord_username}**! "
                f"Due to {self.services.config.inactivity_removal_days} days of inactivity, it's time to say goodbye."
            ),
            color=discord.Color.red()
        )

        _add_departure_fields(embed, stats)

        embed.set_footer(text="Thanks for the memories! 🎬")

        await self._dm_tracked(user, f"inactivity removal farewell for {user.plex_username}", "farewell message", embed=embed)

    async def _get_user_stats(self, plex_username: str) -> Dict[str, Any]:
        """Get user statistics from Tautulli"""
        if not self.services.tautulli.configured:
            return {}

        try:
            tautulli = self.services.tautulli
            user_data = await tautulli.call("get_user", user=plex_username) or {}
            # The latest play, for "last watched".
            history = ((await tautulli.call("get_history", user=plex_username, length=1)) or {}).get("data") or []
            last = history[0] if history else None
            return {
                'total_plays': user_data.get('plays', 0),
                'total_time': user_data.get('duration', 0),
                'last_watched': last.get('full_title') if last else None,
                'favorite_media': (user_data.get('most_watched') or {}).get('title')
            }

        except Exception as e:
            logger.error(f"Error getting stats for {plex_username}: {e}")
            return {}

    async def _remove_plex_role(self, discord_id: int):
        """Remove Plex member role from Discord user"""
        if not self.services.config.plex_member_role_id:
            logger.warning("Plex member role ID not configured")
            return

        try:
            guild = home_guild(self.bot, self.services.config)
            if not guild:
                logger.error("Guild not found")
                return

            member = guild.get_member(discord_id)
            if not member:
                logger.warning(f"Member {discord_id} not found in guild")
                return

            role = guild.get_role(self.services.config.plex_member_role_id)
            if not role:
                logger.warning(f"Plex member role {self.services.config.plex_member_role_id} not found")
                return

            await member.remove_roles(role, reason="Removed due to inactivity on Plex")
            logger.info(f"Removed Plex role from {member.name}")

        except Exception as e:
            logger.error(f"Error removing Plex role from user {discord_id}: {e}")

    @check_inactive_users.before_loop
    async def before_check_inactive_users(self):
        """Wait for bot to be ready before starting checks"""
        await self.bot.wait_until_ready()

    # Admin command for manual removal
    @app_commands.command(name="remove-user", description="Manually remove a user from Plex")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(plex_username="The Plex username to remove")
    async def remove_user(self, interaction: discord.Interaction, plex_username: str):
        """Manually remove a user from Plex with notification"""
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        ok, message = await self.remove_plex_user(plex_username, interaction.user.name)
        await interaction.followup.send(message, ephemeral=True)

    async def remove_plex_user(self, plex_username: str, removed_by: str) -> tuple:
        """Remove someone from Plex, notify them, drop their tracking row.

        Shared by /remove-user and the website's Manage page. Returns
        (ok, message for the admin).
        """
        try:
            if not self.services.plex_server:
                return False, "❌ Plex server not configured"

            # Read the tracking row, then close the session. Everything after this
            # point is plex.tv, Tautulli and Discord I/O; holding a transaction
            # across it locks the database against every other plugin for as long
            # as those calls take. Same restructuring as the automatic path,
            # check_inactive_users.
            async with get_session() as session:
                tracked_user = await person_by_name(session, plex_username)

            if not tracked_user:
                return False, (
                    f"❌ User `{plex_username}` not found in tracking database.\n"
                    f"They may not be tracked or may not exist on the Plex server."
                )

            # The owner isn't on their own share, so the check below would forget them.
            if self._is_permanently_exempt(tracked_user) or tracked_user.plex_username == await self._plex_owner_name():
                return False, f"❌ `{plex_username}` is the server owner's account, which can't be removed from Plex."

            # Are they still on the share? Asked of plex.tv by account id (by name
            # only when the id isn't known), the same way the removal finds them.
            # Not systemAccounts(): it keeps every account the server has ever
            # seen, so someone already taken off the share in Plex was "found",
            # removeFriend then failed, and the row could never be cleared. That
            # list is only the fallback when plex.tv can't be asked, and then
            # removing would fail anyway.
            on_plex = None
            if can_sign_in(self.services.config):
                try:
                    on_plex = await run_blocking(_is_shared_with, self.services.config,
                                                 tracked_user.plex_user_id, tracked_user.plex_username)
                except Exception as e:
                    logger.warning(f"Could not read Plex shares for {plex_username}: {e}")
            if on_plex is None:
                plex_users = await run_blocking(self.services.plex_server.systemAccounts)
                on_plex = any(u.name == plex_username for u in plex_users)

            if not on_plex:
                return await self._forget_never_joined(tracked_user, removed_by)

            # Collect stats up front (the account must still exist in Tautulli),
            # but do not tell the user anything until the removal has actually
            # succeeded. The DM is titled "Removed from Plex Server", so sending
            # it first meant a failed removal left the user believing they had
            # lost access while they still had it. Same ordering already fixed
            # in the automatic path, _remove_inactive_user.
            stats = await self._get_user_stats(plex_username)

            # Remove from Plex
            try:
                # The account found on the share, never a stored email: that can be
                # stale, and a miss must not read as "already gone".
                gone = not await run_blocking(_remove_friend, self.services.config,
                                              tracked_user.plex_user_id, tracked_user.plex_username)
                if not gone:
                    logger.info(f"Manually removed {plex_username} from Plex by {removed_by}")
            except PlexNotFound as e:
                # Only "already gone" if a fresh look at the share agrees.
                if await run_blocking(_is_shared_with, self.services.config,
                                      tracked_user.plex_user_id, tracked_user.plex_username):
                    logger.error(f"Error removing {plex_username} from Plex: {e}")
                    return False, f"❌ Error removing user from Plex: {str(e)}"
                gone = True
            except Exception as e:
                logger.error(f"Error removing {plex_username} from Plex: {e}")
                return False, f"❌ Error removing user from Plex: {str(e)}"
            if gone:
                # Taken off in Plex since the check above: nothing left to take
                # away, so forget them without a removal DM.
                logger.info(f"{plex_username} was already off the Plex share")
                return await self._forget_never_joined(tracked_user, removed_by)

            # Removal succeeded - now notify and clean up Discord state.
            await self._send_manual_removal_dm(tracked_user, stats, removed_by)
            if tracked_user.discord_id:
                await self._remove_plex_role(tracked_user.discord_id)

            # Drop the tracking row in its own short transaction.
            async with get_session() as session:
                await session.execute(
                    delete(PlexUser).where(PlexUser.id == tracked_user.id)
                )
                await session.commit()

            return True, (
                f"✅ Successfully removed `{plex_username}` from Plex server and tracking database."
                + ("\nUser has been notified via DM." if tracked_user.discord_id else "")
            )

        except Exception as e:
            logger.error(f"Error in manual user removal: {e}", exc_info=True)
            return False, f"❌ Error removing user: {str(e)}"

    async def _forget_never_joined(self, tracked_user: PlexUser, removed_by: str) -> tuple:
        """Removing someone who isn't on the Plex server: they never accepted their
        invite (or already left). Nothing to take away on Plex, so: take back an
        invite still waiting, drop their Plex role in Discord, and forget them. No
        "you've been removed" message; they never had access."""
        from core import plex_invites
        name = tracked_user.plex_username
        cancelled = False
        if tracked_user.plex_email:
            try:
                cancelled = await run_blocking(plex_invites.cancel, self.services.config, tracked_user.plex_email)
            except Exception as e:
                logger.warning(f"Couldn't take back the Plex invite for {name}: {e}")
        if tracked_user.discord_id:
            await self._remove_plex_role(tracked_user.discord_id)
        async with get_session() as session:
            await session.execute(delete(PlexUser).where(PlexUser.id == tracked_user.id))
            await session.commit()
        logger.info(f"{removed_by} removed {name}, who wasn't on Plex" + (" (invite taken back)" if cancelled else ""))
        return True, (f"✅ `{name}` wasn't on Plex (they never accepted the invite, or already left), so Plexbie "
                      "forgot them" + (" and took back their invite." if cancelled else "."))

    async def _send_manual_removal_dm(self, user: PlexUser, stats: Dict[str, Any], removed_by: str):
        """Send DM for manual removal"""
        if not user.discord_id:
            await self._notify_without_discord(
                user, "Your Plex access has ended",
                "An admin removed your access to the household Plex. Thanks for watching! "
                "If you think that's a mistake, ask whoever invited you.",
                f"manual Plex removal for {user.plex_username} by {removed_by}")
            return

        embed = discord.Embed(
            title="⚠️ Removed from Plex Server",
            description=(
                f"You have been removed from the Plex server.\n\n"
                f"If you weren't aware of this or would like more information about why, "
                f"please reach out to **{removed_by}**."
            ),
            color=discord.Color.orange()
        )

        _add_departure_fields(embed, stats)

        embed.set_footer(text="This action was performed by a server administrator")

        await self._dm_tracked(user, f"manual Plex removal for {user.plex_username} by {removed_by}", "manual removal notification", embed=embed)

    # ===== User Linking System =====

    @app_commands.command(name="list-tracked-users", description="List all tracked Plex users in database")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def list_tracked_users(self, interaction: discord.Interaction):
        """List all users being tracked in the database"""
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        try:
            async with get_session() as session:
                result = await session.execute(select(PlexUser))
                tracked_users = result.scalars().all()

            if not tracked_users:
                await interaction.followup.send("No tracked users found.", ephemeral=True)
                return

            # Who currently has access. Deliberately not systemAccounts(): that
            # lists every account the server has ever seen, so a user removed from
            # the share is still in it and would never be flagged - which is the
            # one case this flag exists for.
            with_access = await self._plex_users_with_access()
            can_check_orphans = with_access is not None

            embed = discord.Embed(
                title="📊 Tracked Plex Users",
                description=f"Total: {len(tracked_users)} users in database",
                color=discord.Color.blue()
            )

            # Count orphans across ALL tracked users, not just the visible page -
            # computing it inside the display loop understated the real number.
            orphaned_count = (
                sum(1 for user in tracked_users if user.plex_username not in with_access)
                if can_check_orphans else 0
            )

            shown = tracked_users[:MAX_LISTED_USERS]
            for user in shown:
                discord_info = f"<@{user.discord_id}>" if user.discord_id else "❌ Not linked"
                status_emoji = "🟢" if user.days_inactive < 25 else "🟡" if user.days_inactive < 30 else "🔴"

                # Check if orphaned
                is_orphaned = (
                    can_check_orphans and user.plex_username not in with_access
                )
                if is_orphaned:
                    status_emoji = "⚠️"

                user_info = f"**Plex:** `{user.plex_username}`"
                if is_orphaned:
                    user_info += " ⚠️ **NOT ON PLEX**"
                user_info += f"\n**Discord:** {discord_info}\n"
                user_info += f"**Inactive:** {status_emoji} {user.days_inactive} days"

                if user.last_watched:
                    user_info += f"\n**Last Watch:** <t:{int(ensure_utc(user.last_watched).timestamp())}:R>"

                embed.add_field(
                    name=f"ID: {user.id}",
                    value=user_info,
                    inline=False
                )

            # Footer must state what actually happens. The inactivity check does NOT
            # delete orphans - it logs "skipping (may be new)" and continues - so
            # claiming auto-removal left them to accumulate while the admin was
            # told they were handled.
            notes = []
            if len(tracked_users) > len(shown):
                notes.append(f"Showing first {len(shown)} of {len(tracked_users)}")
            if orphaned_count > 0:
                notes.append(
                    f"⚠️ {orphaned_count} not on Plex - remove with /remove-user "
                    f"(not cleaned up automatically)"
                )
            if not can_check_orphans:
                notes.append(
                    "Could not verify who still has access (PLEX_USERNAME/"
                    "PLEX_PASSWORD unset or plex.tv unreachable) - no orphan flags shown"
                )
            if notes:
                embed.set_footer(text=" · ".join(notes))

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await reply_failure(interaction, logger, "Error listing tracked users", e)

    #: Accounts never listed as needing removal. 1 is the server owner; 0 is Plex's
    #: own /accounts/0 sentinel, which has no name and is not a user at all - it was
    #: being reported for manual removal, which nobody can action.
    PROTECTED_PLEX_ACCOUNT_IDS = (0, 1)

    @staticmethod
    def _plex_account_problems(account) -> list:
        """Why this Plex account is malformed, or an empty list if it is fine."""
        problems = []
        if not account.name or not account.name.strip():
            problems.append("Empty/blank username")
        elif len(account.name) > 100:
            problems.append(f"Username too long ({len(account.name)} chars)")
        return problems

    @app_commands.command(name="list-plex-users", description="List Plex users on the server")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.describe(show="Which accounts to list (default: all)")
    @app_commands.choices(show=[
        app_commands.Choice(name="All accounts", value="all"),
        app_commands.Choice(name="Only malformed accounts", value="invalid"),
    ])
    async def list_plex_users(
        self,
        interaction: discord.Interaction,
        show: Optional[app_commands.Choice[str]] = None,
    ):
        """List Plex accounts, optionally only the malformed ones.

        Replaces a separate /cleanup-plex-users command, which was a strict subset
        of this one: both read systemAccounts and applied the same validity rules,
        and this command already computed the invalid list in order to display it.
        """
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        invalid_only = show is not None and show.value == "invalid"

        try:
            if not self.services.plex_server:
                await interaction.followup.send("❌ Plex server not configured", ephemeral=True)
                return

            plex_users = await run_blocking(self.services.plex_server.systemAccounts)

            valid_users = []
            invalid_users = []

            for user in plex_users:
                user_info = f"**ID:** {user.id}\n**Name:** `{repr(user.name)}`"
                if getattr(user, "email", None):
                    user_info += f"\n**Email:** {user.email}"

                problems = self._plex_account_problems(user)
                # A protected account is reported as-is but never as one to remove:
                # the owner account can legitimately look odd and must not be
                # offered up for deletion.
                if problems and user.id not in self.PROTECTED_PLEX_ACCOUNT_IDS:
                    invalid_users.append(
                        f"{user_info}\n⚠️ **Issues:** {', '.join(problems)}"
                    )
                else:
                    valid_users.append(user_info)

            if invalid_only:
                if not invalid_users:
                    await interaction.followup.send(
                        "✅ No malformed Plex accounts found.", ephemeral=True
                    )
                    return

                embed = discord.Embed(
                    title="⚠️ Malformed Plex Accounts",
                    description=(
                        f"Found {len(invalid_users)} account(s) that should be removed.\n\n"
                        "**Note:** system accounts cannot be deleted through the Plex "
                        "API. Remove them in the Plex web interface:\n"
                        "Settings → Users → [User] → Remove Access"
                    ),
                    color=discord.Color.orange(),
                )
                embed.add_field(
                    name=f"Accounts to remove ({len(invalid_users)} total)",
                    # Clamped: an over-long value is rejected with HTTPException
                    # 400, which made this fail whenever it had findings - and the
                    # long-username case is exactly what it looks for.
                    value=truncate_field("\n\n".join(invalid_users[:10])),
                    inline=False,
                )
                if len(invalid_users) > 10:
                    embed.set_footer(
                        text=f"Showing first 10 of {len(invalid_users)} accounts"
                    )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            embed = discord.Embed(
                title="📋 All Plex Users",
                description=f"Total: {len(plex_users)} accounts",
                color=discord.Color.blue(),
            )

            if invalid_users:
                embed.add_field(
                    name="❌ Malformed (should be removed)",
                    value=truncate_field("\n\n".join(invalid_users[:10])),
                    inline=False,
                )

            if valid_users:
                for i in range(0, min(len(valid_users), 15), 5):
                    chunk = valid_users[i:i + 5]
                    embed.add_field(
                        name=f"✅ Valid ({i + 1}-{i + len(chunk)})",
                        value=truncate_field("\n\n".join(chunk)),
                        inline=False,
                    )

            if invalid_users:
                embed.set_footer(
                    text="Run again with show: Only malformed accounts for removal steps"
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await reply_failure(interaction, logger, "Error listing Plex users", e)

    @app_commands.command(name="manage-links", description="Manage Discord-Plex user links")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def manage_links(self, interaction: discord.Interaction):
        """Open the user link management panel"""
        if not await require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        try:
            # Create the control panel
            view = UserLinkControlPanel(self.bot, self.services)

            embed = discord.Embed(
                title="🔗 User Link Management",
                description="Manage Discord to Plex account links",
                color=discord.Color.blue()
            )

            embed.add_field(
                name="📋 View Links",
                value="See all current Discord-Plex links",
                inline=False
            )

            embed.add_field(
                name="➕ Link User",
                value="Link a Discord user to their Plex account",
                inline=False
            )

            embed.add_field(
                name="➖ Unlink User",
                value="Remove an existing Discord-Plex link",
                inline=False
            )

            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            await reply_failure(interaction, logger, "Error opening manage-links panel", e, prefix="❌ Error opening link management")

    async def link_accounts(self, discord_id: int, plex_username: str) -> tuple:
        """Link a Discord member to a Plex account; starts tracking the account if it wasn't.

        Shared by /manage-links and the website's Manage page. Returns (ok, message).
        """
        try:
            discord_user = await self.bot.fetch_user(int(discord_id))
        except Exception:
            return False, "That Discord account couldn't be found."
        async with get_session() as session:
            taken = (await session.execute(
                select(PlexUser).where(PlexUser.discord_id == int(discord_id), PlexUser.plex_username != plex_username)
            )).scalar_one_or_none()
            if taken:
                return False, f"{discord_user.name} is already linked to {taken.plex_username}. Unlink that first."
            existing = await person_by_name(session, plex_username)
            if existing:
                existing.discord_id = int(discord_id)
                existing.discord_username = discord_user.name
            else:
                session.add(PlexUser(discord_id=int(discord_id), discord_username=discord_user.name,
                                     plex_username=plex_username))
            await session.commit()
        logger.info(f"Linked Discord user {discord_user.name} ({discord_id}) to Plex user {plex_username}")
        return True, f"Linked {plex_username} to {discord_user.name}."

    async def unlink_account(self, plex_username: str) -> tuple:
        """Remove a Discord link, keeping the account tracked. Returns (ok, message)."""
        async with get_session() as session:
            user = await person_by_name(session, plex_username)
            if not user or not user.discord_id:
                return False, f"{plex_username} isn't linked to Discord."
            was = user.discord_username
            user.discord_id = None
            user.discord_username = None
            await session.commit()
        logger.info(f"Unlinked Plex user {plex_username} from Discord user {was}")
        return True, f"Unlinked {plex_username} from {was}."

    async def _get_unlinked_discord_users(self, guild: discord.Guild) -> list:
        """Get Discord users that are not yet linked to a Plex account"""
        async with get_session() as session:
            # Get all linked Discord IDs
            result = await session.execute(
                select(PlexUser.discord_id).where(PlexUser.discord_id.isnot(None))
            )
            linked_ids = {row[0] for row in result.all()}

        # Filter guild members
        unlinked = []
        for member in guild.members:
            if member.bot:
                continue
            if member.id not in linked_ids:
                # Format: "username (nickname)" or just "username" if no nickname
                display = f"{member.name}"
                if member.nick:
                    display += f" ({member.nick})"
                unlinked.append((member.id, display))

        return unlinked[:25]  # Discord limit

    async def _get_unlinked_plex_users(self) -> list:
        """Get Plex/Tautulli users that are not yet linked to a Discord account"""
        # Get users from Tautulli (more reliable usernames than Plex system accounts)
        if not self.services.tautulli.configured:
            return []

        async with get_session() as session:
            # Get Plex usernames that ARE linked to a Discord account
            result = await session.execute(
                select(PlexUser.plex_username).where(PlexUser.discord_id.isnot(None))
            )
            linked_usernames = {row[0].lower() for row in result.all()}  # Case-insensitive

        try:
            tautulli_users = await self.services.tautulli.users()

            unlinked = []
            for user in tautulli_users:
                friendly_name = user.get('friendly_name', '')
                user_id = user.get('user_id', 0)

                # Filter out invalid usernames (empty, too long, or already linked)
                if (friendly_name and
                    friendly_name.strip() and
                    len(friendly_name) <= 100 and
                    friendly_name.lower() not in linked_usernames):
                    # Use tuple: (user_id, display_name, username)
                    unlinked.append((user_id, friendly_name, friendly_name))

            return unlinked[:25]  # Discord limit

        except Exception as e:
            logger.error(f"Error fetching Tautulli users: {e}")
            return []


async def _linked_users() -> list[PlexUser]:
    """Everyone linked to a Discord account, as View Links and Unlink User list them."""
    async with get_session() as session:
        result = await session.execute(
            select(PlexUser).where(PlexUser.discord_id.isnot(None))
        )
        return list(result.scalars().all())


class UserLinkControlPanel(AdminOnlyView):
    """Main control panel for user link management"""

    def __init__(self, bot: commands.Bot, services: BotServices):
        super().__init__(timeout=300)
        self.bot = bot
        self.services = services

    @discord.ui.button(label="View Links", style=discord.ButtonStyle.primary, emoji="📋")
    async def view_links(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show all current links"""
        await interaction.response.defer(ephemeral=True)

        try:
            linked_users = await _linked_users()

            if not linked_users:
                await interaction.followup.send("No linked users found.", ephemeral=True)
                return

            embed = discord.Embed(
                title="📋 Current Discord-Plex Links",
                description=f"Found {len(linked_users)} linked accounts",
                color=discord.Color.blue()
            )

            # Group into fields (max 25)
            for user in linked_users[:25]:
                discord_mention = f"<@{user.discord_id}>"
                embed.add_field(
                    name=f"{user.discord_username or 'Unknown'}",
                    value=f"{discord_mention} ↔️ `{user.plex_username}`",
                    inline=False
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await reply_failure(interaction, logger, "Error viewing links", e)

    @discord.ui.button(label="Link User", style=discord.ButtonStyle.success, emoji="➕")
    async def link_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Start the linking process"""
        await interaction.response.defer(ephemeral=True)

        try:
            # Get bot's cog for helper methods
            cog = self.bot.get_cog("UserMgmtCog")
            if not cog:
                await interaction.followup.send("❌ User management system not loaded", ephemeral=True)
                return

            # Get unlinked users
            guild = interaction.guild
            unlinked_discord = await cog._get_unlinked_discord_users(guild)
            unlinked_plex = await cog._get_unlinked_plex_users()

            if not unlinked_discord:
                await interaction.followup.send("✅ All Discord users are already linked!", ephemeral=True)
                return

            if not unlinked_plex:
                await interaction.followup.send("✅ All Plex users are already linked!", ephemeral=True)
                return

            # Show link selection view
            view = LinkUserView(self.bot, self.services, unlinked_discord, unlinked_plex)

            embed = discord.Embed(
                title="➕ Link Discord User to Plex Account",
                description="Select a Discord user and their corresponding Plex account",
                color=discord.Color.green()
            )

            embed.add_field(
                name="Step 1",
                value="Select the Discord user from the dropdown below",
                inline=False
            )

            embed.add_field(
                name="Step 2",
                value="Select their Plex account from the second dropdown",
                inline=False
            )

            embed.add_field(
                name="Step 3",
                value="Click 'Confirm Link' to save",
                inline=False
            )

            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            await reply_failure(interaction, logger, "Error starting link process", e)

    @discord.ui.button(label="Unlink User", style=discord.ButtonStyle.danger, emoji="➖")
    async def unlink_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Remove an existing link"""
        await interaction.response.defer(ephemeral=True)

        try:
            linked_users = await _linked_users()

            if not linked_users:
                await interaction.followup.send("No linked users to unlink.", ephemeral=True)
                return

            # Create unlink view
            view = UnlinkUserView(self.bot, self.services, linked_users)

            embed = discord.Embed(
                title="➖ Unlink Discord-Plex Account",
                description="Select a user to unlink",
                color=discord.Color.red()
            )

            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        except Exception as e:
            await reply_failure(interaction, logger, "Error starting unlink process", e)


class LinkUserView(AdminOnlyView):
    """View for linking a Discord user to Plex account"""

    def __init__(self, bot: commands.Bot, services: BotServices, discord_users: list, plex_users: list):
        super().__init__(timeout=300)
        self.bot = bot
        self.services = services
        self.selected_discord_id = None
        self.selected_plex_user_id = None
        self.selected_plex_username = None

        # Add Discord user select
        discord_select = DiscordUserSelect(discord_users)
        discord_select.callback = self.discord_user_selected
        self.add_item(discord_select)

        # Add Plex user select
        plex_select = PlexUserSelect(plex_users)
        plex_select.callback = self.plex_user_selected
        self.add_item(plex_select)

    async def discord_user_selected(self, interaction: discord.Interaction):
        """Called when Discord user is selected"""
        select = [item for item in self.children if isinstance(item, DiscordUserSelect)][0]
        self.selected_discord_id = int(select.values[0])

        await interaction.response.send_message(
            f"✅ Discord user selected: <@{self.selected_discord_id}>",
            ephemeral=True
        )

    async def plex_user_selected(self, interaction: discord.Interaction):
        """Called when Plex user is selected"""
        select = [item for item in self.children if isinstance(item, PlexUserSelect)][0]
        self.selected_plex_user_id = int(select.values[0])

        # Look up the Plex username from the Plex server using the ID
        try:
            plex_users = await run_blocking(self.services.plex_server.systemAccounts)
            plex_user = next((u for u in plex_users if u.id == self.selected_plex_user_id), None)
            if plex_user:
                self.selected_plex_username = plex_user.name
            else:
                await interaction.response.send_message(
                    "❌ Error: Could not find Plex user",
                    ephemeral=True
                )
                return
        except Exception as e:
            await reply_failure(interaction, logger, "Error looking up Plex user", e, prefix="❌ Error looking up Plex user")
            return

        await interaction.response.send_message(
            f"✅ Plex user selected: `{self.selected_plex_username}`",
            ephemeral=True
        )

    @discord.ui.button(label="Confirm Link", style=discord.ButtonStyle.success, row=2)
    async def confirm_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm and create the link"""
        if not self.selected_discord_id or not self.selected_plex_username:
            await interaction.response.send_message(
                "❌ Please select both a Discord user and a Plex account first",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            cog = self.bot.get_cog("UserMgmtCog")
            ok, message = await cog.link_accounts(self.selected_discord_id, self.selected_plex_username)
            if not ok:
                await interaction.followup.send(f"❌ {message}", ephemeral=True)
                return
            discord_user = await self.bot.fetch_user(self.selected_discord_id)

            # Success message
            embed = discord.Embed(
                title="✅ Link Created Successfully",
                description="Discord user has been linked to Plex account",
                color=discord.Color.green()
            )

            embed.add_field(
                name="Discord User",
                value=f"<@{self.selected_discord_id}> ({discord_user.name})",
                inline=True
            )

            embed.add_field(
                name="Plex Account",
                value=f"`{self.selected_plex_username}`",
                inline=True
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

            await disable_and_refresh(self, interaction)

            logger.info(f"Linked Discord user {discord_user.name} ({self.selected_discord_id}) to Plex user {self.selected_plex_username}")

        except Exception as e:
            await reply_failure(interaction, logger, "Error creating link", e, prefix="❌ Error creating link")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel the linking process"""
        await interaction.response.send_message("❌ Linking cancelled", ephemeral=True)

        await disable_and_refresh(self, interaction, quiet=True)


class UnlinkUserView(AdminOnlyView):
    """View for unlinking a user"""

    def __init__(self, bot: commands.Bot, services: BotServices, linked_users: list):
        super().__init__(timeout=300)
        self.bot = bot
        self.services = services
        self.selected_user_id = None

        # Create select options
        options = []
        for user in linked_users[:25]:
            label = f"{user.discord_username} ↔ {user.plex_username}"
            options.append(discord.SelectOption(
                label=label[:100],  # Discord max length
                value=str(user.id),
                description=f"Discord ID: {user.discord_id}"[:100]
            ))

        select = discord.ui.Select(
            placeholder="Select a linked user to unlink",
            options=options
        )
        select.callback = self.user_selected
        self.add_item(select)

    async def user_selected(self, interaction: discord.Interaction):
        """Called when user is selected"""
        # Find the select menu among children
        select = next((item for item in self.children if isinstance(item, discord.ui.Select)), None)
        if select and select.values:
            self.selected_user_id = int(select.values[0])

        await interaction.response.send_message(
            "✅ User selected. Click 'Confirm Unlink' to remove the link.",
            ephemeral=True
        )

    @discord.ui.button(label="Confirm Unlink", style=discord.ButtonStyle.danger, row=1)
    async def confirm_unlink(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm and remove the link"""
        if not self.selected_user_id:
            await interaction.response.send_message(
                "❌ Please select a user first",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            # Do the lookup and the write, then close before replying: the
            # not-found reply used to be sent with the read transaction still open.
            async with get_session() as session:
                result = await session.execute(
                    select(PlexUser).where(PlexUser.id == self.selected_user_id)
                )
                user = result.scalar_one_or_none()

                if user:
                    # Store info for confirmation message
                    discord_username = user.discord_username
                    plex_username = user.plex_username
                    discord_id = user.discord_id

                    # Remove Discord link (keep Plex user entry for tracking)
                    user.discord_id = None
                    user.discord_username = None

                    await session.commit()

            if not user:
                await interaction.followup.send("❌ User not found", ephemeral=True)
                return

            # Success message
            embed = discord.Embed(
                title="✅ Link Removed Successfully",
                description="Discord-Plex link has been removed",
                color=discord.Color.orange()
            )

            embed.add_field(
                name="Discord User",
                value=f"<@{discord_id}> ({discord_username})",
                inline=True
            )

            embed.add_field(
                name="Plex Account",
                value=f"`{plex_username}`",
                inline=True
            )

            await interaction.followup.send(embed=embed, ephemeral=True)

            await disable_and_refresh(self, interaction)

            logger.info(f"Unlinked Discord user {discord_username} ({discord_id}) from Plex user {plex_username}")

        except Exception as e:
            await reply_failure(interaction, logger, "Error removing link", e, prefix="❌ Error removing link")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel the unlinking process"""
        await interaction.response.send_message("❌ Unlinking cancelled", ephemeral=True)

        await disable_and_refresh(self, interaction, quiet=True)


class DiscordUserSelect(discord.ui.Select):
    """Select menu for Discord users"""

    def __init__(self, users: list):
        options = []
        for user_id, display_name in users:
            options.append(discord.SelectOption(
                label=display_name[:100],
                value=str(user_id),
                description=f"ID: {user_id}"
            ))

        super().__init__(
            placeholder="Select Discord user",
            options=options,
            row=0
        )


class PlexUserSelect(discord.ui.Select):
    """Select menu for Plex users"""

    def __init__(self, users: list):
        options = []
        for plex_user_id, display_name, plex_username in users:
            options.append(discord.SelectOption(
                label=display_name[:100],
                value=str(plex_user_id)  # Use ID for uniqueness
            ))

        super().__init__(
            placeholder="Select Plex account",
            options=options,
            row=1
        )


