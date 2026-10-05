# path: core/role_order.py
"""Can Plexbie hand out its roles?

Discord lets a bot give or take a role only if that role sits *below* the bot's
own highest role in Server Settings > Roles. Roles a bot creates land at the
bottom of the list, alongside (not below) the bot's own role, so a freshly set
up server needs one drag: Plexbie's role to the top. Only a person can do it -
a bot can't move its own role up.
"""
from typing import Dict, Iterable, List

#: The settings naming roles Plexbie gives out, and how to describe each.
HANDED_OUT = {"PLEX_MEMBER_ROLE_ID": "Plex Member", "ARRIVALS_ROLE_ID": "New on Plex", "ADMIN_ROLE_ID": "Plexbie Admin"}


def out_of_reach(bot_positions: Iterable[int], roles: Dict[str, dict], wanted: Iterable[str]) -> List[str]:
    """Names of the `wanted` role ids (that exist) the bot can't give: not below
    its highest role. `roles` maps id -> {"name", "position"}."""
    top = max(bot_positions, default=0)
    return [roles[r]["name"] for r in wanted if r in roles and roles[r]["position"] >= top]


def fix_text(names: List[str], bot_name: str = "Plexbie") -> str:
    listed = ", ".join(names)
    return (f"In Discord, open Server Settings > Roles and drag the {bot_name} role above {listed}. "
            "Discord only lets a bot give out roles listed below its own.")
