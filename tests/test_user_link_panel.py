# path: tests/test_user_link_panel.py
"""The /manage-links panel: who View Links and Unlink User list.

Both buttons show the people linked to Discord, so both read that list through
one helper. Each keeps its own message for an empty list.
"""
import asyncio
import pathlib
import tempfile

import conftest  # noqa: F401


class _Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, *, embed=None, view=None, ephemeral=False):
        self.sent.append((content, embed, view))


class _Response:
    async def defer(self, ephemeral=False, thinking=False):
        pass


class _Interaction:
    def __init__(self):
        self.response = _Response()
        self.followup = _Followup()


_PEOPLE = [
    {"plex_username": "alice", "discord_id": 11, "discord_username": "alice#1"},
    {"plex_username": "bob", "discord_id": None, "discord_username": None},
    {"plex_username": "cara", "discord_id": 33, "discord_username": "cara#3"},
]


def _with_database(people, scenario):
    """Run scenario(module) against a fresh database holding these people."""
    import database.session as session_module
    from plugins.user_mgmt import cog as module
    from plugins.user_mgmt.models import PlexUser

    db = pathlib.Path(tempfile.mkdtemp()) / "plexbie.db"

    async def run():
        if session_module.engine is not None:
            await session_module.engine.dispose()
        await session_module.init_database(f"sqlite:///{db}")
        try:
            async with session_module.get_session() as session:
                for spec in people:
                    session.add(PlexUser(**spec))
                await session.commit()
            return await scenario(module)
        finally:
            await session_module.engine.dispose()

    return asyncio.run(run())


def _press(module, button):
    """Press one panel button; returns what it sent."""
    async def go():
        panel = module.UserLinkControlPanel(None, None)
        interaction = _Interaction()
        await getattr(module.UserLinkControlPanel, button)(panel, interaction, None)
        return interaction.followup.sent
    return go()


def test_linked_users_are_only_the_people_linked_to_discord():
    async def scenario(module):
        return sorted(u.plex_username for u in await module._linked_users())

    assert _with_database(_PEOPLE, scenario) == ["alice", "cara"]


def test_both_buttons_list_the_same_linked_users():
    """View Links and Unlink User read the one list, not a query each."""
    async def scenario(module):
        reads = []
        real = module._linked_users

        async def counted():
            reads.append(1)
            return await real()

        module._linked_users = counted
        try:
            (_, embed, _), = await _press(module, "view_links")
            (_, _, view), = await _press(module, "unlink_user")
        finally:
            module._linked_users = real
        return reads, embed, view

    reads, embed, view = _with_database(_PEOPLE, scenario)
    assert len(reads) == 2
    assert [f.value.split("`")[1] for f in embed.fields] == ["alice", "cara"]
    options = next(c for c in view.children if hasattr(c, "options")).options
    assert [o.label.split(" ↔ ")[1] for o in options] == ["alice", "cara"]


def test_each_button_keeps_its_own_empty_message():
    nobody = [{"plex_username": "bob", "discord_id": None, "discord_username": None}]

    async def scenario(module):
        return await _press(module, "view_links"), await _press(module, "unlink_user")

    view_links, unlink = _with_database(nobody, scenario)
    assert view_links == [("No linked users found.", None, None)]
    assert unlink == [("No linked users to unlink.", None, None)]
