# path: tests/test_portal.py
"""The website's safety rails: signed sessions, who may see and do what, and
writes that can't be forged from another site or double-applied.

The portal lets people without Discord request, join and (for admins) approve.
Every one of those writes must be reachable only by a signed-in person with the
right role, from plexbie's own pages, and must never act on something Discord
has already decided.
"""
import asyncio
import json
import pathlib
import tempfile
import time

import conftest  # noqa: F401
from aiohttp.test_utils import TestClient, TestServer
from helpers import FakeServices

from core.config import Config
from portal.actions import Actions
from portal.app import build_app
from portal.auth import safe_next, sign, unsign

SECRET = "s" * 40


# ------------------------------------------------------------ signed cookies
def test_a_signed_session_round_trips():
    payload = {"via": "plex", "pid": 7, "exp": time.time() + 60}
    assert unsign(SECRET, sign(SECRET, payload)) == payload


def test_a_tampered_session_is_refused():
    token = sign(SECRET, {"admin": False, "exp": time.time() + 60})
    body, mac = token.rsplit(".", 1)
    forged = sign("another secret entirely....................", {"admin": True, "exp": time.time() + 60})
    assert unsign(SECRET, forged) is None
    assert unsign(SECRET, forged.rsplit(".", 1)[0] + "." + mac) is None
    assert unsign(SECRET, body + "." + mac[:-2] + "AA") is None


def test_an_expired_session_is_refused():
    assert unsign(SECRET, sign(SECRET, {"exp": time.time() - 1})) is None


def test_garbage_and_missing_secrets_are_refused():
    for token in (None, "", "no-dot", ".", "a.b"):
        assert unsign(SECRET, token) is None
    assert unsign("", sign(SECRET, {"exp": time.time() + 60})) is None


def test_login_redirects_only_go_to_our_own_pages():
    assert safe_next("/library") == "/library"
    for bad in (None, "", "https://evil.example", "//evil.example", "/\\evil.example", "app"):
        assert safe_next(bad) == "/"


# -------------------------------------------------------------- the web app
class _Actions(Actions):
    """Real guards, recorded effects."""

    def __init__(self, services):
        super().__init__(bot=None, services=services, data=None, public_url="https://plexbie.com")
        self.calls = []

    async def create_request(self, user, body):
        self.calls.append(("request", body))
        return {"slot": 1, "stage": "requested"}

    async def decide_request(self, user, message_id, approve):
        self.calls.append(("decide", message_id, approve))
        return {"ok": True, "message": "done"}

    async def exempt(self, user, body):
        self.calls.append(("exempt", body))
        return {"ok": True, "message": "kept"}


def _client(user, *, readonly=False):
    services = FakeServices(Config())
    actions = None if readonly else _Actions(services)

    async def who(request):
        return user

    app = build_app(services, who=who, readonly=readonly, dist=None,
                    image_cache=tempfile.mkdtemp(), actions=actions)
    return TestClient(TestServer(app)), actions


MEMBER = {"user": {"id": "1", "name": "Pat", "via": "plex"}, "member": True, "admin": False, "plexAccountId": 9}
ADMIN = {**MEMBER, "admin": True}
OUTSIDER = {**MEMBER, "member": False}
OK_HEADERS = {"X-Plexbie": "1", "Content-Type": "application/json"}


def _run(user, method, path, *, headers=None, body=None, readonly=False):
    async def scenario():
        client, actions = _client(user, readonly=readonly)
        await client.start_server()
        try:
            resp = await client.request(method, path, headers=headers or {}, data=json.dumps(body or {}))
            return resp.status, actions
        finally:
            await client.close()
    return asyncio.run(scenario())


def test_logged_out_people_see_nothing():
    for path in ("/api/status", "/api/library", "/api/requests", "/api/admin/requests"):
        assert _run(None, "GET", path)[0] == 401, path


def test_people_without_plex_access_see_nothing():
    for path in ("/api/status", "/api/library", "/api/requests"):
        assert _run(OUTSIDER, "GET", path)[0] == 403, path


def test_the_manage_api_is_admins_only():
    assert _run(MEMBER, "GET", "/api/admin/requests")[0] == 403
    status, actions = _run(MEMBER, "POST", "/api/admin/requests/123/approve", headers=OK_HEADERS)
    assert status == 403 and actions.calls == []
    status, actions = _run(MEMBER, "POST", "/api/admin/cleanup/exempt", headers=OK_HEADERS, body={"ratingKey": "5", "keep": True})
    assert status == 403 and actions.calls == []


def test_an_admin_can_decide():
    status, actions = _run(ADMIN, "POST", "/api/admin/requests/123/approve", headers=OK_HEADERS)
    assert status == 200 and actions.calls == [("decide", "123", True)]


def test_writes_without_our_header_are_refused():
    status, actions = _run(ADMIN, "POST", "/api/admin/requests/123/approve", headers={"Content-Type": "application/json"})
    assert status == 403 and actions.calls == []


def test_writes_from_another_site_are_refused():
    headers = {**OK_HEADERS, "Origin": "https://evil.example"}
    status, actions = _run(MEMBER, "POST", "/api/requests", headers=headers, body={"kind": "movie", "id": "1"})
    assert status == 403 and actions.calls == []


def test_writes_from_plexbie_com_are_allowed():
    headers = {**OK_HEADERS, "Origin": "https://plexbie.com"}
    status, actions = _run(MEMBER, "POST", "/api/requests", headers=headers, body={"kind": "movie", "id": "1"})
    assert status == 201 and actions.calls == [("request", {"kind": "movie", "id": "1"})]


def test_the_preview_refuses_every_write():
    for path in ("/api/requests", "/api/join", "/api/admin/requests/1/approve", "/api/logout"):
        assert _run(ADMIN, "POST", path, headers=OK_HEADERS, readonly=True)[0] == 403, path


def test_rate_limits_stop_a_flood():
    actions = _Actions(FakeServices(Config()))
    for _ in range(3):
        actions.limit("plex:9", "join")
    try:
        actions.limit("plex:9", "join")
    except Exception as e:
        assert getattr(e, "status", None) == 429
    else:
        raise AssertionError("the fourth join in a day should be refused")
    actions.limit("plex:10", "join")  # someone else is unaffected


def test_searching_browsing_and_saving_choices_have_hourly_limits():
    """Each of these costs Seerr, TMDB or Open Library calls (or a disk write), so a
    member, or a stolen app sign-in, can't drive them without end: the one search
    box and "More like this" share search's allowance, scrolling a Discover shelf
    has its own (so browsing can't use up searching), and so does saving language
    choices. The call past the limit is refused before anything upstream is asked."""
    from portal import prefs as prefs_module
    from portal.actions import LIMITS
    from portal.data import Data

    asked = []

    class _Data(Data):
        async def search_all(self, q):
            asked.append("search_all")
            return {"movie": [], "tv": [], "book": []}

        async def similar(self, kind, tid):
            asked.append("similar")
            return []

        async def shelf(self, kind, key, page, langs=None):
            asked.append("shelf")
            return {"kind": kind, "key": key, "page": page, "items": []}

    store = {}

    async def kv_get(ns, key):
        return store.get((ns, key))

    async def kv_set(ns, key, value):
        asked.append("prefs")
        store[(ns, key)] = value

    routes = (("GET", "/api/search/all?q=dune", "lookup", "search_all"),
              ("GET", "/api/titles/movie/5/similar", "lookup", "similar"),
              ("GET", "/api/discover/movie/genre-18?page=2", "browse", "shelf"),
              ("POST", "/api/prefs", "prefs", "prefs"))

    async def scenario(method, path, kind, uses_up=None):
        services = FakeServices(Config())
        actions = _Actions(services)

        async def who(request):
            return MEMBER

        app = build_app(services, who=who, readonly=False, dist=None, image_cache=tempfile.mkdtemp(),
                        data=_Data(services), actions=actions)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for _ in range(LIMITS[uses_up or kind][0] - (0 if uses_up else 1)):
                actions.limit(MEMBER["user"]["id"], uses_up or kind)
            statuses = []
            for _ in range(2):
                resp = await client.request(method, path, headers=OK_HEADERS, data=json.dumps({"languages": ["en"]}))
                statuses.append(resp.status)
            return statuses
        finally:
            await client.close()

    saved = prefs_module.kv_get, prefs_module.kv_set
    prefs_module.kv_get, prefs_module.kv_set = kv_get, kv_set
    try:
        for method, path, kind, upstream in routes:
            assert kind in LIMITS, f"{path} has no hourly limit"
            asked.clear()
            statuses = asyncio.run(scenario(method, path, kind))
            assert statuses == [200, 429], f"{path}: the last call within the hour works, the next is refused, got {statuses}"
            assert asked == [upstream], f"{path}: the refused call still reached {asked[1:]}"
        asked.clear()
        statuses = asyncio.run(scenario("GET", "/api/discover/movie/genre-18?page=2", "browse", uses_up="lookup"))
        assert statuses == [200, 200] and asked == ["shelf", "shelf"], "scrolling Discover isn't stopped by a used-up search allowance"
    finally:
        prefs_module.kv_get, prefs_module.kv_set = saved


# ------------------------------------------------- decisions and the store
async def _init(path):
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


def test_a_request_decided_in_discord_cannot_be_decided_again_on_the_website():
    from database.request_store import STATUS_APPROVED, mark_resolved, save_request
    from plugins.media_requests.cog import decide_request

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(4242, user_id=1, media={"id": 1, "media_type": "movie", "title": "X"})
        await mark_resolved(4242, STATUS_APPROVED, "Discord admin")
        again = await decide_request(None, None, 4242, False, "web admin")
        missing = await decide_request(None, None, 999, True, "web admin")
        return again, missing

    again, missing = asyncio.run(scenario())
    assert again == {"ok": False, "message": "Already approved."}
    assert missing["ok"] is False


def test_a_plex_only_request_keeps_who_asked():
    from database.request_store import get_request, save_request

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(5151, user_id=None, media={"id": 2, "media_type": "tv", "name": "Y"},
                           extra={"plex_account_id": 9, "requester_name": "Pat", "via": "website"})
        return await get_request(5151)

    record = asyncio.run(scenario())
    assert record["plex_account_id"] == 9 and record["requester_name"] == "Pat"
    assert record["status"] == "pending"


def test_a_join_decided_in_discord_cannot_be_decided_again_on_the_website():
    from database.kv_store import kv_set
    from plugins.user_invites.cog import INVITE_MESSAGES_NAMESPACE, INVITES_NAMESPACE, decide_join_request

    class Bot:
        services = FakeServices(Config())

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await kv_set(INVITE_MESSAGES_NAMESPACE, "777", {"user_id": 55, "email": "a@b.co"})
        await kv_set(INVITES_NAMESPACE, "55", {"email": "a@b.co", "status": "approved", "message_id": 777})
        return await decide_join_request(Bot(), Bot.services, 777, True, "web admin")

    assert asyncio.run(scenario()) == {"ok": False, "message": "Already approved."}


def test_no_portal_attribute_hides_a_method():
    """self._sab = (url, key) once replaced the _sab() method, so every book's
    progress lookup failed with "'tuple' object is not callable"."""
    import ast
    for path in (pathlib.Path(conftest.PROJECT_ROOT) / "portal").glob("*.py"):
        for cls in (n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.ClassDef)):
            methods = {f.name for f in cls.body if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for node in ast.walk(cls):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                            assert t.attr not in methods, f"{path.name}: self.{t.attr} hides {cls.name}.{t.attr}()"


# ------------------------------------------------------------ invite links
def _invites_scenario(body):
    from portal.invites import Invites

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        return await body(Invites())
    return asyncio.run(scenario())


ME = {"id": 77, "username": "mom", "email": "Mom@Example.com"}


def test_an_invite_stores_only_a_fingerprint_of_the_link():
    from database.kv_store import kv_get_all
    from portal.invites import NAMESPACE, digest

    async def body(inv):
        token, info = await inv.create(label="Mom", email=None, days=7, actor="Admin")
        return token, info, await kv_get_all(NAMESPACE)

    token, info, stored = _invites_scenario(body)
    assert len(token) == 32 and token not in json.dumps(stored)
    assert list(stored) == [digest(token)] and info["id"] == digest(token)


def test_an_invite_works_once_even_when_two_sign_ins_race():
    calls = []

    async def body(inv):
        token, _ = await inv.create(label="Mom", email=None, days=7, actor="Admin")
        key = (await inv.find(token))[0]

        async def join(rec):
            calls.append(rec["label"])
            await asyncio.sleep(0.05)
            return True
        outcomes = await asyncio.gather(inv.redeem(key, ME, join), inv.redeem(key, ME, join))
        return sorted(outcomes), await inv.find(token)

    outcomes, after = _invites_scenario(body)
    assert outcomes == ["invalid", "ok"] and calls == ["Mom"] and after is None


def test_an_email_locked_invite_refuses_other_accounts_and_stays_unspent():
    async def body(inv):
        token, _ = await inv.create(label="Mom", email="mom@example.com", days=7, actor="Admin")
        key = (await inv.find(token))[0]
        wrong = await inv.redeem(key, {**ME, "email": "someone@else.com"}, lambda rec: asyncio.sleep(0, True))
        still = await inv.find(token) is not None
        right = await inv.redeem(key, ME, lambda rec: asyncio.sleep(0, True))
        return wrong, still, right

    assert _invites_scenario(body) == ("email", True, "ok")


def test_a_failed_share_leaves_the_invite_usable():
    async def body(inv):
        token, _ = await inv.create(label="Mom", email=None, days=7, actor="Admin")
        key = (await inv.find(token))[0]
        first = await inv.redeem(key, ME, lambda rec: asyncio.sleep(0, False))
        return first, await inv.find(token) is not None

    assert _invites_scenario(body) == ("failed", True)


def test_revoked_expired_and_malformed_links_do_not_work():
    from database.kv_store import kv_get, kv_set
    from portal.invites import NAMESPACE

    async def body(inv):
        revoked, _ = await inv.create(label="A", email=None, days=7, actor="Admin")
        expired, _ = await inv.create(label="B", email=None, days=7, actor="Admin")
        key = (await inv.find(revoked))[0]
        assert (await inv.revoke(key, "Admin"))["ok"]
        old = (await inv.find(expired))[0]
        rec = await kv_get(NAMESPACE, old)
        rec["expires_at"] = "2020-01-01T00:00:00+00:00"
        await kv_set(NAMESPACE, old, rec)
        statuses = sorted(i["status"] for i in await inv.all())
        return [await inv.find(t) for t in (revoked, expired, "short", "x" * 32 + "!", None)], statuses

    found, statuses = _invites_scenario(body)
    assert found == [None] * 5 and statuses == ["expired", "revoked"]


def test_invite_details_are_checked():
    async def body(inv):
        errors = []
        for kw in ({"label": "", "email": None}, {"label": "Mom", "email": "not-an-email"}):
            try:
                await inv.create(days=7, actor="Admin", **kw)
            except ValueError as e:
                errors.append(str(e))
        _, capped = await inv.create(label="Long", email=None, days=365, actor="Admin")
        return errors, capped

    errors, capped = _invites_scenario(body)
    from datetime import datetime
    span = datetime.fromisoformat(capped["expiresAt"]) - datetime.fromisoformat(capped["createdAt"])
    assert len(errors) == 2 and span.days == 30


def test_plex_sign_ins_cannot_ask_to_join_without_an_invite():
    actions = _Actions(FakeServices(Config()))
    plex_user = {"user": {"id": "9", "name": "stranger", "via": "plex"}, "member": False, "plexAccountId": "9", "plexName": "stranger"}
    try:
        asyncio.run(Actions.join(actions, plex_user, {}))
    except Exception as e:
        assert getattr(e, "status", None) == 403
    else:
        raise AssertionError("a Plex sign-in without an invite must not be able to ask to join")


# ------------------------------------------------------------ admin parity
def test_linking_refuses_a_discord_account_already_linked_elsewhere_and_unlink_keeps_tracking():
    from sqlalchemy import select
    from database.session import get_session
    from plugins.user_mgmt.cog import UserMgmtCog
    from plugins.user_mgmt.models import PlexUser

    class Member:
        def __init__(self, uid):
            self.id, self.name = uid, f"member{uid}"

    class Bot:
        async def fetch_user(self, uid):
            return Member(uid)

    cog = object.__new__(UserMgmtCog)
    cog.bot, cog.services = Bot(), FakeServices(Config())

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        first = await cog.link_accounts(11, "mom")          # untracked account: starts tracking
        clash = await cog.link_accounts(11, "dad")
        unlinked = await cog.unlink_account("mom")
        again = await cog.unlink_account("mom")
        async with get_session() as session:
            row = (await session.execute(select(PlexUser).where(PlexUser.plex_username == "mom"))).scalar_one()
        return first[0], clash, unlinked[0], again[0], (row.discord_id, row.plex_username)

    first, clash, unlinked, again, row = asyncio.run(scenario())
    assert first and not clash[0] and "already linked to mom" in clash[1]
    assert unlinked and not again and row == (None, "mom")


def test_cleanup_settings_from_the_website_keep_the_discord_safety_limits():
    saved = []

    class Cog:
        config = {"enabled": True, "dry_run": True, "inactivity_days": 90, "notify_days_before": 7,
                  "exclude_libraries": [], "notification_channel_id": None}

        async def load_data(self):
            return True

        async def save_config(self):
            saved.append(dict(self.config))
            return True

    class Bot:
        def get_cog(self, name):
            return Cog() if name == "MediaCleanupCog" else None

    class Data:
        class cache:
            @staticmethod
            def drop(key):
                pass

    actions = _Actions(FakeServices(Config()))
    actions.bot, actions.data = Bot(), Data()
    cog_config = Cog.config

    async def scenario():
        refused = []
        for change in ({"inactivityDays": 7}, {"warnDaysBefore": 90}, {"excludedLibraries": "Movies"}):
            try:
                await actions.cleanup_settings(ADMIN, change)
            except Exception as e:
                refused.append(getattr(e, "status", None))
        ok = await actions.cleanup_settings(ADMIN, {"practice": False, "inactivityDays": 120, "excludedLibraries": ["Kids", "Kids"]})
        return refused, ok

    refused, ok = asyncio.run(scenario())
    assert refused == [400, 400, 400] and ok["ok"]
    assert cog_config["dry_run"] is False and cog_config["inactivity_days"] == 120 and cog_config["exclude_libraries"] == ["Kids"]


def test_say_from_the_website_keeps_mass_pings_off_unless_asked():
    import discord
    sent = []

    class Channel:
        id, name = 5, "general"

        async def send(self, text, allowed_mentions=None):
            sent.append((text, allowed_mentions.everyone, allowed_mentions.roles))

    class Guild:
        def get_channel(self, cid):
            return Channel() if cid == 5 else None

    class Bot:
        def get_guild(self, gid):
            return Guild()

    actions = _Actions(FakeServices(Config()))
    actions.bot = Bot()
    actions.config.guild_id = 1

    async def scenario():
        quiet = await actions.say(ADMIN, {"channelId": "5", "message": "Movie night @everyone"})
        loud = await actions.say(ADMIN, {"channelId": "5", "message": "Fire drill", "allowMassPings": True})
        refused = []
        for body in ({"channelId": "5", "message": ""}, {"channelId": "5", "message": "x" * 2001}, {"channelId": "9", "message": "hi"}):
            try:
                await actions.say(ADMIN, body)
            except Exception as e:
                refused.append(getattr(e, "status", None))
        return quiet, loud, refused

    quiet, loud, refused = asyncio.run(scenario())
    assert quiet["ok"] and loud["ok"] and refused == [400, 400, 404]
    assert sent == [("Movie night @everyone", False, False), ("Fire drill", True, True)]
    assert discord.AllowedMentions  # the real class was used


def test_who_brought_whom_includes_plexbie_invite_links():
    from database.kv_store import kv_set
    from portal.invites import NAMESPACE

    class Bot:
        def get_guild(self, gid):
            return None

        def get_cog(self, name):
            return None

    actions = _Actions(FakeServices(Config()))
    actions.bot = Bot()

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await kv_set(NAMESPACE, "a" * 64, {"label": "Mom", "created_by": "Admin", "used_by": "mom_plex", "used_at": "2026-10-01T10:00:00+00:00"})
        await kv_set(NAMESPACE, "b" * 64, {"label": "Unused", "created_by": "Admin"})
        return await actions.discord_overview(ADMIN)

    out = asyncio.run(scenario())
    assert out["joins"] == [{"who": "mom_plex", "by": "Admin", "via": "plexbie", "code": "Mom", "at": "2026-10-01T10:00:00+00:00", "role": None}]
    assert out["party"] is None and out["channels"] == []


# ------------------------------------------------- people list and backlog
def test_people_lists_everyone_shared_with_the_owner_marked_and_stale_rows_flagged():
    from database.session import get_session
    from plugins.user_mgmt.models import PlexUser
    from portal.admin import Admin
    from portal.data import Data

    class Shares(Admin):
        async def _shared(self):
            return {"alex", "river", "sam"}

        async def _owner(self):
            return "ServerOwner"

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        async with get_session() as session:
            for name in ("alex", "ServerOwner", "gone"):
                session.add(PlexUser(plex_username=name))
            await session.commit()
        return await Shares(Data(FakeServices(Config()))).people()

    rows = {p["plexName"]: p for p in asyncio.run(scenario())}
    assert set(rows) == {"alex", "ServerOwner", "gone", "river", "sam"}
    assert rows["ServerOwner"]["owner"] and rows["ServerOwner"]["hasAccess"]
    assert rows["gone"]["hasAccess"] is False and rows["gone"]["tracked"]
    assert not rows["river"]["tracked"] and rows["river"]["hasAccess"]


def test_closed_backlog_requests_are_neither_waiting_nor_decisions():
    from database.request_store import mark_resolved, save_request
    from portal.admin import Admin
    from portal.data import Data

    data = Data(FakeServices(Config()))

    async def video(media, seasons):
        return {"stage": "approved"}
    data.progress.video = video

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(10, user_id=7, media={"id": 1, "media_type": "movie", "title": "Old"})
        await mark_resolved(10, "closed", "Backlog cleanup")
        await save_request(11, user_id=7, media={"id": 2, "media_type": "movie", "title": "Waiting"})
        return await Admin(data).requests(), await data.my_requests(7)

    admin_view, mine = asyncio.run(scenario())
    assert [r["title"] for r in admin_view["pending"]] == ["Waiting"]
    assert admin_view["recent"] == [] and admin_view["older"] == []
    stages = {r["title"]["title"]: r["stage"] for r in mine}
    assert stages == {"Old": "closed", "Waiting": "requested"}


# --------------------------------------------------------- download progress
def test_a_season_pack_counts_once_and_sabnzbd_numbers_win():
    """The real Simpsons case: Sonarr listed a 13-episode pack 13 times at full size,
    so the page showed 8% (then 72%) while SABnzbd was at 92%."""
    from portal.progress import Progress
    pack = [{"downloadId": "SABnzbd_nzo_abc", "size": 6.5e9, "sizeleft": 1.8e9, "status": "downloading",
             "trackedDownloadState": "downloading", "episode": {"episodeNumber": n}} for n in range(1, 14)]
    sab = {"SABnzbd_nzo_abc": {"nzo_id": "SABnzbd_nzo_abc", "mb": "6246.4", "mbleft": "500.0",
                               "status": "Downloading", "timeleft": "0:04:12"}}
    live = Progress._downloading(pack, sab)
    assert live["percent"] == 91 and live["stage"] == "downloading"
    assert live["detail"] == "Season pack, 13 episodes, about 4 min left"
    # without SABnzbd: Sonarr's own numbers, still counted once (1 - 1.8/6.5 = 72%)
    assert Progress._downloading(pack)["percent"] == 72


def test_separate_episode_downloads_add_up_and_paused_is_said_plainly():
    from portal.progress import Progress
    recs = [{"downloadId": "a", "size": 1e9, "sizeleft": 0}, {"downloadId": "b", "size": 1e9, "sizeleft": 1e9}]
    sab = {"a": {"mb": "1000", "mbleft": "0", "status": "Paused"}, "b": {"mb": "1000", "mbleft": "1000", "status": "Paused"}}
    live = Progress._downloading(recs, sab)
    assert live["percent"] == 50 and live["detail"] == "2 episodes in 2 downloads, paused in SABnzbd"
    assert Progress._time_left("1:02:00:00") == "about 26 h left" and Progress._time_left("0:00:30") == "under a minute left"


def test_a_download_sabnzbd_finished_reads_as_importing_not_stuck_at_100():
    from portal.progress import Progress
    pack = [{"downloadId": "nzo1", "size": 6.5e9, "sizeleft": 0, "trackedDownloadState": "downloading"} for _ in range(13)]
    done = {"nzo1": {"nzo_id": "nzo1", "_done": "Completed"}}
    assert Progress._downloading(pack, done) == {"stage": "importing", "percent": 100, "detail": "Downloaded, moving it onto Plex"}
    broke = Progress._downloading(pack, {"nzo1": {"nzo_id": "nzo1", "_done": "Failed"}})
    assert broke["stage"] == "downloading" and "another copy" in broke["problem"]


def test_sabnzbd_unpacking_shows_as_its_own_stage_with_its_count():
    from portal.progress import Progress, unpacking
    pack = [{"downloadId": "nzo1", "size": 6.5e9, "sizeleft": 0, "trackedDownloadState": "downloading"} for _ in range(13)]
    live = Progress._downloading(pack, {"nzo1": {"nzo_id": "nzo1", "_done": "Extracting", "action_line": "Unpacking: 03/12"}})
    assert live == {"stage": "unpacking", "percent": 25, "detail": "Unpacking, 3 of 12 in SABnzbd"}
    assert unpacking({"status": "Verifying"}) == {"stage": "unpacking", "percent": None, "detail": "Checking the files in SABnzbd"}
    assert unpacking({"status": "Completed"}) is None and unpacking({"status": "Failed"}) is None


# --------------------------------------------------------- more seasons later
def test_more_seasons_can_be_asked_for_after_season_one():
    """The Simpsons case: once season 1 was asked for, the show refused any more."""
    from portal.actions import Actions
    seasons = [{"n": 1, "status": "available"}, {"n": 2, "status": "requested"}, {"n": 3, "status": "partial"},
               {"n": 4, "status": "none"}, {"n": 5, "status": "upcoming"}]
    assert Actions.pick_seasons(seasons, "all") == ([3, 4], False)
    assert Actions.pick_seasons(seasons, [1, 2, 4]) == ([4], False)
    assert Actions.pick_seasons(seasons, "latest") == ([4], True)
    fresh = [{"n": 1, "status": "none"}, {"n": 2, "status": "none"}]
    assert Actions.pick_seasons(fresh, "all") == ("all", False)
    for pick, seasons_ in (([1, 2], seasons), ("all", [{"n": 1, "status": "available"}])):
        try:
            Actions.pick_seasons(seasons_, pick)
        except Exception as e:
            assert getattr(e, "status", None) == 409
        else:
            raise AssertionError("asking only for seasons that are there or requested must be refused")


def test_each_season_says_where_it_stands():
    from database.request_store import save_request
    from portal.data import Data

    data = Data(FakeServices(Config()))

    async def series():
        return {456: {"seasons": [{"seasonNumber": 1, "statistics": {"episodeFileCount": 13}},
                                  {"seasonNumber": 2, "statistics": {"episodeFileCount": 5}}]}}
    data.progress._sonarr_series = series
    seerr = {"mediaInfo": {"seasons": [{"seasonNumber": 3, "status": 3}]},
                 "seasons": [{"seasonNumber": 0, "episodeCount": 9}, {"seasonNumber": 1, "episodeCount": 13},
                             {"seasonNumber": 2, "episodeCount": 22}, {"seasonNumber": 3, "episodeCount": 24},
                             {"seasonNumber": 4, "episodeCount": 22}, {"seasonNumber": 5, "episodeCount": 25},
                             {"seasonNumber": 6, "episodeCount": 10, "airDate": "2999-01-01"}]}

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(1, user_id=7, media={"id": 456, "media_type": "tv", "name": "The Simpsons"}, seasons=[4])
        return await data.tv_seasons("456", seerr)

    got = {s["n"]: (s["status"], s["have"]) for s in asyncio.run(scenario())}
    assert got == {1: ("available", 13), 2: ("partial", 5), 3: ("requested", 0), 4: ("requested", 0),
                   5: ("none", 0), 6: ("upcoming", 0)}


def test_trending_gives_five_films_and_five_shows_with_their_plex_status():
    from portal.data import Data
    data = Data(FakeServices(Config()))
    item = lambda kind, i, status=None: {"mediaType": kind, "id": i, "title": f"{kind}{i}", "name": f"{kind}{i}",
                                         "posterPath": "/p.jpg", "mediaInfo": {"status": status} if status else None}
    pages = {
        "discover/trending?page=1": [item("movie", 1, 5), item("person", 9), item("tv", 2, 3), item("movie", 1),
                                      {**item("movie", 3), "posterPath": None}] + [item("movie", i) for i in range(4, 9)],
        "discover/trending?page=2": [], "discover/trending?page=3": [], "discover/trending?page=4": [],
        "discover/movies?page=1": [], "discover/movies?page=2": [], "discover/tv?page=2": [],
        "discover/tv?page=1": [{**item("tv", i), "mediaType": None} for i in range(20, 26)],
    }

    async def fake_seerr(path, ttl=0):
        return {"results": pages[path]}
    data._seerr = fake_seerr

    out = asyncio.run(data.popular(per_kind=5))
    assert [t["id"] for t in out["movies"]] == ["1", "4", "5", "6", "7"]
    assert out["movies"][0]["availability"] == "available"
    assert [t["id"] for t in out["tv"]] == ["2", "20", "21", "22", "23"] and out["tv"][0]["availability"] == "requested"


def test_approving_a_new_season_of_a_show_already_in_sonarr_searches_for_it():
    """The Simpsons season 2: approved, episodes flagged monitored, but no search ever
    ran, so nothing downloaded. Approval must mark the season wanted and search it."""
    from plugins.media_requests.cog import AdminApprovalView
    calls = []

    class Resp:
        status, content_length = 200, None

        def __init__(self, data=None):
            self.data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self, **kw):
            return self.data

    series = {"id": 137, "seasons": [{"seasonNumber": 1, "monitored": True}, {"seasonNumber": 2, "monitored": False},
                                      {"seasonNumber": 3, "monitored": False}]}

    class Http:
        def request(self, method, url, json=None, **kw):
            if method == "GET":
                calls.append(("GET", url))
                return Resp(globals()["json"].loads(globals()["json"].dumps(series)))
            if method == "PUT":
                calls.append(("PUT", url, [s["seasonNumber"] for s in json["seasons"] if s["monitored"]]))
            else:
                calls.append(("POST", url, json))
            return Resp({})

    config = Config()
    config.sonarr_url, config.sonarr_token = "http://sonarr", "k"
    services = FakeServices(config)
    services.http_session = Http()
    view = object.__new__(AdminApprovalView)
    view.services, view.media = services, {"name": "The Simpsons", "media_type": "tv", "id": 456}
    episodes = ([{"seasonNumber": 1, "hasFile": True, "airDateUtc": "1990-01-01T00:00:00Z"}] * 13
                + [{"seasonNumber": 2, "hasFile": False, "airDateUtc": "1990-10-11T00:00:00Z"}] * 22
                + [{"seasonNumber": 3, "hasFile": False, "airDateUtc": "2999-01-01T00:00:00Z"}])

    from core import season_search
    started = []
    original = season_search.start
    season_search.start = lambda services, series_id, seasons, **kw: started.append((series_id, list(seasons)))
    try:
        searched = asyncio.run(view._monitor_and_search_seasons(series, [2, 3], episodes))
    finally:
        season_search.start = original
    assert searched == [2]
    assert ("PUT", "http://sonarr/api/v3/series/137", [1, 2, 3]) in calls
    # Season 2 is searched (with the episode-by-episode fallback); season 3 hasn't aired.
    assert started == [(137, [2])]


def test_each_season_request_shows_only_its_own_download():
    """Three Simpsons requests (S1 on Plex, S2 done, S3 downloading) all showed S3's 78%."""
    from portal.progress import Progress
    from portal.cache import TTLCache
    prog = Progress(FakeServices(Config()), TTLCache())

    async def series():
        return {456: {"id": 137, "seasons": [
            {"seasonNumber": 1, "statistics": {"episodeCount": 13, "episodeFileCount": 13}},
            {"seasonNumber": 3, "statistics": {"episodeCount": 24, "episodeFileCount": 0}},
            {"seasonNumber": 4, "statistics": {"episodeCount": 22, "episodeFileCount": 0}}]}}

    async def queue():
        return {137: [{"downloadId": "s3", "seasonNumber": 3, "size": 100, "sizeleft": 22} for _ in range(24)]}

    async def sab():
        return {}
    prog._sonarr_series, prog._sonarr_queue, prog._sab_slots = series, queue, sab
    media = {"id": 456, "media_type": "tv"}
    s1 = asyncio.run(prog.video(media, [1]))
    s3 = asyncio.run(prog.video(media, [3]))
    s4 = asyncio.run(prog.video(media, [4]))
    assert s1["stage"] == "available"
    assert s3["stage"] == "downloading" and s3["percent"] == 78
    assert s4["stage"] == "searching"



def test_on_plex_only_once_plex_lists_it_and_adding_to_plex_until_then():
    """Coven Academy showed "On Plex" from Sonarr's files before Plex had it, and
    before Discord or the requester heard anything."""
    from portal.progress import Progress
    from portal.cache import TTLCache
    prog = Progress(FakeServices(Config()), TTLCache())
    plex = {"seasons": {1: 10}, "movie": False}
    nudged = []

    async def series():
        return {298505: {"id": 9, "tvdbId": 461356, "title": "Coven Academy", "seasons": [
            {"seasonNumber": 1, "statistics": {"episodeCount": 21, "episodeFileCount": 21}}]}}

    async def movies():
        return {603: {"id": 3, "title": "The Matrix", "hasFile": True, "imdbId": "tt0133093"}}

    async def nothing():
        return {}

    async def plex_key(kind, *ids):
        if kind == "show":
            return "77" if "tvdb://461356" in ids else None
        return "5" if plex["movie"] else None

    async def plex_seasons(rk):
        return dict(plex["seasons"])

    async def on_plex():
        nudged.append(True)
    prog._sonarr_series, prog._sonarr_queue, prog._sab_slots = series, nothing, nothing
    prog._radarr_movies, prog._radarr_queue = movies, nothing
    prog._plex_key, prog._plex_seasons, prog.on_plex = plex_key, plex_seasons, on_plex
    show, film = {"id": 298505, "media_type": "tv"}, {"id": 603, "media_type": "movie"}

    async def go():
        early = await prog.video(show, None)
        film_early = await prog.video(film, None)
        plex["seasons"], plex["movie"] = {1: 21}, True
        later = await prog.video(show, None)
        film_later = await prog.video(film, None)
        await asyncio.sleep(0)
        return early, film_early, later, film_later
    early, film_early, later, film_later = asyncio.run(go())
    assert early["stage"] == "importing" and early["waitingForPlex"] and "few minutes" in early["detail"]
    assert early["seasons"] == [{"n": 1, "have": 10, "total": 21}], "the bar counts what Plex has"
    assert film_early["stage"] == "importing"
    assert later["stage"] == "available" and later["seasons"][0]["have"] == 21
    assert film_later["stage"] == "available"
    assert nudged == [True], "the bot is told once (rate-limited) so Discord and email follow"


def test_a_new_link_kills_the_old_one_and_keeps_the_person():
    async def body(inv):
        old, info = await inv.create(label="Mom", email="mom@example.com", days=3, actor="Admin")
        old_key = info["id"]
        new, fresh = await inv.renew(old_key, "Admin")
        from datetime import datetime
        span = (datetime.fromisoformat(fresh["expiresAt"]) - datetime.fromisoformat(fresh["createdAt"])).days
        return await inv.find(old), await inv.find(new) is not None, fresh["label"], fresh["email"], span, len(await inv.all())

    old_works, new_works, label, email, span, count = _invites_scenario(body)
    assert old_works is None and new_works and (label, email, span, count) == ("Mom", "mom@example.com", 3, 1)


def test_used_invites_are_kept_when_inviting_again_and_only_dead_ones_delete():
    async def body(inv):
        token, info = await inv.create(label="Grandpa", email=None, days=7, actor="Admin")
        key = info["id"]
        await inv.redeem(key, {"id": 5, "username": "gramps"}, lambda rec: asyncio.sleep(0, True))
        refused_used = await inv.delete(key, "Admin")
        _, again = await inv.renew(key, "Admin")
        refused_active = await inv.delete(again["id"], "Admin")
        await inv.revoke(again["id"], "Admin")
        deleted = await inv.delete(again["id"], "Admin")
        return refused_used["ok"], refused_active["ok"], deleted["ok"], sorted(i["status"] for i in await inv.all())

    assert _invites_scenario(body) == (False, False, True, ["used"])


# ------------------------------------------------------------ help requests
def test_help_can_only_be_asked_on_your_own_request_once_and_reaches_the_admins():
    from database.request_store import save_request
    posted, dms = [], []

    class Channel:
        async def send(self, embed=None, **kw):
            posted.append(embed)

    class Bot:
        def get_channel(self, cid):
            return Channel()

        def get_guild(self, gid):
            return None

        def get_user(self, uid):
            return None

        async def fetch_user(self, uid):
            class U:
                id, name, display_name = uid, "Jordan", "Jordan"

                async def send(self, content=None, embed=None):
                    dms.append(content or embed.description)
            return U()

    actions = _Actions(FakeServices(Config()))
    actions.bot = Bot()
    actions.config.admin_channel_id = 9
    from portal.data import Data
    actions.data = Data(actions.services)

    async def no_progress(media, seasons):
        return {"stage": "downloading", "percent": 0, "detail": "Season pack, 22 episodes"}
    actions.data.progress.video = no_progress
    jordan = {"user": {"id": "7", "name": "Jordan", "via": "discord"}, "member": True, "admin": False, "discordId": "7"}
    pat = {"user": {"id": "8", "name": "Pat", "via": "discord"}, "member": True, "admin": False, "discordId": "8"}

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(4242, user_id=7, media={"id": 456, "media_type": "tv", "name": "The Simpsons"}, seasons=[2])
        from database.request_store import mark_resolved
        await mark_resolved(4242, "approved", "Admin")     # approved, then stuck: what help is for
        refused = []
        for who, body in ((pat, {"reason": "stuck"}), (jordan, {"reason": "other"})):
            try:
                await actions.ask_help(who, "4242", body)
            except Exception as e:
                refused.append(getattr(e, "status", None))
        ok = await actions.ask_help(jordan, "4242", {"reason": "stuck", "note": "0% since this morning"})
        try:
            await actions.ask_help(jordan, "4242", {"reason": "stuck"})
        except Exception as e:
            refused.append(getattr(e, "status", None))
        resolved = await actions.help_resolve(ADMIN, ok["help"]["id"], {"reply": "Kicked the search"})
        again = await actions.help_resolve(ADMIN, ok["help"]["id"], {})
        return refused, ok, resolved, again

    refused, ok, resolved, again = asyncio.run(scenario())
    assert refused == [404, 400, 409] and ok["ok"]
    card = posted[0]
    assert "The Simpsons (season 2)" in card.title and "Stuck downloading" in card.description
    assert any("Season pack, 22 episodes" in f.value for f in card.fields)
    assert resolved["ok"] and not again["ok"] and dms == ["**Message from Pat**\nKicked the search"]


# ------------------------------------------------- all requests (Manage)
def test_the_new_admin_request_endpoints_are_admins_only():
    assert _run(MEMBER, "GET", "/api/admin/all")[0] == 403
    assert _run(MEMBER, "GET", "/api/admin/request/123")[0] == 403
    for path, body in (("/api/admin/request/123/ticket", {"note": "x"}), ("/api/admin/request/123/search/again", {})):
        assert _run(MEMBER, "POST", path, headers=OK_HEADERS, body=body)[0] == 403, path


def _all_requests_data():
    from portal.data import Data
    data = Data(FakeServices(Config()))
    live = {
        1: {"stage": "searching", "detail": "Looking for a copy"},
        2: {"stage": "downloading", "percent": 40, "detail": "2 min left"},
        3: {"stage": "available"},
        4: {"stage": "available"},
        5: {"stage": "downloading", "percent": 12},
        6: {"stage": "downloading", "percent": 0, "problem": "The download failed. Ask an admin in Discord."},
    }

    async def video(media, seasons):
        return live[media["id"]]
    data.progress.video = video
    return data


def test_all_requests_shows_everyones_live_stage_and_says_what_looks_stuck():
    from datetime import datetime, timedelta, timezone
    from database.kv_store import kv_set
    from database.request_store import mark_resolved, save_request, set_fields
    from portal.admin import STAGE_NAMESPACE, Admin

    data = _all_requests_data()
    ago = lambda **kw: (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()   # noqa: E731

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        for key, uid, mid, title in ((101, 7, 1, "Searching Forever"), (102, 8, 2, "Coming Along"),
                                     (103, 7, 3, "Long Done"), (104, 8, 4, "Just Done"),
                                     (105, 7, 5, "Frozen Download"), (106, 8, 6, "Failed One")):
            await save_request(key, user_id=uid, media={"id": mid, "media_type": "movie", "title": title})
            await mark_resolved(key, "approved", "Sam")
        await set_fields(101, resolved_at=ago(hours=30))          # approved over a day ago, still searching
        await set_fields(103, timestamp=ago(days=60), available_at=ago(days=40))   # asked and finished too long ago to list
        await set_fields(104, available_at=ago(days=3))
        await kv_set(STAGE_NAMESPACE, "105", {"stage": "downloading", "since": ago(hours=9), "percent": 12, "moved": ago(hours=7)})
        await save_request(107, user_id=7, media={"id": 2, "media_type": "movie", "title": "Still Waiting"})   # waiting: listed too
        await save_request(108, user_id=7, media={"id": 2, "media_type": "movie", "title": "Said No"})
        await mark_resolved(108, "declined", "Sam")
        await save_request(109, user_id=8, media={"id": 2, "media_type": "movie", "title": "Old No"})
        await mark_resolved(109, "declined", "Sam")
        await set_fields(109, timestamp=ago(days=90))             # declined long ago: only with "everything"
        admin = Admin(data)
        listed = await admin.all_requests()
        every = await admin.all_requests(everything=True)
        found_old = await admin.all_requests("long done")
        found_number = await admin.all_requests("#4")   # Just Done is the 4th request
        detail = await admin.request_detail("102")
        return listed, every, found_old, found_number, detail

    listed, every, found_old, found_number, detail = asyncio.run(scenario())
    rows = {r["title"]["title"]: r for r in listed["rows"]}
    assert set(rows) == {"Searching Forever", "Coming Along", "Just Done", "Frozen Download", "Failed One", "Still Waiting", "Said No"}
    assert rows["Still Waiting"]["stage"] == "requested" and rows["Said No"]["stage"] == "declined"
    assert {r["title"]["title"] for r in every["rows"]} == set(rows) | {"Long Done", "Old No"} and every["everything"]
    assert listed["total"] == 9 and not listed["everything"]
    assert rows["Searching Forever"]["stuck"] == ["Nothing found for over a day"]
    assert rows["Frozen Download"]["stuck"] == ["Download hasn't moved in 6 hours"]
    assert rows["Failed One"]["stuck"] == ["The download failed. Ask an admin in Discord."]
    assert rows["Coming Along"]["stuck"] == [] and rows["Coming Along"]["progress"]["percent"] == 40
    assert rows["Just Done"]["stage"] == "available" and rows["Just Done"]["finishedAt"]
    assert rows["Coming Along"]["approvedBy"] == "Sam" and rows["Coming Along"]["requester"]
    assert listed["counts"] == {"active": 4, "stuck": 3, "waiting": 1, "finished": 1, "declined": 1}
    # stuck ones first, then newest first
    order = [r["title"]["title"] for r in listed["rows"]]
    assert set(order[:3]) == {"Searching Forever", "Frozen Download", "Failed One"} and order[3:] == ["Said No", "Still Waiting", "Just Done", "Coming Along"]
    # a search reaches anything, however old, and by number
    assert [r["title"]["title"] for r in found_old["rows"]] == ["Long Done"] and found_old["counts"] is None
    assert [r["title"]["title"] for r in found_number["rows"]] == ["Just Done"]
    assert detail["via"] == "Discord" and detail["tickets"] == [] and detail["activity"] == []


def test_an_admin_ticket_goes_on_needs_help_and_tells_the_member_only_when_asked():
    from database.request_store import mark_resolved, save_request
    posted, dms = [], []

    class Channel:
        async def send(self, content=None, embed=None, **kw):
            posted.append(embed or content)

    class Bot:
        def get_channel(self, cid):
            return Channel()

        def get_guild(self, gid):
            return None

        def get_user(self, uid):
            return None

        async def fetch_user(self, uid):
            class U:
                id, name, display_name = uid, "Jordan", "Jordan"

                async def send(self, content=None, embed=None, view=None):
                    dms.append(content or embed.description)
            return U()

    actions = _Actions(FakeServices(Config()))
    actions.bot = Bot()
    actions.config.admin_channel_id = 9
    actions.data = _all_requests_data()

    async def scenario():
        from portal import help as helpdesk
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(201, user_id=7, media={"id": 1, "media_type": "movie", "title": "Searching Forever"})
        await mark_resolved(201, "approved", "Sam")
        refused = []
        for body in ({"note": ""}, ):
            try:
                await actions.admin_ticket(ADMIN, "201", body)
            except Exception as e:
                refused.append(getattr(e, "status", None))
        quiet = await actions.admin_ticket(ADMIN, "201", {"note": "Indexer was down"})
        try:
            await actions.admin_ticket(ADMIN, "201", {"note": "again"})
        except Exception as e:
            refused.append(getattr(e, "status", None))
        ticket = (await helpdesk.open_for({"201"}))["201"]
        dms_before_resolve = list(dms)
        resolved_quietly = await actions.help_resolve(ADMIN, quiet["help"]["id"], {"reply": "Fixed"})
        told = await actions.admin_ticket(ADMIN, "201", {"note": "Trying another release", "tell": True})
        resolved_loudly = await actions.help_resolve(ADMIN, told["help"]["id"], {"reply": "On Plex now"})
        detail = await __import__("portal.admin", fromlist=["Admin"]).Admin(actions.data).request_detail("201")
        return refused, quiet, ticket, dms_before_resolve, resolved_quietly, told, resolved_loudly, detail

    refused, quiet, ticket, dms_before, resolved_quietly, told, resolved_loudly, detail = asyncio.run(scenario())
    assert refused == [400, 409] and quiet["ok"] and told["ok"]
    assert ticket["opened_by"] and ticket["quiet"] and ticket["reason"] == "Opened by an admin"
    assert ticket["note"] == "Indexer was down" and "Searching" in ticket["status_then"]
    assert posted[0].title.startswith("🛠️ Ticket opened on No.")
    assert dms_before == [] and resolved_quietly == {"ok": True, "message": "Resolved."}
    assert dms == ["An admin is looking into your request for Searching Forever. You'll hear back here when it's sorted.",
                   "**Message from Pat**\nOn Plex now"]
    assert resolved_loudly["ok"] and len(detail["tickets"]) == 2 and detail["activity"] == []
    assert [t["opened_by"] for t in detail["tickets"]] == [ticket["opened_by"]] * 2


# ------------------------------------------- shows Sonarr never got (Seerr)
def test_a_show_seerr_cant_pass_to_sonarr_says_why_and_looks_stuck_straight_away():
    """Faraway Downs: TMDB has no TheTVDB ID for it, so Seerr accepted the request, failed
    to hand it to Sonarr and deleted its own request. Plexbie said "Approved" for 12 hours."""
    from core.clients import ServiceError
    from database.request_store import mark_resolved, save_request, set_fields
    from portal.admin import Admin
    from portal.data import Data

    data = Data(FakeServices(Config(seerr_url="http://seerr", seerr_token="t")))

    async def video(media, seasons):
        return {"stage": "approved", "detail": "Approved and passed to Seerr", "notInSonarr": True}
    data.progress.video = video
    shows = {204999: {"name": "Faraway Downs", "externalIds": {"tvdbId": None}},
             300: {"name": "Has TVDB", "externalIds": {"tvdbId": 9}}, 301: {"name": "Dropped", "externalIds": {"tvdbId": 10}}}

    async def seerr(path, ttl):
        return shows[int(path.split("/")[1])]
    data._seerr = seerr

    async def seerr_get(path, **kw):
        if path == "request/31":
            raise ServiceError("Seerr answered HTTP 404", 404)
        return {"id": 30}
    data.services.seerr.get = seerr_get

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        for key, mid, name, rid in ((1, 204999, "Faraway Downs", 230), (2, 300, "Has TVDB", 30), (3, 301, "Dropped", 31)):
            await save_request(key, user_id=7, media={"id": mid, "media_type": "tv", "name": name}, seasons=[1])
            await mark_resolved(key, "approved", "Sam")
            await set_fields(key, overseerr_request_id=rid)
        return await data.my_requests(7), await Admin(data).all_requests()

    mine, listed = asyncio.run(scenario())
    by = {r["title"]["title"]: r for r in mine}
    assert by["Faraway Downs"]["progress"]["problem"] == Data.NO_TVDB[1]
    assert by["Faraway Downs"]["progress"]["detail"] == Data.NO_TVDB[0]
    assert by["Dropped"]["progress"]["problem"] == Data.SEERR_DROPPED[1]
    assert "problem" not in by["Has TVDB"]["progress"]            # still on its way: nothing to say
    assert all("notInSonarr" not in (r["progress"] or {}) for r in mine)
    stuck = {r["title"]["title"]: r["stuck"] for r in listed["rows"]}
    assert stuck["Faraway Downs"] == [Data.NO_TVDB[1]] and stuck["Dropped"] == [Data.SEERR_DROPPED[1]]
    assert stuck["Has TVDB"] == []          # approved just now: not stuck yet


def test_a_show_added_by_hand_counts_once_it_is_on_plex():
    from portal.progress import Progress
    from portal.cache import TTLCache
    progress = Progress(FakeServices(Config()), TTLCache())

    async def plex_key(kind, *guids):
        return "77" if "tmdb://204999" in guids else None

    async def plex_seasons(rk):
        return {1: 6}
    progress._plex_key, progress._plex_seasons = plex_key, plex_seasons
    on_plex = asyncio.run(progress._not_in_sonarr(204999, [1]))
    missing = asyncio.run(progress._not_in_sonarr(5, [1]))
    assert on_plex == {"stage": "available", "seasons": [{"n": 1, "have": 6, "total": 6}]}
    assert missing["stage"] == "approved" and missing["notInSonarr"]


def test_approving_a_show_with_no_tvdb_entry_says_so_instead_of_sending_it_to_seerr():
    from plugins.media_requests.cog import no_tvdb_entry
    services = FakeServices(Config(seerr_url="http://seerr", seerr_token="t"))

    async def get(path, **kw):
        return {"204999": {"name": "Faraway Downs", "externalIds": {"tvdbId": None}},
                "1396": {"name": "Breaking Bad", "externalIds": {"tvdbId": 81189}}}[path.split("/")[1]]
    services.seerr.get = get
    assert asyncio.run(no_tvdb_entry(services, 204999)) is True
    assert asyncio.run(no_tvdb_entry(services, 1396)) is False
    assert asyncio.run(no_tvdb_entry(FakeServices(Config()), 204999)) is False   # no Seerr: don't guess


# ------------------------------------------------------------ tickets
def test_ticket_endpoints_are_for_admins_and_answers_for_members():
    assert _run(MEMBER, "GET", "/api/admin/tickets")[0] == 403
    assert _run(MEMBER, "GET", "/api/admin/ticket/abcdefabcdef")[0] == 403
    for path in ("comment", "status", "take"):
        assert _run(MEMBER, "POST", f"/api/admin/ticket/abcdefabcdef/{path}", headers=OK_HEADERS, body={"text": "x"})[0] == 403
    assert _run(OUTSIDER, "POST", "/api/requests/5/help/reply", headers=OK_HEADERS, body={"text": "x"})[0] == 403


def _ticket_bot(posted, dms):
    class Channel:
        async def send(self, content=None, embed=None, **kw):
            posted.append(embed or content)

    class Bot:
        def get_channel(self, cid):
            return Channel()

        def get_guild(self, gid):
            return None

        def get_user(self, uid):
            return None

        async def fetch_user(self, uid):
            class U:
                id, name, display_name = uid, "Jordan", "Jordan"

                async def send(self, content=None, embed=None, view=None):
                    dms.append({"text": content or embed.description, "button": bool(view and view.children)})
                    return type("Sent", (), {"id": 555, "channel": type("C", (), {"id": 444})()})()
            return U()
    return Bot()


def test_a_ticket_is_a_conversation_with_an_owner_notes_and_answers():
    from database.request_store import mark_resolved, save_request
    from portal import help as helpdesk
    from portal.admin import Admin
    posted, dms = [], []
    actions = _Actions(FakeServices(Config()))
    actions.bot = _ticket_bot(posted, dms)
    actions.config.admin_channel_id = 9
    actions.public_url = "https://plexbie.example"
    actions.data = _all_requests_data()
    jordan = {"user": {"id": "7", "name": "Jordan", "via": "discord"}, "member": True, "admin": False, "discordId": "7"}
    sam = {**ADMIN, "user": {"id": "1", "name": "Sam", "via": "discord"}, "discordId": "1"}

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(301, user_id=7, media={"id": 1, "media_type": "movie", "title": "Searching Forever"})
        await mark_resolved(301, "approved", "Sam")
        asked = await actions.ask_help(jordan, "301", {"reason": "stuck", "note": "0% all morning"})
        hid = asked["help"]["id"]
        took = await actions.ticket_take(sam, hid)
        await actions.ticket_comment(sam, hid, {"kind": "note", "text": "Indexer is down, trying another"})
        await actions.ticket_comment(sam, hid, {"kind": "reply", "text": "Is it the 4K one you wanted?"})
        await actions.ticket_status(sam, hid, {"status": "waiting"})
        waiting = await Admin(actions.data).tickets()
        mine_while_waiting = [r for r in await actions.data.my_requests(7) if r["id"] == "301"][0]["help"]
        answered = await actions.member_reply(jordan, "301", {"text": "Yes, the 4K one"})
        after_answer = await Admin(actions.data).ticket(hid)
        solved = await actions.ticket_status(sam, hid, {"status": "resolved", "message": "Grabbed the 4K release"})
        again = await actions.member_reply(jordan, "301", {"text": "thanks"}) if False else None
        refused = None
        try:
            await actions.member_reply(jordan, "301", {"text": "one more thing"})
        except Exception as e:
            refused = getattr(e, "status", None)
        final = await Admin(actions.data).ticket(hid)
        return took, waiting, mine_while_waiting, answered, after_answer, solved, refused, final, again

    took, waiting, mine, answered, after, solved, refused, final, _ = asyncio.run(scenario())
    assert "yours" in took["message"]
    row = waiting["rows"][0]
    assert row["waiting"] and row["owner"] == "Sam" and waiting["counts"] == {"action": 0, "waiting": 1, "solved": 0}
    # the member sees what was said to them, never the admins' note
    kinds = [e["kind"] for e in mine["thread"]]
    assert mine["waiting"] and kinds == ["member", "reply"] and all("Indexer" not in e["text"] for e in mine["thread"])
    assert answered["ok"] and not after["waiting"]
    assert [e["kind"] for e in after["thread"]] == ["member", "status", "note", "reply", "status", "member"]
    assert after["request"]["requester"] and after["request"]["stage"] == "searching"
    # the reply went to Jordan with a Reply button; the answer reached the admin channel with a link
    assert {"text": "**Message from Sam**\nIs it the 4K one you wanted?", "button": True} in dms
    assert any(isinstance(p, str) and "Jordan** answered" in p and "/manage?tab=tickets&ticket=" in p for p in posted)
    assert posted[0].url.startswith("https://plexbie.example/manage?tab=tickets&ticket=")
    assert solved["ok"] and final["status"] == "resolved" and final["thread"][-1]["text"] == "Solved"
    assert refused == 409                 # a solved ticket takes no more answers


def test_an_admin_ticket_can_send_its_own_message_and_old_tickets_get_a_timeline():
    from database.kv_store import kv_set
    from database.request_store import mark_resolved, save_request
    from portal import help as helpdesk
    posted, dms = [], []
    actions = _Actions(FakeServices(Config()))
    actions.bot = _ticket_bot(posted, dms)
    actions.config.admin_channel_id = 9
    actions.data = _all_requests_data()

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(401, user_id=7, media={"id": 1, "media_type": "movie", "title": "Searching Forever"})
        await mark_resolved(401, "approved", "Sam")
        out = await actions.admin_ticket(ADMIN, "401", {"note": "Not on TheTVDB", "tell": True, "message": "Grabbing it by hand tonight"})
        made = await helpdesk.all_help()
        # a ticket from before timelines: built from what it kept
        await kv_set(helpdesk.NAMESPACE, "aaaaaaaaaaaa", {"request": "9", "slot": 9, "title": "Old", "kind": "tv", "who": "Pat",
                                                        "reason": "Stuck downloading", "note": "since Monday", "status": "resolved",
                                                        "created_at": "2026-09-01T00:00:00+00:00", "resolved_by": "Sam",
                                                        "resolved_at": "2026-09-02T00:00:00+00:00", "reply": "Fixed",
                                                        "actions": [{"at": "2026-09-01T01:00:00+00:00", "by": "Sam", "did": "Searched again"}]})
        old = [h for h in await helpdesk.all_help() if h["id"] == "aaaaaaaaaaaa"][0]
        return out, made, old

    out, made, old = asyncio.run(scenario())
    t = [h for h in made if h["request"] == "401"][0]
    assert out["ok"] and [e["kind"] for e in t["thread"]] == ["note", "reply"]
    assert t["thread"][0]["text"] == "Not on TheTVDB" and t["thread"][1]["text"] == "Grabbing it by hand tonight"
    assert dms[0] == {"text": "**Message from Pat**\nGrabbing it by hand tonight", "button": True}
    built = helpdesk.thread_of(old)
    assert [(e["kind"], e["text"]) for e in built] == [("member", "Stuck downloading. since Monday"), ("action", "Searched again"),
                                                      ("reply", "Fixed"), ("status", "Solved")]


def test_the_approval_dm_loses_its_ticket_button_once_it_is_on_plex():
    from database.request_store import get_request, mark_resolved, save_request, set_fields
    from portal.ticket_view import mark_arrived
    edits = []

    class Partial:
        def __init__(self, mid):
            self.mid = mid

        async def edit(self, content=None, view="unset"):
            edits.append((self.mid, content, view))

    class Channel:
        def get_partial_message(self, mid):
            return Partial(mid)

    class Bot:
        def get_channel(self, cid):
            return Channel()

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(501, user_id=7, media={"id": 42, "media_type": "movie", "title": "Arrival"})
        await mark_resolved(501, "approved", "Sam")
        await set_fields(501, approval_dm={"channel": "444", "message": "555", "text": "✅ Approved: Arrival"})
        await save_request(502, user_id=8, media={"id": 42, "media_type": "movie", "title": "Arrival"})   # someone else's
        await mark_resolved(502, "approved", "Sam")
        n = await mark_arrived(Bot(), 42, user_id=7)
        again = await mark_arrived(Bot(), 42, user_id=7)                   # already marked: nothing
        return n, again, await get_request(501), await get_request(502)

    n, again, mine, theirs = asyncio.run(scenario())
    assert n == 1 and again == 0 and mine.get("arrived_at") and not theirs.get("arrived_at")
    assert edits == [(555, "✅ Approved: Arrival\n🎬 **Arrival** is on Plex now.", None)]


# ------------------------------------------------------- works out of the box
def test_the_website_is_on_by_default_with_no_borrowed_address():
    import os
    saved = {k: os.environ.pop(k, None) for k in ("WEB_PORT", "WEB_PUBLIC_URL")}
    try:
        cfg = Config()
        assert cfg.web_port == 7979 and cfg.web_public_url == ""
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_a_session_secret_is_made_once_kept_private_and_reused():
    import os
    import stat
    from portal import server

    here = os.getcwd()
    os.chdir(tempfile.mkdtemp())
    try:
        first = server._stored_secret()
        mode = stat.S_IMODE(os.stat(server.SECRET_FILE).st_mode)
        second = server._stored_secret()
        server.SECRET_FILE.write_text("x" * 40)
        kept = server._stored_secret()
    finally:
        os.chdir(here)
    assert first and len(first) >= 32 and first == second and mode == 0o600 and kept == "x" * 40


def test_the_site_works_out_its_own_address_behind_a_proxy():
    from portal.auth import Auth

    class Req:
        scheme, host = "http", "127.0.0.1:7979"

        def __init__(self, headers, remote="172.17.0.2"):       # through a proxy on the Docker network
            self.headers, self.remote = headers, remote

    assert Auth.base_url(Req({})) == "http://127.0.0.1:7979"
    assert Auth.base_url(Req({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "plex.example.com"})) == "https://plex.example.com"
    # The same headers straight from a visitor are only their say-so: ignored.
    assert Auth.base_url(Req({"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"}, remote="203.0.113.9")) \
        == "http://127.0.0.1:7979"


def test_a_title_seerr_doesnt_know_is_not_found_not_down():
    """Seerr answers an unknown TMDB id with 500 "Unable to retrieve series"."""
    from core.clients import ServiceError
    from portal.data import Data
    data = Data(FakeServices(Config()))

    async def unknown(path, ttl=0):
        raise ServiceError("Seerr answered HTTP 500 (Unable to retrieve series.)", 500)
    data._seerr = unknown
    try:
        asyncio.run(data.title("tv", "999999999"))
        raise AssertionError("expected LookupError")
    except LookupError:
        pass


def test_cleanup_reports_go_to_the_admin_channel_by_default():
    from plugins.media_cleanup.cog import MediaCleanupCog
    sent = []

    class Channel:
        async def send(self, *a, **k):
            sent.append(k or a)

    class Bot:
        def get_channel(self, cid):
            return Channel() if cid == 321 else None

    cfg = Config()
    cfg.admin_channel_id = 321
    cog = object.__new__(MediaCleanupCog)
    cog.bot, cog.services, cog.config = Bot(), FakeServices(cfg), {"notification_channel_id": None}
    asyncio.run(cog.send_cleanup_notification([{"title": "Old Film", "type": "movie", "days_until_deletion": 3}], "warning"))
    assert sent, "the report went nowhere without a cleanup-specific channel"


def test_the_public_name_is_https_even_behind_npm():
    """NPM rewrites X-Forwarded-Proto to the last hop's http; the public address wins for its own host."""
    import os
    from portal.auth import Auth

    class Req:
        def __init__(self, host, proto="http"):
            self.headers, self.scheme, self.host = {"X-Forwarded-Proto": proto}, "http", host
            self.remote = "172.17.0.2"                            # NPM on the Docker network
    saved = os.environ.get("WEB_PUBLIC_URL")
    os.environ["WEB_PUBLIC_URL"] = "https://plexbie.com"
    try:
        assert Auth.base_url(Req("plexbie.com")) == "https://plexbie.com"
        assert Auth.base_url(Req("10.0.0.5:7979")) == "http://10.0.0.5:7979", "the LAN address stays as reached"
    finally:
        os.environ.pop("WEB_PUBLIC_URL", None)
        if saved is not None:
            os.environ["WEB_PUBLIC_URL"] = saved


def test_every_call_into_the_data_helper_exists():
    """When helpers move (shared clients), a forgotten caller must fail here, not on a real request."""
    import ast
    from portal.data import Data
    missing = []
    for path in (pathlib.Path(conftest.PROJECT_ROOT) / "portal").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "data" and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "self" and not hasattr(Data, node.attr)
                    and node.attr not in ("cache", "progress", "services", "config")):
                missing.append(f"{path.name}:{node.lineno} self.data.{node.attr}")
    assert not missing, missing


def test_requests_build_the_media_they_send():
    """A TV/movie request reads Seerr, a book reads Open Library, through the shared clients."""
    from portal.actions import Actions
    from portal.data import Data
    services = FakeServices(Config())
    data = Data(services)

    async def seerr(path, ttl=0):
        assert path == "tv/456"
        return {"name": "Coven Academy", "posterPath": "/p.jpg", "firstAirDate": "2026-01-01", "overview": "o"}
    data._seerr = seerr

    class OL:
        async def get(self, path):
            return {"works/OL1W.json": {"title": "Dune", "authors": [{"author": {"key": "/authors/A1"}}]},
                    "/authors/A1.json": {"name": "Frank Herbert"}}[path]
    services.openlibrary = OL()
    actions = Actions(bot=None, services=services, data=data, public_url="")
    media, _ = asyncio.run(actions._video_media("tv", "456"))
    assert media["name"] == "Coven Academy" and media["id"] == 456
    book = asyncio.run(actions._book("OL1W", "ebook"))
    assert book["title"] == "Dune" and "Frank Herbert" in str(book)


def test_requests_name_who_asked_however_they_asked():
    """Website (Plex sign-in) and Seerr requests have no Discord id: show their Plex name, not "Unknown"."""
    from portal.admin import Admin
    names = {"d55": "alt", "p74": "Pat"}
    assert Admin._who(names, {"user_id": 55}) == "alt"
    assert Admin._who(names, {"user_id": None, "plex_account_id": "74", "requester_name": "pat74"}) == "Pat"
    assert Admin._who(names, {"plex_account_id": "99", "requester_name": "pat74"}) == "pat74"
    assert Admin._who(names, {}) == "Unknown"
    # The join list passes a bare Discord id (its records are keyed by it).
    assert Admin._who(names, "55") == "alt" and Admin._who(names, 1234567) == "Discord user …4567"


def test_plex_index_reads_new_and_old_agent_ids():
    from portal.progress import _plex_index

    class G:
        def __init__(self, i):
            self.id = i

    class Item:
        def __init__(self, rk, guid, guids):
            self.ratingKey, self.guid, self.guids = rk, guid, [G(g) for g in guids]

    class Section:
        type = "show"

        def all(self):
            return [Item(1, "plex://show/abc", ["tmdb://298505", "tvdb://461356"]),
                    Item(2, "com.plexapp.agents.thetvdb://121361?lang=en", [])]

    class Server:
        class library:
            @staticmethod
            def sections():
                return [Section()]
    index = _plex_index(Server(), "show")
    assert index["tvdb://461356"] == "1" and index["tmdb://298505"] == "1"
    assert index["tvdb://121361"] == "2", "older Plex agents are matched too"


def test_watch_on_plex_links_to_the_title_on_this_server():
    from portal.plex import watch_url

    class Server:
        machineIdentifier = "abc123"
    assert watch_url(Server(), "11271") == \
        "https://app.plex.tv/desktop/#!/server/abc123/details?key=%2Flibrary%2Fmetadata%2F11271"
    assert watch_url(None, "1") is None and watch_url(Server(), None) is None


def test_plex_sign_ins_are_recognised_by_account_id_never_by_name():
    """A stranger could register a plex.tv username equal to a Plex Home user's title
    ("Kids") or to a name guessed from a join email, and be let in as that person."""
    from plugins.user_mgmt.models import PlexUser
    from database.session import get_session
    from portal.auth import Auth
    from portal.cache import TTLCache

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        async with get_session() as s:
            s.add_all([PlexUser(plex_username="kids", discord_id=None, plex_user_id=501),
                       PlexUser(plex_username="jane", discord_id=77, plex_user_id=None)])   # guessed from jane@...
        cfg = Config()
        auth = Auth(None, FakeServices(cfg), TTLCache())

        async def share():
            return {"ids": {"1", "501", "600"}, "owner": "1"}
        auth._plex_share_info = share

        async def roles(uid):
            return {"member": True, "admin": uid == 77, "in_guild": True}
        auth._discord_roles = roles
        out = {}
        for label, sess in {"stranger as kids": {"via": "plex", "id": "999", "name": "kids"},
                            "stranger as jane": {"via": "plex", "id": "998", "name": "jane"},
                            "real friend": {"via": "plex", "id": "600", "name": "renamed-friend"},
                            "owner": {"via": "plex", "id": "1", "name": "whoever"}}.items():
            auth.session = lambda request, s=sess: s
            out[label] = await auth.who(None)
        return out

    who = asyncio.run(scenario())
    assert not who["stranger as kids"]["member"], "a Plex Home title is not a username"
    assert not who["stranger as jane"]["member"] and not who["stranger as jane"]["admin"]
    assert who["stranger as jane"]["discordId"] is None, "no one else's Discord link by name"
    assert who["real friend"]["member"], "recognised by account id after a username change"
    assert who["owner"]["member"] and who["owner"]["admin"]


def test_images_are_for_signed_in_members_only():
    """Anyone could fetch (and so fill the disk with) every cover there is."""
    nobody = _run(None, "GET", "/img/tmdb/w342/abcdef.jpg")
    outsider = _run(OUTSIDER, "GET", "/img/tmdb/w342/abcdef.jpg")
    outsider_avatar = _run(OUTSIDER, "GET", "/img/avatar/123/" + "a" * 32 + ".png")
    member_bad_shape = _run(MEMBER, "GET", "/img/tmdb/w99/abcdef.jpg")
    assert nobody[0] == 403 and outsider[0] == 403
    assert outsider_avatar[0] != 403, "a signed-in visitor still sees their own avatar"
    assert member_bad_shape[0] == 404, "members get through to the proxy's own checks"


def test_the_image_cache_drops_what_was_used_least_recently():
    import os
    from portal.images import _prune
    root = pathlib.Path(tempfile.mkdtemp())
    for i in range(10):
        f = root / "ab" / f"img{i}"
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(b"x" * 1000)
        f.with_suffix(".type").write_text("image/jpeg")
        os.utime(f, (1000 + i, 1000 + i))
    removed = _prune(root, 5000)
    left = sorted(p.name for p in (root / "ab").iterdir() if p.suffix != ".type")
    assert removed >= 5 and "img9" in left and "img0" not in left
    assert not (root / "ab" / "img0.type").exists()


def test_plex_image_widths_snap_to_a_few_sizes():
    from portal.images import PLEX_WIDTHS
    assert next(w for w in PLEX_WIDTHS if w >= 343) == 780
    assert next((w for w in PLEX_WIDTHS if w >= 99999), PLEX_WIDTHS[-1]) == 1600


def test_www_goes_to_the_public_name():
    """Sign-in cookies and return addresses belong to plexbie.com, not www."""
    import os
    saved = os.environ.get("WEB_PUBLIC_URL")
    os.environ["WEB_PUBLIC_URL"] = "https://plexbie.example"
    try:
        async def scenario():
            client, _ = _client(MEMBER)
            await client.start_server()
            try:
                www = await client.get("/app/library?x=1", headers={"Host": "www.plexbie.example"}, allow_redirects=False)
                same = await client.get("/healthz", headers={"Host": "plexbie.example"}, allow_redirects=False)
                return www.status, www.headers.get("Location"), same.status
            finally:
                await client.close()
        status, location, same = asyncio.run(scenario())
    finally:
        os.environ.pop("WEB_PUBLIC_URL", None)
        if saved is not None:
            os.environ["WEB_PUBLIC_URL"] = saved
    assert status == 301 and location == "https://plexbie.example/app/library?x=1"
    assert same == 200


def test_forwarded_addresses_are_trusted_only_from_proxies_and_read_from_the_right():
    """Any LAN client could name its own address with CF-Connecting-IP and skip
    every per-visitor limit; through NPM, so could anyone on the internet."""
    from portal.ratelimit import client_ip

    class Req:
        def __init__(self, remote, headers):
            self.remote, self.headers = remote, headers
    via_tunnel = Req("172.17.0.5", {"X-Forwarded-For": "6.6.6.6, 203.0.113.7", "CF-Connecting-IP": "203.0.113.7"})
    assert client_ip(via_tunnel) == "203.0.113.7", "the right-most untrusted hop, not what the visitor wrote"
    lan = Req("192.168.1.50", {"CF-Connecting-IP": "1.2.3.4", "X-Forwarded-For": "1.2.3.4"})
    assert client_ip(lan) == "192.168.1.50", "a LAN machine isn't a proxy"
    cf_only = Req("172.17.0.5", {"CF-Connecting-IP": "198.51.100.9"})
    assert client_ip(cf_only) == "198.51.100.9"


def test_the_owner_is_remembered_by_account_id_when_plex_tv_is_down():
    from plugins.user_mgmt.models import PlexUser  # noqa: F401  (tables)
    from portal.auth import Auth
    from portal.cache import TTLCache

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        auth = Auth(None, FakeServices(Config()), TTLCache())
        state = {"info": {"ids": {"1", "5"}, "owner": "1"}}

        async def share():
            return state["info"]

        async def owner_name():
            return "kids"            # a name anyone could register
        auth._plex_share_info, auth._plex_owner = share, owner_name
        auth.session = lambda request: {"via": "plex", "id": "1", "name": "real-owner"}
        first = await auth.who(None)
        state["info"] = None         # plex.tv unreachable now
        fresh = Auth(None, FakeServices(Config()), TTLCache())
        fresh._plex_share_info, fresh._plex_owner = share, owner_name
        fresh.session = lambda request: {"via": "plex", "id": "9", "name": "kids"}
        impostor = await fresh.who(None)
        fresh.session = lambda request: {"via": "plex", "id": "1", "name": "real-owner"}
        owner_again = await fresh.who(None)
        return first, impostor, owner_again
    first, impostor, owner_again = asyncio.run(scenario())
    assert first["admin"] and owner_again["admin"]
    assert not impostor["admin"], "the name alone no longer makes someone the owner"


def test_the_first_plex_nudge_isnt_skipped_on_a_just_booted_machine():
    """time.monotonic() counts from boot; starting the rate limit at 0 skipped the
    first nudge for the first minute after a reboot (and failed CI on fresh VMs)."""
    from portal import progress as module
    from portal.cache import TTLCache
    prog = module.Progress(FakeServices(Config()), TTLCache())
    nudged = []

    async def on_plex():
        nudged.append(True)
    prog.on_plex = on_plex
    saved = module.time.monotonic
    module.time.monotonic = lambda: 5.0          # five seconds after boot

    async def go():
        prog._arrived("tv:1:None", False)
        prog._arrived("tv:1:None", True)
        await asyncio.sleep(0)
    try:
        asyncio.run(go())
    finally:
        module.time.monotonic = saved
    assert nudged == [True]


def test_a_title_on_seerrs_blocklist_cant_be_requested():
    """Seerr added a blocklist (media status 6): a blocked title looked requestable,
    then failed with a bare error after an admin approved it."""
    from plugins.media_requests.cog import AdminApprovalView
    from portal.data import Data
    assert Data._availability({"status": 6}) == "blocked"
    assert Data._availability({"status": 5}) == "available" and Data._availability(None) == "none"

    class Over:
        configured = True

        async def post(self, path, body, raw=False):
            return 403, '{"message":"This media is blocklisted."}'

    services = FakeServices(Config())
    services.seerr = Over()
    view = AdminApprovalView(media={"id": 603, "media_type": "movie", "title": "Blocked Film"}, user_id=1,
                             services=services)
    ok = asyncio.run(view._submit_to_seerr())
    assert ok is False and "blocklist" in view._seerr_refused

    actions = Actions(None, FakeServices(Config()), None, "")
    actions.limit = lambda *a: None
    actions.data = type("D", (), {"my_requests": staticmethod(lambda *a: asyncio.sleep(0, []))})()

    async def video(kind, tid):
        return {"id": 603}, {"mediaInfo": {"status": 6}}
    actions._video_media = video
    user = {"user": {"id": "1", "name": "Pat"}, "discordId": None, "plexAccountId": "9"}
    try:
        asyncio.run(actions.create_request(user, {"kind": "movie", "id": "603"}))
        raise AssertionError("a blocked title was accepted")
    except Exception as e:
        assert getattr(e, "status", None) == 409 and "blocked" in e.text


def test_a_discord_sign_in_cannot_be_replayed_to_hammer_discord():
    """Each callback calls Discord from the bot's address; a replayed state, or a flood, must not."""
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from portal.auth import Auth
    from portal.cache import TTLCache

    class Refused(Exception):
        status = 401

    class Http:
        calls = 0

        def post(self, *a, **k):
            Http.calls += 1
            raise Refused()

    services = FakeServices(Config())
    services.http_session = Http()
    auth = Auth(None, services, TTLCache())

    async def callback(state):
        auth._cookie = lambda request, name: {"via": "discord", "state": state, "next": "/app"}
        request = make_mocked_request("GET", f"/auth/discord/callback?state={state}&code=x")
        try:
            await auth.discord_callback(request)
        except web.HTTPFound as e:
            return e.location, "plexbie_flow" in e.cookies
        return None, False

    async def scenario():
        first = await callback("s1")
        replay = await callback("s1")
        flood = [await callback(f"n{i}") for i in range(12)]
        return first, replay, flood

    first, replay, flood = asyncio.run(scenario())
    assert first == ("/?login=failed", True), "a failure clears the flow cookie"
    assert replay[0] == "/?login=expired", "the same state twice is refused before calling Discord"
    assert flood[-1][0] == "/?login=busy"
    assert Http.calls == 10, "one address gets ten tries per ten minutes"


def test_plain_http_through_cloudflare_goes_to_https_and_https_says_so():
    import os
    saved = os.environ.get("WEB_PUBLIC_URL")
    os.environ["WEB_PUBLIC_URL"] = "https://plexbie.example"
    try:
        async def scenario():
            client, _ = _client(MEMBER)
            await client.start_server()
            try:
                plain = await client.get("/app?x=1", allow_redirects=False,
                                         headers={"Host": "plexbie.example", "CF-Visitor": '{"scheme":"http"}'})
                secure = await client.get("/healthz", allow_redirects=False,
                                          headers={"Host": "plexbie.example", "CF-Visitor": '{"scheme":"https"}'})
                lan = await client.get("/healthz", allow_redirects=False,
                                       headers={"Host": "10.0.0.5:7979", "CF-Visitor": '{"scheme":"http"}'})
                return ((plain.status, plain.headers.get("Location")),
                        (secure.status, secure.headers.get("Strict-Transport-Security")),
                        (lan.status, lan.headers.get("Strict-Transport-Security")))
            finally:
                await client.close()
        plain, secure, lan = asyncio.run(scenario())
    finally:
        os.environ.pop("WEB_PUBLIC_URL", None)
        if saved is not None:
            os.environ["WEB_PUBLIC_URL"] = saved
    assert plain == (301, "https://plexbie.example/app?x=1")
    assert secure == (200, "max-age=31536000")
    assert lan == (200, None), "the plain LAN address keeps working, without HSTS"


def test_the_cache_is_bounded_and_never_serves_stale_access():
    from portal.cache import TTLCache
    cache = TTLCache(max_keys=3)

    async def scenario():
        for i in range(5):
            await cache.get(f"search:{i}", 60, lambda i=i: asyncio.sleep(0, result=i))
        kept = list(cache._values)

        async def boom():
            raise RuntimeError("plex.tv down")
        await cache.get("library", 0, lambda: asyncio.sleep(0, result="old"))
        stale = await cache.get("library", 0, boom)
        await cache.get("auth:plex-access", 0, lambda: asyncio.sleep(0, result={"ids": {"5"}}))
        try:
            await cache.get("auth:plex-access", 0, boom)
            access = "served stale"
        except RuntimeError:
            access = "raised"
        return kept, stale, access

    kept, stale, access = asyncio.run(scenario())
    assert kept == ["search:2", "search:3", "search:4"]
    assert stale == "old" and access == "raised"


def test_while_plex_tv_is_down_only_its_own_recent_answer_decides_access():
    """Not Plexbie's tracking rows, which keep people removed in Plex directly."""
    import time as _time
    from database.kv_store import kv_set
    from plugins.user_mgmt.models import PlexUser
    from database.session import get_session
    from portal.auth import SHARE_LIST, SHARE_LIST_DAYS, Auth
    from portal.cache import TTLCache

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        async with get_session() as s:
            s.add(PlexUser(plex_username="removed-in-plex", plex_user_id=42))
            await s.commit()
        none_yet = await Auth(None, FakeServices(Config()), TTLCache())._plex_share_info()
        await kv_set(*SHARE_LIST, {"ids": ["5"], "owner": "1", "at": _time.time()})
        recent = await Auth(None, FakeServices(Config()), TTLCache())._plex_share_info()
        await kv_set(*SHARE_LIST, {"ids": ["5"], "owner": "1", "at": _time.time() - (SHARE_LIST_DAYS + 1) * 86400})
        old = await Auth(None, FakeServices(Config()), TTLCache())._plex_share_info()
        return none_yet, recent, old

    none_yet, recent, old = asyncio.run(scenario())
    assert none_yet is None, "the tracking rows don't stand in for plex.tv"
    assert recent == {"ids": {"5"}, "owner": "1"}
    assert old is None


def test_on_off_settings_must_be_real_booleans():
    """bool("false") is True: text would turn media cleanup on, or allow @everyone."""
    from aiohttp import web
    from portal.actions import _flag
    assert _flag({"enabled": True}, "enabled") is True
    assert _flag({"enabled": False}, "enabled") is False
    assert _flag({}, "keep") is False
    for bad in ("false", "true", 0, 1, None, "off"):
        try:
            _flag({"enabled": bad}, "enabled")
            raise AssertionError(f"{bad!r} was accepted")
        except web.HTTPBadRequest:
            pass


def _site(operator="", contact=""):
    """A built-site folder with an index.html like Vite's, served by build_app."""
    dist = pathlib.Path(tempfile.mkdtemp())
    (dist / "index.html").write_text('<!doctype html><html><head><title>Plexbie</title>\n<!--plexbie:head-->\n</head>'
                                     '<body><div id="root"></div></body></html>')
    (dist / "brand").mkdir()
    (dist / "brand" / "og-card.png").write_bytes(b"png")
    (dist / "sw.js").write_text("// worker")
    (dist / "manifest.webmanifest").write_text("{}")
    services = FakeServices(Config())
    services.config.site_operator, services.config.site_contact = operator, contact

    async def who(request):
        return None
    app = build_app(services, who=who, readonly=True, dist=str(dist), image_cache=tempfile.mkdtemp())
    return TestClient(TestServer(app))


def test_link_previews_get_a_real_card_and_invites_never_a_name():
    import os
    saved = os.environ.get("WEB_PUBLIC_URL")
    os.environ["WEB_PUBLIC_URL"] = "https://plexbie.example"
    try:
        async def scenario():
            c = _site()
            await c.start_server()
            try:
                out = {}
                for path in ("/", "/privacy", "/invite", "/library", "/title/movie/1", "/nope", "/favicon.ico",
                             "/index.html", "/privacy/"):
                    r = await c.get(path, allow_redirects=False, headers={"User-Agent": "Discordbot/2.0"})
                    out[path] = (r.status, await r.text(), r.headers.get("Location"), r.headers.get("X-Robots-Tag"))
                robots = await (await c.get("/robots.txt")).text()
                gone = [(await c.get(p)).status for p in ("/sitemap.xml", "/llms.txt", "/go/github")]
                return out, robots, gone
            finally:
                await c.close()
        out, robots, gone = asyncio.run(scenario())
    finally:
        os.environ.pop("WEB_PUBLIC_URL", None)
        if saved is not None:
            os.environ["WEB_PUBLIC_URL"] = saved
    status, home, _, robots_hdr = out["/"]
    assert status == 200 and 'og:image" content="https://plexbie.example/brand/og-card.png"' in home
    assert 'twitter:card" content="summary_large_image"' in home and "<title>Plexbie</title>" in home
    assert robots_hdr == "noindex, nofollow" and 'name="robots" content="noindex, nofollow"' in home, \
        "a household's server stays out of search"
    assert "canonical" not in home and "application/ld+json" not in home, "the project's own site does that"
    assert out["/library"][0] == 200 and out["/title/movie/1"][0] == 200, "the household's pages are at the root"
    assert "Privacy · Plexbie" in out["/privacy"][1] and out["/privacy"][3] == "noindex, nofollow"
    invite = out["/invite"][1]
    assert "You’re invited · Plexbie" in invite and "canonical" not in invite
    assert out["/nope"][0] == 404 and "Not found · Plexbie" in out["/nope"][1], "not a soft 404"
    assert out["/favicon.ico"][0] == 404 and "<html" not in out["/favicon.ico"][1]
    assert out["/index.html"][:1] == (301,) and out["/index.html"][2] == "/"
    assert out["/privacy/"][0] == 301 and out["/privacy/"][2] == "/privacy"
    assert "User-agent: Discordbot" in robots and "Disallow: /\n" in robots, "previews in, search engines out"
    assert gone == [404, 404, 404], "no sitemap, AI notes or click counter: those were the project site's"


def test_old_app_addresses_forward_to_the_root():
    """The household's pages were under /app: links in Discord, emails and bookmarks
    still say so, and land on the same page at the root."""
    async def scenario():
        c = _site()
        await c.start_server()
        try:
            out = {}
            for path in ("/app", "/app/", "/app/library", "/app/title/movie/5?x=1", "/app/manage?tab=requests", "/apple-touch-icon.png"):
                r = await c.get(path, allow_redirects=False)
                out[path] = (r.status, r.headers.get("Location"))
            return out
        finally:
            await c.close()
    out = asyncio.run(scenario())
    assert out["/app"] == (301, "/") and out["/app/"] == (301, "/") and out["/app/library"] == (301, "/library")
    assert out["/app/title/movie/5?x=1"] == (301, "/title/movie/5?x=1")
    assert out["/app/manage?tab=requests"] == (301, "/manage?tab=requests")
    assert out["/apple-touch-icon.png"][0] == 404, "whole path segments only"


def test_the_page_says_who_runs_this_plexbie_for_its_privacy_page():
    async def scenario(**kw):
        c = _site(**kw)
        await c.start_server()
        try:
            return await (await c.get("/privacy")).text()
        finally:
            await c.close()
    page = asyncio.run(scenario(operator='The "Smith" household <3', contact="pat@example.com"))
    tag = page.split('name="plexbie-site" content="', 1)[1].split('"', 1)[0]
    import html as _html
    assert json.loads(_html.unescape(tag)) == {"operator": 'The "Smith" household <3', "contact": "pat@example.com"}
    assert "<3" not in page.split("</head>")[0].replace("&lt;3", ""), "escaped in the head"
    unset = asyncio.run(scenario())
    assert 'content="{&quot;operator&quot;: &quot;&quot;, &quot;contact&quot;: &quot;&quot;}"' in unset


def test_without_a_public_address_cloudflares_word_still_means_https():
    """WEB_PUBLIC_URL unset (a fresh install): CF-Visitor from the trusted proxy still
    redirects plain http, sends HSTS over https, and keeps cookies Secure even when a
    proxy behind Cloudflare rewrites X-Forwarded-Proto to http."""
    import os
    from portal.auth import Auth

    class Req:
        scheme, host = "http", "plexbie.example"

        def __init__(self, headers, remote="172.17.0.2"):
            self.headers, self.remote = headers, remote

    saved = os.environ.pop("WEB_PUBLIC_URL", None)
    try:
        assert Auth.base_url(Req({"X-Forwarded-Proto": "http", "CF-Visitor": '{"scheme":"https"}'})) == "https://plexbie.example"
        assert Auth.base_url(Req({"CF-Visitor": '{"scheme":"https"}'}, remote="203.0.113.9")) == "http://plexbie.example", \
            "CF-Visitor straight from a visitor is only their say-so"

        async def scenario():
            client, _ = _client(MEMBER)
            await client.start_server()
            try:
                plain = await client.get("/app?x=1", allow_redirects=False,
                                         headers={"Host": "plexbie.example", "CF-Visitor": '{"scheme":"http"}'})
                secure = await client.get("/healthz", allow_redirects=False,
                                          headers={"Host": "plexbie.example", "CF-Visitor": '{"scheme":"https"}'})
                lan = await client.get("/healthz", allow_redirects=False, headers={"Host": "10.0.0.5:7979"})
                return ((plain.status, plain.headers.get("Location")),
                        secure.headers.get("Strict-Transport-Security"), (lan.status, lan.headers.get("Strict-Transport-Security")))
            finally:
                await client.close()
        plain, hsts, lan = asyncio.run(scenario())
    finally:
        if saved is not None:
            os.environ["WEB_PUBLIC_URL"] = saved
    assert plain == (301, "https://plexbie.example/app?x=1")
    assert hsts == "max-age=31536000"
    assert lan == (200, None)


def test_a_non_ascii_cookie_is_refused_not_a_crash():
    """hmac.compare_digest raises TypeError on non-ASCII text; a crafted cookie must
    simply not verify (it was a 500 under /img and the Discord callback)."""
    from portal.auth import sign, unsign
    good = sign("s" * 32, {"id": "1", "typ": "plexbie_session"})
    body = good.rsplit(".", 1)[0]
    assert unsign("s" * 32, body + ".sïgnature") is None
    assert unsign("s" * 32, "ünïcode.ünïcode") is None


def test_robots_and_page_heads_ignore_a_forwarded_host_from_the_visitor():
    """With no public address set, the page head must name the host the request came
    to, not an X-Forwarded-Host a visitor slipped through the proxy."""
    import os
    saved = os.environ.pop("WEB_PUBLIC_URL", None)
    try:
        async def scenario():
            c = _site()
            await c.start_server()
            try:
                h = {"Host": "plexbie.example", "X-Forwarded-Host": "evil.example"}
                home = await (await c.get("/", headers=h)).text()
                robots = await (await c.get("/robots.txt", headers=h)).text()
                return home, robots
            finally:
                await c.close()
        home, robots = asyncio.run(scenario())
    finally:
        if saved is not None:
            os.environ["WEB_PUBLIC_URL"] = saved
    assert "plexbie.example/brand/og-card.png" in home
    assert not any("evil.example" in t for t in (home, robots))


def test_pages_isolate_their_window_but_the_sign_in_popup_stays_reachable():
    async def scenario():
        c = _site()
        await c.start_server()
        try:
            page = (await c.get("/privacy")).headers.get("Cross-Origin-Opener-Policy")
            auth = (await c.get("/auth/plex/go", allow_redirects=False)).headers.get("Cross-Origin-Opener-Policy")
            return page, auth
        finally:
            await c.close()
    page, auth = asyncio.run(scenario())
    assert page == "same-origin-allow-popups"
    assert auth is None, "the Plex sign-in window must stay closable from the page that opened it"


def test_the_alerts_worker_and_manifest_are_never_served_stale():
    async def scenario():
        c = _site()
        await c.start_server()
        try:
            out = {}
            for path in ("/sw.js", "/manifest.webmanifest", "/brand/og-card.png"):
                r = await c.get(path)
                out[path] = (r.status, r.headers.get("Cache-Control"))
            return out
        finally:
            await c.close()
    out = asyncio.run(scenario())
    assert out["/sw.js"] == (200, "no-cache") and out["/manifest.webmanifest"] == (200, "no-cache")
    assert out["/brand/og-card.png"] == (200, "public, max-age=3600"), "other files keep their hour"


def test_a_film_plex_matched_to_other_ids_is_still_found_by_its_file():
    """Plex matched Obsession (2026) to another IMDb entry with no TMDB id, so the
    'waiting for Plex' check told the admins it wasn't on Plex while Plex was playing it."""
    from portal.progress import _plex_index, file_key

    class G:
        def __init__(self, i):
            self.id = i

    class Part:
        def __init__(self, f):
            self.file = f

    class Media:
        def __init__(self, f):
            self.parts = [Part(f)]

    class Item:
        def __init__(self, rk, guids, files):
            self.ratingKey, self.guid, self.guids = rk, "plex://movie/x", [G(g) for g in guids]
            self.media = [Media(f) for f in files]

    class Section:
        type = "movie"

        def all(self):
            return [Item(7, ["imdb://tt39365308"], ["/movies/movies/Obsession (2026)/Obsession (2026) Remux-2160p.mkv"]),
                    Item(8, ["tmdb://1"], [])]

    class Server:
        class library:
            @staticmethod
            def sections():
                return [Section()]
    index = _plex_index(Server(), "movie")
    radarr_file = "/data/media/movies/Obsession (2026)/Obsession (2026) Remux-2160p.mkv"
    assert "tmdb://1339713" not in index and "imdb://tt37287335" not in index
    assert index[file_key(radarr_file)] == "7", "the same folder and file, through another mount"
    assert file_key("/data/movies/Other (2026)/Obsession (2026) Remux-2160p.mkv") not in index, "the folder counts too"
    assert file_key(None) is None and file_key("lonely.mkv") is None


def test_a_download_plex_identifies_as_another_film_is_flagged_not_hidden():
    """Radarr grabbed a namesake's release ("Obsession - Du sollst mich lieben", 2025)
    for Obsession (2026): Plex had the file, as another film. On Plex, and flagged."""
    from portal.cache import TTLCache
    from portal.progress import Progress, file_key

    class Svc:
        plex_server = object()
        radarr = sonarr = sab = type("C", (), {"configured": False})()

    p = Progress(Svc(), TTLCache())
    movie = {"tmdbId": 1339713, "imdbId": "tt37287335", "title": "Obsession", "hasFile": True, "id": 5,
             "movieFile": {"path": "/data/media/movies/Obsession (2026)/Obsession (2026) Remux-2160p.mkv"}}

    async def radarr_movies():
        return {1339713: movie}
    p._radarr_movies = radarr_movies

    def index_with(ids_for_file):
        async def get(key, ttl, load):
            return {**{i: "7" for i in ids_for_file}, file_key(movie["movieFile"]["path"]): "7"}
        p.cache.get = get

    async def scenario():
        index_with(["imdb://tt39365308"])
        wrong = await p.video({"media_type": "movie", "id": 1339713}, None)
        index_with(["imdb://tt37287335"])
        right = await p.video({"media_type": "movie", "id": 1339713}, None)
        index_with([])
        unmatched = await p.video({"media_type": "movie", "id": 1339713}, None)
        return wrong, right, unmatched
    wrong, right, unmatched = asyncio.run(scenario())
    assert wrong["stage"] == "available" and wrong["plexIds"] == ["imdb://tt39365308"]
    assert "imdb://tt37287335" in wrong["wantedIds"]
    assert right == {"stage": "available"}, "the right film is just on Plex"
    assert unmatched == {"stage": "available"}, "Plex not matching it at all is no evidence either way"


def test_discover_has_shelves_per_kind_each_title_once_with_genres_and_more_pages():
    from portal.data import Data
    data = Data(FakeServices(Config()))
    item = lambda kind, i, status=None: {"mediaType": kind, "id": i, "title": f"{kind}{i}", "name": f"{kind}{i}",
                                         "posterPath": "/p.jpg", "mediaInfo": {"status": status} if status else None}
    asked = []
    pages = {
        "discover/trending?page=1": {"results": [item("movie", 1, 5), item("tv", 2), item("movie", 3)], "totalPages": 9},
        "discover/trending?page=2": {"results": [item("movie", 4)], "totalPages": 9},
        "discover/movies?page=1": {"results": [{**item("movie", 1), "mediaType": None}, {**item("movie", 5), "mediaType": None}], "totalPages": 1},
        "discover/movies/upcoming?page=1": {"results": [{**item("movie", 6), "mediaType": None}], "totalPages": 3},
        "discover/movies?sortBy=vote_average.desc&voteCountGte=2000&page=1": {"results": [], "totalPages": 0},
        "discover/genreslider/movie": [{"id": 27, "name": "Horror", "backdrops": []}],
        "discover/movies/genre/27?page=2": {"results": [{**item("movie", 7), "mediaType": None}], "totalPages": 5},
        "movie/1339713/recommendations": {"results": []},
        "movie/1339713/similar": {"results": [{**item("movie", 8), "mediaType": None}]},
    }

    async def fake_seerr(path, ttl=0):
        asked.append(path)
        return pages[path]
    data._seerr = fake_seerr

    out = asyncio.run(data.discover("movie"))
    shelves = {s["key"]: ([t["id"] for t in s["titles"]], s["more"]) for s in out["shelves"]}
    assert shelves == {"trending": (["1", "3", "4"], True), "popular": (["5"], False), "upcoming": (["6"], True)}, \
        "film 1 only once, shows left out, an empty shelf not shown"
    assert out["shelves"][0]["titles"][0]["availability"] == "available" and out["genres"] == [{"id": 27, "name": "Horror"}]
    genre = asyncio.run(data.shelf("movie", "genre-27", 2))
    assert [t["id"] for t in genre["titles"]] == ["7"] and genre["more"] is True and genre["page"] == 2
    assert [t["id"] for t in asyncio.run(data.similar("movie", "1339713"))] == ["8"], "recommendations, else similar"
    for bad in (("book", "popular", 1), ("movie", "nope", 1), ("movie", "popular", 99)):
        try:
            asyncio.run(data.shelf(*bad))
        except LookupError:
            continue
        raise AssertionError(f"{bad} should be refused")


def test_each_member_picks_the_languages_their_shelves_show_and_none_is_everything():
    from portal import prefs
    from portal.data import Data
    import portal.prefs as module
    store = {}

    async def kv_get(ns, key):
        return store.get((ns, key))

    async def kv_set(ns, key, value):
        store[(ns, key)] = value
    saved = module.kv_get, module.kv_set
    module.kv_get, module.kv_set = kv_get, kv_set
    try:
        alex, pat = {"discordId": "7"}, {"plexAccountId": "9"}
        assert asyncio.run(prefs.get(alex)) == {"languages": []}, "everything until they choose"
        assert asyncio.run(prefs.save(alex, {"languages": ["ja", "en", "xx", 5, "en"]})) == {"languages": ["en", "ja"]}
        assert asyncio.run(prefs.get(alex)) == {"languages": ["en", "ja"]} and asyncio.run(prefs.get(pat)) == {"languages": []}
        assert asyncio.run(prefs.save(alex, {"languages": "all"})) == {"languages": []}
    finally:
        module.kv_get, module.kv_set = saved
    assert prefs.tmdb_codes([]) is None and prefs.tmdb_codes(["zh"]) == {"zh", "cn"}, "Chinese includes Cantonese"

    data = Data(FakeServices(Config()))
    item = lambda i, lang: {"mediaType": None, "id": i, "title": f"m{i}", "posterPath": "/p.jpg", "originalLanguage": lang}
    pages = {f"discover/movies?page={p}": {"results": [item(p * 10 + 1, "en"), item(p * 10 + 2, "ja"), item(p * 10 + 3, "zh")],
                                          "totalPages": 9} for p in range(1, 7)}

    async def fake_seerr(path, ttl=0):
        return pages[path]
    data._seerr = fake_seerr
    every = asyncio.run(data.shelf("movie", "popular", 1))
    anime = asyncio.run(data.shelf("movie", "popular", 2, {"ja"}))
    assert [t["id"] for t in every["titles"]] == ["11", "12", "13"]
    assert [t["id"] for t in anime["titles"]] == ["42", "52", "62"], "page 2 of a choice reads Seerr's pages 4-6"


def test_one_search_box_finds_films_shows_and_books_and_a_broken_part_is_just_empty():
    from portal.data import Data
    data = Data(FakeServices(Config()))
    asked = []

    async def search(q, kind):
        asked.append(kind)
        if kind == "audiobook":
            raise RuntimeError("Open Library is down")
        return [{"kind": kind, "id": "1", "title": f"{kind} {q}"}]
    data.search = search
    out = asyncio.run(data.search_all("dune"))
    assert out == {"movie": [{"kind": "movie", "id": "1", "title": "movie dune"}],
                   "tv": [{"kind": "tv", "id": "1", "title": "tv dune"}], "book": []}
    assert sorted(asked) == ["audiobook", "movie", "tv"]


def test_discords_return_address_follows_the_public_address():
    """Moving the site (WEB_PUBLIC_URL) moves Discord's return address with it: an older
    DISCORD_CALLBACK_URL left behind would send people to the old address."""
    import os
    from core.config import discord_callback
    saved = {k: os.environ.get(k) for k in ("WEB_PUBLIC_URL", "DISCORD_CALLBACK_URL")}
    try:
        os.environ["WEB_PUBLIC_URL"], os.environ["DISCORD_CALLBACK_URL"] = "home.example.com/", "https://old.example.com/auth/discord/callback"
        assert discord_callback() == "https://home.example.com/auth/discord/callback"
        os.environ.pop("WEB_PUBLIC_URL")
        assert discord_callback() == "https://old.example.com/auth/discord/callback", "no public address: as written"
        os.environ.pop("DISCORD_CALLBACK_URL")
        assert discord_callback() is None
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_health_says_when_discord_doesnt_list_the_sign_in_address():
    from portal.admin import Admin
    from portal.data import Data

    class Answer:
        def __init__(self, status, body):
            self.status, self.body = status, body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return self.body

    class Http:
        def __init__(self, listed):
            self.listed, self.asked = listed, []

        def get(self, url, **kw):
            self.asked.append((url, kw["headers"]["Authorization"]))
            return Answer(200, {"redirect_uris": self.listed})

    def run(listed):
        services = FakeServices(Config())
        cfg = services.config
        cfg.discord_client_id, cfg.discord_client_secret, cfg.discord_bot_token = "1", "secret", "BOT"
        cfg.discord_callback_url = "https://home.example.com/auth/discord/callback"
        cfg.plex_url = cfg.plex_token = None
        services.http_session = Http(listed)
        checks = asyncio.run(Admin(Data(services)).health())
        return next(c for c in checks if c["name"] == "Discord sign-in"), services.http_session.asked
    good, asked = run(["https://home.example.com/auth/discord/callback"])
    assert good["ok"] and asked == [("https://discord.com/api/v10/applications/@me", "Bot BOT")]
    bad, _ = run(["https://old.example.com/auth/discord/callback"])
    assert not bad["ok"] and "https://home.example.com/auth/discord/callback" in bad["detail"] and "Redirects" in bad["detail"]


def test_only_a_sign_in_cookie_is_a_sign_in():
    """The same key signs download links and iPhone sources: none of them, nor an untyped
    payload, may be used as the session cookie (a leaked source URL is not a login)."""
    from portal import app_release
    from portal.auth import SESSION_COOKIE, Auth
    from aiohttp.test_utils import make_mocked_request
    cfg = Config()
    cfg.web_session_secret = "s" * 40
    auth = Auth(None, FakeServices(cfg), None)
    me = {"user": {"id": "42", "name": "Pat", "via": "discord"}}

    def as_cookie(value):
        return make_mocked_request("GET", "/", headers={"Cookie": f"{SESSION_COOKIE}={value}"})
    source = app_release.source_token(cfg.web_session_secret, me)
    untyped = sign(cfg.web_session_secret, {"id": "42", "via": "discord", "exp": time.time() + 60})
    typed = sign(cfg.web_session_secret, {"id": "42", "via": "discord", "typ": SESSION_COOKIE, "exp": time.time() + 60})
    assert auth.session(as_cookie(source)) is None, "an iPhone source URL is not a sign-in"
    assert auth.session(as_cookie(untyped)) is None
    assert auth.session(as_cookie(typed))["id"] == "42"
    assert app_release.source_session(cfg.web_session_secret, source) == {"via": "discord", "id": "42", "name": "Pat"}


def test_a_hidden_tab_cant_turn_a_page_redirect_into_another_site():
    async def scenario():
        c = _site()
        await c.start_server()
        try:
            out = {}
            for path in ("/app/%09/evil.example", "/%09/evil.example/", "/app/%0d/evil.example", "/app/%5c/evil.example", "/app//evil.example"):
                r = await c.get(path, allow_redirects=False)
                out[path] = (r.status, r.headers.get("Location"))
            return out
        finally:
            await c.close()
    out = asyncio.run(scenario())
    for path, (status, location) in out.items():
        assert not (location or "").startswith("//") and "evil.example" not in (location or "").split("/")[2:3], (path, status, location)
    assert out["/app/%09/evil.example"][0] == 404 and out["/%09/evil.example/"][0] == 404
    assert out["/app//evil.example"] == (301, "/evil.example"), "a doubled slash stays on this site"


def test_push_services_are_told_this_installs_own_contact():
    import os
    from core.notify import vapid_contact
    saved = {k: os.environ.get(k) for k in ("SITE_CONTACT", "WEB_PUBLIC_URL")}
    try:
        os.environ["SITE_CONTACT"], os.environ["WEB_PUBLIC_URL"] = "pat@example.com", "https://plexbie.example.com"
        assert vapid_contact() == "mailto:pat@example.com"
        os.environ["SITE_CONTACT"] = "not an email"
        assert vapid_contact() == "https://plexbie.example.com"
        os.environ.pop("SITE_CONTACT"); os.environ["WEB_PUBLIC_URL"] = "http://10.0.0.5:7979"
        assert vapid_contact() == "mailto:support@plexbie.com", "nothing usable: the project's"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_app_alerts_are_off_unless_this_install_opts_in():
    """App alerts go through the project's Expo account: off by default, so other installs
    don't send through it; the app then points members to the website's alerts."""
    import os
    from core import notify
    saved = os.environ.pop("APP_PUSH", None)
    try:
        assert not notify.app_push_on()
        assert asyncio.run(notify._push_app([("k", {"token": "ExponentPushToken[x]"})], {"title": "t"})) == 0, "nothing sent"
        os.environ["APP_PUSH"] = "expo"
        assert notify.app_push_on()
    finally:
        os.environ.pop("APP_PUSH", None)
        if saved is not None:
            os.environ["APP_PUSH"] = saved


# ------------------------------------------------- DMs: the shared inbox
def _inbox_bot(dms, thread_posts, admin_posts):
    """A bot with an admin channel (id 9) that can make threads, and household members who can be DMed."""
    class Thread:
        id, archived = 77, False

        async def send(self, content=None, **kw):
            thread_posts.append((content, kw.get("allowed_mentions"), bool(kw.get("view"))))

    class Start:
        async def create_thread(self, name, auto_archive_duration=None):
            return Thread()

    class Channel:
        guild = None

        async def send(self, content=None, **kw):
            admin_posts.append(content)
            return Start()

    class Home:
        id = 4242

        def get_member(self, uid):
            return object()     # everyone here is in the household's server

    class Bot:
        services = FakeServices(Config())
        portal_actions = None

        def get_guild(self, gid):
            return Home() if gid == Home.id else None

        def get_channel(self, cid):
            return Thread() if int(cid) == 77 else Channel()

        def get_user(self, uid):
            return None

        async def fetch_user(self, uid):
            class U:
                id, name, display_name = uid, "Jordan", "Jordan"

                async def send(self, content=None, embed=None, view=None):
                    dms.append(content)
                    return True
            return U()
    bot = Bot()
    bot.services.config.admin_channel_id = 9
    bot.services.config.guild_id = Home.id
    return bot


def test_the_inbox_routes_are_admins_only():
    for path, body in (("/api/admin/messages/d7/reply", {"text": "hi"}), ("/api/admin/messages/d7/done", {}),
                       ("/api/admin/message/20261005T120000000000-abcdef/to-ticket", {}), ("/api/admin/inbox", {"autoreply": False})):
        assert _run(MEMBER, "POST", path, headers=OK_HEADERS, body=body)[0] == 403, path
        assert _run(None, "POST", path, headers=OK_HEADERS, body=body)[0] in (401, 403), path


def test_a_dm_is_answered_once_posted_in_its_thread_and_admins_can_reply_signed():
    from core import message_log
    from portal import inbox
    dms, thread_posts, admin_posts, alerts = [], [], [], []
    bot = _inbox_bot(dms, thread_posts, admin_posts)
    actions = _Actions(FakeServices(Config()))
    actions.bot = bot
    actions.config.admin_channel_id = 9

    async def alert(who, name, text):
        alerts.append((who, text))
    actions.alert_admins_about_dm = alert
    bot.portal_actions = actions
    sam = {**ADMIN, "user": {"id": "1", "name": "Sam", "via": "discord"}, "discordId": "1"}

    class Msg:
        guild, attachments = None, []

        def __init__(self, text):
            async def send(_self, content=None, embed=None, view=None):
                dms.append(content)
            self.content = text
            self.author = type("A", (), {"id": 7, "name": "jordan", "display_name": "Jordan", "bot": False, "send": send})()

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await inbox.on_dm(bot, Msg("hey, @everyone is Dune coming?"))
        await inbox.on_dm(bot, Msg("also the sound is off"))            # no second auto-reply, no second alert
        await inbox.on_dm(bot, Msg("jordan@example.com"))              # /join-plex's email reply: logged only
        before = {p["id"]: p for p in await message_log.people()}["d7"]
        out = await actions.message_reply(sam, "d7", {"text": "Grabbing it tonight"})
        after = {p["id"]: p for p in await message_log.people()}["d7"]
        refused = []
        for who, text in (("d999", "hi"), ("d7", ""), ("x7", "hi")):
            try:
                await actions.message_reply(sam, who, {"text": text})
            except Exception as e:
                refused.append(getattr(e, "status", None))
        convo = await message_log.conversation("d7")
        return before, out, after, refused, convo

    before, out, after, refused, convo = asyncio.run(scenario())
    assert dms[0] == inbox.AUTOREPLY_TEXT and dms[1] == "Grabbing it tonight\n— Sam (admin)" and len(dms) == 2
    assert alerts == [("d7", "hey, @everyone is Dune coming?")]
    assert admin_posts and "Jordan" in admin_posts[0]                   # the thread's starter, then everything in the thread
    assert all(m is not None and not m.everyone for _, m, _ in thread_posts) and len(thread_posts) == 3
    assert thread_posts[0][2] and "↩️ **Sam** replied: Grabbing it tonight" == thread_posts[-1][0]
    assert before["unread"] == 3 and after["unread"] == 0 and after["done"]["by"] == "Sam"
    assert out["ok"] and refused == [404, 400, 404]
    mine = [m for m in convo if m["by"] == "Sam"]
    assert len(mine) == 1 and mine[0]["direction"] == "out"


def test_a_dm_can_go_on_their_open_ticket():
    from core import message_log
    from database.request_store import mark_resolved, save_request
    from portal import help as helpdesk
    posted, dms = [], []
    actions = _Actions(FakeServices(Config()))
    actions.bot = _ticket_bot(posted, dms)
    actions.data = _all_requests_data()
    jordan = {"user": {"id": "7", "name": "Jordan", "via": "discord"}, "member": True, "admin": False, "discordId": "7"}

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        await save_request(601, user_id=7, media={"id": 1, "media_type": "movie", "title": "Searching Forever"})
        await mark_resolved(601, "approved", "Sam")
        await actions.ask_help(jordan, "601", {"reason": "stuck", "note": "still 0%"})
        key = await message_log.record(channel="discord", direction="in", text="any news?", discord_id=7, discord_name="Jordan")
        out = await actions.message_to_ticket(ADMIN, key)
        try:
            await actions.message_to_ticket(ADMIN, key)
            twice = None
        except Exception as e:
            twice = getattr(e, "status", None)
        t = (await helpdesk.open_for({"601"}))["601"]
        return out, twice, helpdesk.thread_of(t), await message_log.conversation("d7")

    out, twice, thread, convo = asyncio.run(scenario())
    assert out["ok"] and twice == 409
    assert ("member", "any news?") in [(e["kind"], e["text"]) for e in thread]
    assert [m for m in convo if m["text"] == "any news?"][0]["ticket"] == out["ticket"]


def test_the_admin_channel_gets_no_receipt_when_plexbie_dms_someone():
    from core.admin_mirror import dm_user_id
    dms, thread_posts, admin_posts = [], [], []
    bot = _inbox_bot(dms, thread_posts, admin_posts)

    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "p.db")
        return await dm_user_id(bot, bot.services, 7, context="approved", content="✅ Approved")

    assert asyncio.run(scenario()) and dms == ["✅ Approved"] and admin_posts == [] and thread_posts == []
