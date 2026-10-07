# path: tests/test_permissions.py
"""Authorization gating for commands and interactive views.

Regression coverage for: no view in the codebase defined interaction_check, so
every persistent approval view dispatched to whoever clicked it. Anyone able to
see the admin channel could grant Plex access or approve requests.
"""
import asyncio

import conftest  # noqa: F401
from helpers import (
    FakeInteraction, FakeMember, FakeRole, FakeUser, NoIdsConfig, PermConfig,
)

from core.permissions import AdminOnlyView, is_bot_admin, require_admin


# --- is_bot_admin: the three ways in, and the ways that must stay out ---

def test_guild_administrator_is_admin():
    assert is_bot_admin(FakeInteraction(FakeMember(1, administrator=True), PermConfig)) is True


def test_configured_admin_role_is_admin():
    member = FakeMember(2, roles=[FakeRole(PermConfig.admin_role_id)])
    assert is_bot_admin(FakeInteraction(member, PermConfig)) is True


def test_bot_owner_is_admin_without_perms_or_roles():
    """The lockout safety net: owner passes even with nothing else granted."""
    owner = FakeMember(PermConfig.bot_owner_id)
    assert is_bot_admin(FakeInteraction(owner, PermConfig)) is True


def test_plain_member_is_denied():
    assert is_bot_admin(FakeInteraction(FakeMember(3), PermConfig)) is False


def test_unrelated_role_is_denied():
    member = FakeMember(4, roles=[FakeRole(555)])
    assert is_bot_admin(FakeInteraction(member, PermConfig)) is False


def test_dm_user_without_guild_permissions_is_denied():
    """discord.User has no guild_permissions; must fail closed, not AttributeError."""
    assert is_bot_admin(FakeInteraction(FakeUser(5), PermConfig)) is False


def test_denied_when_no_ids_configured():
    """Missing config must not become an implicit allow."""
    assert is_bot_admin(FakeInteraction(FakeMember(6), NoIdsConfig)) is False


def test_denied_when_client_has_no_services():
    """A view reached without a resolvable config still fails closed."""
    interaction = FakeInteraction(FakeMember(7), PermConfig)
    interaction.client = object()  # no .services at all
    assert is_bot_admin(interaction) is False


# --- require_admin: guard used inside command bodies ---

def test_require_admin_allows_admin_silently():
    interaction = FakeInteraction(FakeMember(8, administrator=True), PermConfig)
    assert asyncio.run(require_admin(interaction)) is True
    assert interaction.refusals == []


def test_require_admin_denies_and_explains():
    interaction = FakeInteraction(FakeMember(9), PermConfig)
    assert asyncio.run(require_admin(interaction)) is False
    assert len(interaction.refusals) == 1


def test_require_admin_uses_followup_when_already_responded():
    """After defer(), a refusal must go via followup or it is never delivered."""
    interaction = FakeInteraction(FakeMember(10), PermConfig, response_done=True)
    assert asyncio.run(require_admin(interaction)) is False
    assert interaction.followup.sent and not interaction.response.sent


# --- AdminOnlyView.interaction_check: the gate discord.py calls pre-dispatch ---

def test_view_gate_blocks_non_admin():
    view = AdminOnlyView()
    interaction = FakeInteraction(FakeMember(11), PermConfig)
    assert asyncio.run(view.interaction_check(interaction)) is False
    assert len(interaction.refusals) == 1


def test_view_gate_allows_admin():
    view = AdminOnlyView()
    interaction = FakeInteraction(FakeMember(12, administrator=True), PermConfig)
    assert asyncio.run(view.interaction_check(interaction)) is True


# --- admin_only: a button or modal that only admins may press ---

def test_admin_only_refuses_before_the_callback_runs():
    from core.permissions import admin_only
    ran = []

    class Button:
        @admin_only
        async def callback(self, interaction):
            ran.append(interaction.user.id)

    member = FakeInteraction(FakeMember(31), PermConfig)
    admin = FakeInteraction(FakeMember(32, administrator=True), PermConfig)
    asyncio.run(Button().callback(member))
    asyncio.run(Button().callback(admin))
    assert ran == [32] and len(member.refusals) == 1 and admin.refusals == []


def test_the_dm_inbox_buttons_answer_admins_only():
    """Reply, Add to their ticket, Done and the reply box all carry the shared gate."""
    from core.permissions import admin_only
    from portal import inbox

    async def press():
        refused = []
        for item, run in ((inbox.ReplyButton("d7"), "callback"), (inbox.TicketButton("20261007T101500000000-abcdef"), "callback"),
                          (inbox.DoneButton("d7"), "callback"), (inbox.ReplyModal("d7"), "on_submit")):
            interaction = FakeInteraction(FakeMember(33), PermConfig)
            await getattr(item, run)(interaction)
            refused.append(len(interaction.refusals))
        return refused

    assert asyncio.run(press()) == [1, 1, 1, 1]
    gate = admin_only(lambda self, interaction: None).__code__
    for cls, run in ((inbox.ReplyButton, "callback"), (inbox.TicketButton, "callback"),
                     (inbox.DoneButton, "callback"), (inbox.ReplyModal, "on_submit")):
        assert getattr(cls, run).__code__ is gate, f"{cls.__name__}.{run} isn't wrapped in admin_only"


# --- RequesterOnlyView: the /request steps answer only the member who started them ---

def test_request_steps_refuse_anyone_but_the_requester():
    from utils.views import RequesterOnlyView
    from plugins.media_requests.cog import BookFormatView, MediaSelectView, MediaTypeSelectView, SeasonSelectionView

    async def check(member):
        view = RequesterOnlyView()
        view.user_id = 21
        interaction = FakeInteraction(member, PermConfig)
        return await view.interaction_check(interaction), interaction.refusals

    allowed, quiet = asyncio.run(check(FakeMember(21)))
    refused, told = asyncio.run(check(FakeMember(22, administrator=True)))
    assert (allowed, quiet) == (True, [])
    assert refused is False and len(told) == 1, "even an admin can't press someone else's request"
    for cls in (MediaTypeSelectView, MediaSelectView, SeasonSelectionView, BookFormatView):
        assert issubclass(cls, RequesterOnlyView), cls.__name__


# --- every privileged view must actually inherit the gate ---

def test_all_privileged_views_are_gated():
    """The core regression. Each of these performs a privileged action:
    granting Plex access, approving requests, or toggling the file-deleting job.
    """
    from plugins.media_cleanup.cog import CleanupControlPanel, CleanupSettingsView
    from plugins.media_requests.cog import AdminApprovalView, BookAdminApprovalView
    from plugins.user_invites.cog import PlexInviteApprovalView

    privileged = [
        PlexInviteApprovalView,   # inviteFriend -> real Plex library access
        AdminApprovalView,        # Seerr submit + Sonarr/Radarr monitoring
        BookAdminApprovalView,    # NZBHydra/SABnzbd downloads
        CleanupControlPanel,      # runs the deletion scan
        CleanupSettingsView,      # enables/disables the deletion job
    ]
    for cls in privileged:
        assert issubclass(cls, AdminOnlyView), f"{cls.__name__} is not admin-gated"
        assert cls.interaction_check is AdminOnlyView.interaction_check, (
            f"{cls.__name__} overrides interaction_check and may bypass the gate"
        )


def test_no_view_relies_on_ephemeral_delivery_alone():
    """Ephemeral delivery is not a permission boundary.

    CleanupSettingsView is only ever sent ephemerally, which made it look safe.
    It must still carry its own gate so a future non-ephemeral send, or reuse as
    a persistent view, does not silently expose it.
    """
    from plugins.media_cleanup.cog import CleanupSettingsView

    view = CleanupSettingsView(cog=None)
    interaction = FakeInteraction(FakeMember(13), PermConfig)
    assert asyncio.run(view.interaction_check(interaction)) is False
