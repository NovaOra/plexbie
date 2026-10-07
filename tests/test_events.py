# path: tests/test_events.py
"""Seerr and Tautulli webhook events, and connecting them (core/webhook_connect)."""
import asyncio
import json
import pathlib
import tempfile

import conftest  # noqa: F401
from helpers import FakeServices

from core.config import Config


# ---------------------------------------------------------------- Tautulli

class _Calls(list):
    def hook(self, name):
        async def record(*args, **kwargs):
            self.append((name, args))
        return staticmethod(record)


def _tautulli(cogs):
    from webhooks import tautulli_handler as module

    class Bot:
        def get_cog(self, name):
            return cogs.get(name)

    events = module.TautulliEvents(Bot())

    async def no_kv(*a, **k):
        return None
    module.kv_set = no_kv
    return module, events


def test_tautulli_playback_refreshes_now_watching_and_clears_warnings():
    calls = _Calls()

    class Watch:
        refresh_now_watching = calls.hook("now")
        refresh_boards = calls.hook("boards")
        def events_connected(self):
            calls.append(("slow", ()))

    class Users:
        on_playback = calls.hook("playback")
        def events_connected(self):
            calls.append(("slow-users", ()))

    module, events = _tautulli({"WatchTrackingCog": Watch(), "UserMgmtCog": Users()})

    async def go():
        await events.dispatch("play", {"user": "pat", "email": "p@x.y", "user_id": "9"})
        await events.dispatch("pause", {"user": "pat"})       # same burst: one refresh
        await asyncio.sleep(2.2)
    asyncio.run(go())
    names = [c[0] for c in calls]
    assert names.count("now") == 1, names
    assert ("playback", ("pat", "p@x.y", "9")) in calls
    assert "slow" in names and "slow-users" in names, "the polls should slow down once events arrive"
    assert "boards" not in names, "boards refresh on stop/watched, not play"


def test_tautulli_plex_down_alerts_at_once():
    calls = _Calls()

    class Health:
        report_event = calls.hook("health")

    module, events = _tautulli({"ServiceHealthCog": Health()})
    asyncio.run(events.dispatch("intdown", {}))
    asyncio.run(events.dispatch("intup", {}))
    assert calls[0][0] == "health" and calls[0][1][:2] == ("plex", True)
    assert calls[1][1][:2] == ("plex", False)


# ---------------------------------------------------------------- Seerr

def _seerr_world():
    from database import session as session_module
    from plugins.media_requests import cog as requests_cog
    from webhooks import seerr_handler as module

    db = pathlib.Path(tempfile.mkdtemp()) / "events.db"
    tracked = []

    async def fake_track(self):
        tracked.append(self.media.get("id"))
    requests_cog.AdminApprovalView._register_with_tracking = fake_track

    class Bot:
        services = FakeServices(Config())
        portal_actions = None

        def get_channel(self, _):
            return None

        def get_cog(self, _):
            return None

        def get_user(self, _):
            return None

    return module, session_module, db, Bot(), tracked


def _event(kind, rid, tmdb, media_type="tv", seasons="2"):
    return {"notification_type": kind, "subject": "The Simpsons (1989)", "message": "",
            "media": {"media_type": media_type, "tmdbId": str(tmdb), "status": "PENDING"},
            "request": {"request_id": str(rid), "requestedBy_username": "Robin", "requestedBy_email": "",
                        "requestedBy_settings_discordId": ""},
            "extra": [{"name": "Requested Seasons", "value": seasons}]}


def test_a_request_made_in_seerr_becomes_a_plexbie_request_and_its_decision_lands():
    module, session_module, db, bot, tracked = _seerr_world()
    from database.request_store import get_request

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            events = module.SeerrEvents(bot)
            await events.dispatch("MEDIA_PENDING", _event("MEDIA_PENDING", 41, 456))
            first = await get_request(41)
            await events.dispatch("MEDIA_APPROVED", _event("MEDIA_APPROVED", 41, 456))
            await events.dispatch("MEDIA_APPROVED", _event("MEDIA_APPROVED", 41, 456))   # repeat: no-op
            return first, await get_request(41)
        finally:
            await session_module.engine.dispose()
    first, after = asyncio.run(go())
    assert first["source"] == "seerr" and first["status"] == "pending"
    assert first["seasons"] == [2] and first["media"]["name"] == "The Simpsons"
    assert first["requester_name"] == "Robin"
    assert after["status"] == "approved" and after["resolved_by"] == "Seerr"
    assert tracked == [456], "approved: registered once for arrival notices"


def test_plexbies_own_request_echoed_by_seerr_is_not_mirrored():
    module, session_module, db, bot, tracked = _seerr_world()
    from database.request_store import all_requests, get_request, save_request

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            await save_request(1234567890123, user_id=5, media={"id": 789, "media_type": "movie", "title": "Film"})
            module.remember_submission("movie", 789)
            await module.SeerrEvents(bot).dispatch("MEDIA_AUTO_APPROVED", _event("MEDIA_AUTO_APPROVED", 77, 789, "movie"))
            return await all_requests(), await get_request(1234567890123)
        finally:
            await session_module.engine.dispose()
    everything, own = asyncio.run(go())
    assert list(everything) == ["1234567890123"]
    assert own["overseerr_request_id"] == 77, "Plexbie keeps Seerr's number for its own request"


def test_a_failed_download_opens_a_help_request():
    module, session_module, db, bot, tracked = _seerr_world()
    from portal import help as helpdesk

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            events = module.SeerrEvents(bot)
            await events.dispatch("MEDIA_AUTO_APPROVED", _event("MEDIA_AUTO_APPROVED", 50, 111))
            failed = _event("MEDIA_FAILED", 50, 111)
            failed["message"] = "Sonarr said no"
            await events.dispatch("MEDIA_FAILED", failed)
            await events.dispatch("MEDIA_FAILED", failed)      # once is enough
            return await helpdesk.all_help()
        finally:
            await session_module.engine.dispose()
    helps = asyncio.run(go())
    assert len(helps) == 1 and helps[0]["request"] == "50"
    assert "Sonarr said no" in helps[0]["note"] and helps[0]["status"] == "open"


def _posted(payload):
    """A Seerr delivery as the handler gets it, with the body already read."""
    class Posted(dict):
        async def json(self):
            return json.loads(self["_validated_body"])
    return Posted(_validated_body=payload if isinstance(payload, bytes) else json.dumps(payload).encode())


def test_a_seerr_event_that_fails_inside_plexbie_is_still_answered_200():
    """Seerr retries and then disables a webhook that keeps failing: a fault on
    Plexbie's side is logged, not handed back to Seerr."""
    module, session_module, db, bot, tracked = _seerr_world()
    events = module.SeerrEvents(bot)

    async def broken(kind, data):
        raise RuntimeError("database is locked")
    events.dispatch = broken
    answer = asyncio.run(events.handle(_posted(_event("MEDIA_PENDING", 60, 222))))
    assert answer.status == 200


def test_a_seerr_body_that_isnt_a_json_object_gets_a_400_and_is_not_dispatched():
    module, session_module, db, bot, tracked = _seerr_world()
    events = module.SeerrEvents(bot)
    seen = []

    async def record(kind, data):
        seen.append(kind)
    events.dispatch = record
    for body in (b"garbage", b"[]"):
        assert asyncio.run(events.handle(_posted(body))).status == 400, body
    assert seen == []


def test_an_echo_after_the_own_window_is_mirrored_as_a_new_request():
    """Past OWN_WINDOW Plexbie no longer recognises its own submission, so Seerr's
    word stands: the request is taken as one made in Seerr."""
    module, session_module, db, bot, tracked = _seerr_world()
    from database.request_store import all_requests, get_request, save_request

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            await save_request(1234567890124, user_id=5, media={"id": 790, "media_type": "movie", "title": "Film"})
            module.remember_submission("movie", 790)
            module._OWN[("movie", 790)] -= module.OWN_WINDOW + 1
            await module.SeerrEvents(bot).dispatch("MEDIA_AUTO_APPROVED", _event("MEDIA_AUTO_APPROVED", 78, 790, "movie"))
            return await all_requests(), await get_request(1234567890124)
        finally:
            module._OWN.pop(("movie", 790), None)
            await session_module.engine.dispose()
    everything, own = asyncio.run(go())
    assert sorted(everything) == ["1234567890124", "78"]
    assert everything["78"]["source"] == "seerr" and everything["78"]["status"] == "approved"
    assert not own.get("overseerr_request_id"), "an old submission isn't matched to Seerr's request"


def test_a_seerr_event_without_a_request_number_saves_nothing():
    module, session_module, db, bot, tracked = _seerr_world()
    from database.request_store import all_requests

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            events = module.SeerrEvents(bot)
            for kind in ("MEDIA_PENDING", "MEDIA_AUTO_APPROVED", "MEDIA_FAILED"):
                event = _event(kind, 0, 333)
                event["request"]["request_id"] = ""
                await events.dispatch(kind, event)
                del event["request"]["request_id"]
                await events.dispatch(kind, event)
            no_request = _event("MEDIA_PENDING", 61, 333)
            del no_request["request"]
            await events.dispatch("MEDIA_PENDING", no_request)
            return await all_requests()
        finally:
            await session_module.engine.dispose()
    assert asyncio.run(go()) == {}


# ---------------------------------------------------------------- connecting

def test_new_secrets_only_fill_the_gaps():
    from core.webhook_connect import SECRET_KEYS, new_secrets
    got = new_secrets({"SONARR_WEBHOOK_SECRET": "keep"})
    assert "SONARR_WEBHOOK_SECRET" not in got and set(got) == set(SECRET_KEYS) - {"SONARR_WEBHOOK_SECRET"}
    assert all(len(v) >= 24 for v in got.values())


def test_connecting_old_or_new_seerr_sets_a_webhook_each_one_can_actually_send():
    """Version 1.x parses the stored template twice; Seerr 2.x encodes it once more
    itself. Sent plain to Seerr, every notification failed with
    '"[object Object]" is not valid JSON' - and nothing noticed."""
    from core.clients import ServiceError
    from core.webhook_connect import connect_seerr
    from webhooks.seerr_handler import PAYLOAD

    def app(version, listener_up=True):
        saved = {}

        def stored(text):          # what the app keeps, as its settings route does
            return text if version.startswith("1.") else json.dumps(text)

        def sends(text):           # its webhook agent: JSON.parse(JSON.parse(stored))
            try:
                return json.loads(json.loads(stored(text))) == PAYLOAD
            except (TypeError, ValueError):
                return False

        class Over:
            async def get(self, path):
                return {"version": version}

            async def post(self, path, body, raw=False):
                text = body["options"]["jsonPayload"]
                if path.endswith("/test"):
                    if not (listener_up and sends(text)):
                        raise ServiceError("Seerr answered HTTP 500", 500)
                    return {}
                saved["body"], saved["works"] = body, sends(text)

        class S:
            seerr = Over()
        msg = asyncio.run(connect_seerr(S(), "http://10.0.0.5:7980", "sekrit"))
        return saved, msg

    for version in ("1.35.0", "2.7.3", "3.5.0"):
        saved, msg = app(version)
        body = saved["body"]
        assert saved["works"] and "tested" in msg, version
        assert body["enabled"] and body["types"] == 2 | 4 | 8 | 16 | 64 | 128
        assert body["options"]["webhookUrl"] == "http://10.0.0.5:7980/webhook/seerr"
        assert body["options"]["authHeader"] == "sekrit"
    # Setup, before Plexbie's listener runs: the version decides, untested.
    for version in ("1.35.0", "3.5.0"):
        saved, msg = app(version, listener_up=False)
        assert saved["works"] and "checked again" in msg, version


def test_a_request_is_credited_by_the_plex_account_seerr_knows_not_the_payload():
    """The Discord id and display name in the payload are typed in by each Seerr
    user; the Plex account behind the request (from Seerr's API) is not."""
    module, session_module, db, bot, tracked = _seerr_world()
    from plugins.user_mgmt.models import PlexUser

    class Seerr:
        configured = True

        async def get(self, path):
            return {"requestedBy": {"plexId": {"request/5": 975, "request/6": None}[path]}}

    bot.services.seerr = Seerr()

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            async with session_module.get_session() as s:
                s.add(PlexUser(plex_username="river975", plex_user_id=975, discord_id=123456789012345678))
                await s.commit()
            events = module.SeerrEvents(bot)
            claims = {"requestedBy_username": "Robin",
                      "requestedBy_settings_discordId": "223456789012345678",
                      "requestedBy_settings_discordIds": "999456789012345678"}
            real = await events._requester(claims, 5)
            local = await events._requester(claims, 6)     # an Seerr-only account: no Plex account
            return real, local
        finally:
            await session_module.engine.dispose()
    real, local = asyncio.run(go())
    assert real == {"user_id": 123456789012345678, "plex_account_id": "975", "name": "river975"}
    assert local == {"user_id": None, "plex_account_id": None, "name": "Robin"}, "a claimed Discord id is ignored"


def test_connecting_tautulli_adds_one_named_webhook_with_every_trigger():
    from core.webhook_connect import connect_tautulli
    from webhooks.tautulli_handler import BODY, TRIGGERS
    calls = []
    notifiers = [{"id": 3, "agent_id": 13, "friendly_name": ""}]

    class Taut:
        async def call(self, cmd, **params):
            calls.append((cmd, params))
            if cmd == "get_notifiers":
                return list(notifiers)
            if cmd == "add_notifier_config":
                notifiers.append({"id": 9, "agent_id": 25, "friendly_name": ""})
            return None

    class S:
        tautulli = Taut()
    asyncio.run(connect_tautulli(S(), "http://10.0.0.5:7980", "sekrit"))
    cmd, params = calls[-1]
    assert cmd == "set_notifier_config" and params["notifier_id"] == 9 and params["friendly_name"] == "Plexbie"
    assert params["webhook_hook"] == "http://10.0.0.5:7980/webhook/tautulli"
    assert params["on_extdown"] == 0 and params["on_extup"] == 0
    for t in TRIGGERS:
        assert params[t] == 1 and params[f"{t}_body"] == BODY
        assert json.loads(params[f"{t}_subject"]) == {"Authorization": "Bearer sekrit"}

    # Connecting again reuses it instead of adding a second one.
    calls.clear()
    notifiers[-1]["friendly_name"] = "Plexbie"
    asyncio.run(connect_tautulli(S(), "http://10.0.0.5:7980", "sekrit"))
    assert not any(c[0] == "add_notifier_config" for c in calls)


def test_tautulli_recently_added_is_announced():
    calls = _Calls()

    class Arrivals:
        announce_rating_key = calls.hook("announce")

    module, events = _tautulli({"NewMediaAddedCog": Arrivals()})
    asyncio.run(events.dispatch("created", {"rating_key": "123", "media_type": "episode"}))
    assert calls == [("announce", ("123",))]
    assert "on_created" in module.TRIGGERS and "{rating_key}" in module.BODY


def test_on_start_only_webhooks_plexbie_set_up_are_refreshed():
    from core.webhook_connect import refresh_connections
    calls = []

    class Over:
        configured = True

        def __init__(self, url):
            self.url = url

        async def get(self, path):
            return {"options": {"webhookUrl": self.url}}

        async def post(self, path, body, raw=False):
            if not path.endswith("/test"):          # the setting itself, not the test send
                calls.append(("seerr", body["options"]["webhookUrl"]))

    class Taut:
        configured = True

        async def call(self, cmd, **params):
            if cmd == "get_notifiers":
                return [{"id": 4, "agent_id": 25, "friendly_name": "Someone else's"}]
            calls.append(("tautulli", cmd))

    class Cfg:
        seerr_webhook_secret = "o"
        tautulli_webhook_secret = "t"

    class S:
        config = Cfg()
        tautulli = Taut()

    S.seerr = Over("http://10.0.0.5:7980/webhook/overseerr")
    asyncio.run(refresh_connections(S(), "http://10.0.0.5:7980"))
    assert calls == [("seerr", "http://10.0.0.5:7980/webhook/seerr")], "an older install's address moves to the new one"

    calls.clear()
    S.seerr = Over("http://10.0.0.5:7980/webhook/seerr")
    asyncio.run(refresh_connections(S(), "http://10.0.0.5:7980"))
    assert calls == [("seerr", "http://10.0.0.5:7980/webhook/seerr")], calls

    calls.clear()
    S.seerr = Over("http://my-other-app/hook")
    asyncio.run(refresh_connections(S(), "http://10.0.0.5:7980"))
    assert calls == [], "someone else's webhook is left alone"

    calls.clear()      # pointed at the Docker bridge on purpose: the address is kept
    S.seerr = Over("http://172.17.0.1:7980/webhook/overseerr")
    asyncio.run(refresh_connections(S(), "http://10.0.0.5:7980"))
    assert calls == [("seerr", "http://172.17.0.1:7980/webhook/seerr")], calls


def test_on_start_a_tls_proxy_address_that_keeps_the_webhook_path_is_kept():
    from core.webhook_connect import refresh_connections
    seerr_hooks, tautulli_hooks = [], []

    class Over:
        configured = True

        async def get(self, path):
            return {"options": {"webhookUrl": "https://proxy.example/plexbie/webhook/seerr"}}

        async def post(self, path, body, raw=False):
            if not path.endswith("/test"):
                seerr_hooks.append(body["options"]["webhookUrl"])

    class Taut:
        configured = True

        def __init__(self, hook):
            self.hook = hook

        async def call(self, cmd, **params):
            if cmd == "get_notifiers":
                return [{"id": 9, "agent_id": 25, "friendly_name": "Plexbie"}]
            if cmd == "get_notifier_config":
                return {"config": {"hook": self.hook}}
            if cmd == "set_notifier_config":
                tautulli_hooks.append(params["webhook_hook"])

    class Cfg:
        seerr_webhook_secret = "o"
        tautulli_webhook_secret = "t"

    class S:
        config = Cfg()
        seerr = Over()

    S.tautulli = Taut("https://proxy.example/plexbie/webhook/tautulli")
    asyncio.run(refresh_connections(S(), "http://10.0.0.5:7980"))
    assert seerr_hooks == ["https://proxy.example/plexbie/webhook/seerr"]
    assert tautulli_hooks == ["https://proxy.example/plexbie/webhook/tautulli"]

    # A Tautulli address that drops the path is pointed back at the LAN one.
    tautulli_hooks.clear()
    S.tautulli = Taut("https://proxy.example/plexbie-tautulli")
    asyncio.run(refresh_connections(S(), "http://10.0.0.5:7980"))
    assert tautulli_hooks == ["http://10.0.0.5:7980/webhook/tautulli"]


def test_a_failed_test_on_start_leaves_seerrs_webhook_as_it_was():
    from core.clients import ServiceError
    from core.webhook_connect import refresh_connections
    saved = []

    class Over:
        configured = True

        async def get(self, path):
            if path == "status":
                return {"version": "2.7.3"}
            return {"options": {"webhookUrl": "http://10.0.0.5:7980/webhook/overseerr"}}

        async def post(self, path, body, raw=False):
            if path.endswith("/test"):
                raise ServiceError("can't reach it")
            saved.append(body)

    class Cfg:
        seerr_webhook_secret = "o"
        tautulli_webhook_secret = ""

    class S:
        config = Cfg()
        seerr = Over()

        class tautulli:
            configured = False

    asyncio.run(refresh_connections(S(), "http://10.0.0.5:7980"))
    assert saved == []


def test_seerr_saying_available_makes_plexbie_check_plex_itself_now():
    module, session_module, db, bot, tracked = _seerr_world()
    checks = []

    class Arrivals:
        async def check_recently_added(self):
            checks.append("plex")

    bot.get_cog = lambda name: Arrivals() if name == "NewMediaAddedCog" else None

    async def go():
        await session_module.init_database(f"sqlite:///{db}")
        try:
            events = module.SeerrEvents(bot)
            await events.dispatch("MEDIA_AVAILABLE", _event("MEDIA_AVAILABLE", 77, 999))
            await asyncio.sleep(0)
        finally:
            await session_module.engine.dispose()
    asyncio.run(go())
    assert checks == ["plex"], "announced from Plex's own list, not on Seerr's word"
