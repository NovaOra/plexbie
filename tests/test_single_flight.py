# path: tests/test_single_flight.py
"""Approval buttons must not act twice on one click-happy admin.

Disabling the buttons and editing the message is not a guard: it is a client-side
render that takes a round-trip, and Discord can deliver a second interaction
before the first edit lands. The media approve path relied on exactly that (and
in fact did not even disable them until after submitting), so a double-click
submitted the same request to Seerr twice.

Also covers an indentation bug found alongside it: BookAdminApprovalView assigned
self.user_id and self.services at the end of on_error instead of in __init__, so
the constructor silently dropped both arguments and the error handler raised
NameError on two free names - meaning a failing button reported nothing useful.
"""
import ast
import asyncio
import pathlib

import conftest  # noqa: F401

import discord
from core.permissions import AdminActionView, single_flight

ROOT = pathlib.Path(conftest.PROJECT_ROOT)


class _FakeResponse:
    def __init__(self):
        self.sent = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content, ephemeral=False):
        self.sent.append(content)
        self._done = True


class _FakeInteraction:
    def __init__(self, message_id=1, user="admin"):
        self.message = type("M", (), {"id": message_id})()
        self.user = user
        self.response = _FakeResponse()


# ===================================================================
# the guard itself
# ===================================================================

def test_claim_succeeds_once_then_refuses():
    class V(AdminActionView):
        pass

    view = V()
    first, second = _FakeInteraction(), _FakeInteraction()

    assert asyncio.run(view.claim(first)) is True
    assert asyncio.run(view.claim(second)) is False
    assert second.response.sent, "the refused interaction must be answered"
    assert "already being handled" in second.response.sent[0]


def test_release_frees_the_message():
    class V(AdminActionView):
        pass

    view = V()
    a = _FakeInteraction()
    assert asyncio.run(view.claim(a)) is True
    view.release(a)
    assert asyncio.run(view.claim(_FakeInteraction())) is True


def test_a_different_message_is_not_blocked():
    class V(AdminActionView):
        pass

    view = V()
    assert asyncio.run(view.claim(_FakeInteraction(message_id=1))) is True
    assert asyncio.run(view.claim(_FakeInteraction(message_id=2))) is True


def test_the_registry_is_per_subclass():
    """A book request and a media request must not collide on one registry."""
    class A(AdminActionView):
        pass

    class B(AdminActionView):
        pass

    assert A._in_flight is not B._in_flight
    assert asyncio.run(A().claim(_FakeInteraction(message_id=7))) is True
    assert asyncio.run(B().claim(_FakeInteraction(message_id=7))) is True


def test_the_guard_spans_separate_instances_of_one_class():
    """Each request is posted with a fresh view, but after a restart one shared
    instance handles them all - the guard has to hold either way.
    """
    class V(AdminActionView):
        pass

    assert asyncio.run(V().claim(_FakeInteraction(message_id=9))) is True
    assert asyncio.run(V().claim(_FakeInteraction(message_id=9))) is False


def test_release_without_claim_is_harmless():
    class V(AdminActionView):
        pass

    V().release(_FakeInteraction())   # must not raise


def test_an_interaction_with_no_message_is_allowed_through():
    class V(AdminActionView):
        pass

    bare = type("I", (), {"message": None, "user": "x"})()
    assert asyncio.run(V().claim(bare)) is True


# ===================================================================
# the decorator
# ===================================================================

def test_two_concurrent_clicks_run_the_body_once():
    calls = []

    class V(AdminActionView):
        @single_flight
        async def act(self, interaction, button):
            calls.append("body")
            await asyncio.sleep(0.02)      # the window a real submission opens

    view = V()

    async def scenario():
        await asyncio.gather(
            view.act(_FakeInteraction(), None),
            view.act(_FakeInteraction(), None),
        )

    asyncio.run(scenario())
    assert calls == ["body"], f"the body ran {len(calls)} times; expected once"


def test_the_reservation_is_released_even_when_the_body_raises():
    class V(AdminActionView):
        @single_flight
        async def act(self, interaction, button):
            raise RuntimeError("submission failed")

    view = V()
    raised = False
    try:
        asyncio.run(view.act(_FakeInteraction(), None))
    except RuntimeError:
        raised = True
    assert raised, "the error must still propagate to discord.py's on_error"
    assert asyncio.run(view.claim(_FakeInteraction())) is True, (
        "a failed attempt must not lock the message out of a retry"
    )


def test_sequential_clicks_both_run():
    """The guard is per in-flight action, not a one-shot latch."""
    calls = []

    class V(AdminActionView):
        @single_flight
        async def act(self, interaction, button):
            calls.append(1)

    view = V()
    asyncio.run(view.act(_FakeInteraction(), None))
    asyncio.run(view.act(_FakeInteraction(), None))
    assert len(calls) == 2


# ===================================================================
# the constructor / on_error bug
# ===================================================================

def test_book_view_keeps_its_constructor_arguments():
    from plugins.media_requests.cog import BookAdminApprovalView

    services = object()
    view = BookAdminApprovalView({"title": "X"}, 4242, services)
    assert view.user_id == 4242, "__init__ dropped user_id"
    assert view.services is services, "__init__ dropped services"


def test_book_view_on_error_reports_instead_of_raising():
    """The handler meant to surface a failure used to raise NameError itself."""
    from plugins.media_requests.cog import BookAdminApprovalView

    view = BookAdminApprovalView({"title": "X"}, 1, object())
    interaction = _FakeInteraction()
    item = type("I", (), {"custom_id": "approve_book_request"})()

    asyncio.run(view.on_error(interaction, RuntimeError("original failure"), item))
    assert interaction.response.sent, "the admin was told nothing"
    assert "original failure" in interaction.response.sent[0], (
        "the real error must reach the admin, not be lost behind a NameError"
    )


# ===================================================================
# pattern: no approval button may skip the guard
# ===================================================================

#: Calls that reach outside the process and must not happen twice for one click.
SIDE_EFFECTING = (
    "_submit_to_download",      # NZBHydra -> SABnzbd
    "_submit_to_seerr",
    "_fulfill_request",
    "removeFriend",
    "inviteFriend",
    "_restore_existing_request_monitoring",
    "_approve_core",            # book approval, shared by the button and the website
)


def _buttons_with_side_effects():
    """(file, callback) for every button whose body reaches an external service.

    Keyed on what the callback *does*, not on which base class the view happens to
    use. An earlier version of this test looked for AdminActionView subclasses,
    which meant reverting the base class made it find nothing and pass vacuously.
    """
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if any(s in rel for s in ("sync-conflict", "tests/", "backups/")):
            continue
        text = path.read_text()
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = []
            is_button = False
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call):
                    f = dec.func
                    attr = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if attr == "button":
                        is_button = True
                    decorators.append(attr)
                else:
                    decorators.append(dec.attr if isinstance(dec, ast.Attribute)
                                      else getattr(dec, "id", ""))
            if not is_button:
                continue
            body = ast.get_source_segment(text, node) or ""
            if any(call in body for call in SIDE_EFFECTING):
                yield rel, node.name, decorators


def test_every_side_effecting_button_is_single_flight():
    """Submitting to Seerr or queueing a download twice is a real cost.

    The media approve button relied on disabling the buttons, which is a
    client-side render - and it did not even do that until after the submission.
    """
    unguarded = [
        f"{rel}:{name}"
        for rel, name, decorators in _buttons_with_side_effects()
        if "single_flight" not in decorators
    ]
    assert unguarded == [], (
        "these buttons reach an external service and can be double-clicked; add "
        "@single_flight under @discord.ui.button:\n  " + "\n  ".join(unguarded)
    )


def test_the_pattern_test_is_actually_looking_at_something():
    """Guard against the audit silently matching nothing and passing vacuously."""
    found = list(_buttons_with_side_effects())
    assert len(found) >= 2, (
        f"expected at least the book and media approve buttons, found {len(found)}: "
        f"{[f'{r}:{n}' for r, n, _ in found]}"
    )
