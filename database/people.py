# path: database/people.py
"""Finding a tracked person (a PlexUser row), the same way everywhere.

Each takes the caller's session, so a lookup can sit inside the transaction
that goes on to change the row.
"""
from typing import List, Optional

from sqlalchemy import select

from plugins.user_mgmt.models import PlexUser


async def person_by_name(session, plex_name: Optional[str]) -> Optional[PlexUser]:
    """The row tracked under that Plex username (names are unique)."""
    if not plex_name:
        return None
    return (await session.execute(select(PlexUser).where(PlexUser.plex_username == plex_name))).scalars().first()


async def person_by_discord(session, discord_id: Optional[int]) -> Optional[PlexUser]:
    """The row linked to that Discord account."""
    if not discord_id:
        return None
    return (await session.execute(select(PlexUser).where(PlexUser.discord_id == int(discord_id)))).scalars().first()


async def people_for_account(session, plex_account_id) -> List[PlexUser]:
    """Every row claiming that Plex account id (more than one is a conflict for an admin)."""
    if not str(plex_account_id or "").isdigit():
        return []
    return list((await session.execute(select(PlexUser).where(PlexUser.plex_user_id == int(plex_account_id)))).scalars().all())


async def sole_person_for_account(session, plex_account_id) -> Optional[PlexUser]:
    """The one row for that Plex account id; None when there's none, or a conflict."""
    rows = await people_for_account(session, plex_account_id)
    return rows[0] if len(rows) == 1 else None
