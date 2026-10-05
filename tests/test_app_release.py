# path: tests/test_app_release.py
"""The app's update notice and members-only downloads, Android and iPhone (portal/app_release)."""
import asyncio
import hashlib
import json
import tempfile
import time
from pathlib import Path

import conftest  # noqa: F401

from portal import app_release

SECRET = "s" * 40
NOTES = "**New in 1.5.0: update notices**\n- Plexbie tells you when a new version is out.\n"


IPA = b"PK fake ipa!"


def _folder(version="1.5.0", code=6, apk=True, ipa=False, **extra) -> Path:
    folder = Path(tempfile.mkdtemp())
    data = b"PK fake apk"
    if apk:
        (folder / f"plexbie-{version}.apk").write_bytes(data)
    if ipa:
        (folder / f"plexbie-{version}.ipa").write_bytes(IPA)
        extra.setdefault("ios", {"file": f"plexbie-{version}.ipa", "sha256": hashlib.sha256(IPA).hexdigest()})
    info = {"version": version, "versionCode": code, "file": f"plexbie-{version}.apk",
            "sha256": hashlib.sha256(data).hexdigest(), "notes": NOTES, "publishedAt": "2026-10-04T15:00:00Z", **extra}
    (folder / "latest.json").write_text(json.dumps(info))
    return folder


def test_the_latest_release_is_read_and_checked():
    info = asyncio.run(app_release.latest(_folder()))
    assert info["version"] == "1.5.0" and info["versionCode"] == 6 and info["size"] == 11
    assert "file" not in app_release.public(info), "members are told the version, not where the file is"
    for bad in (_folder(apk=False), _folder(code="6"), _folder(file="../../.env"), _folder(sha256="nope")):
        assert asyncio.run(app_release.latest(bad)) is None
    assert asyncio.run(app_release.latest(Path(tempfile.mkdtemp()))) is None, "no release yet"


def test_a_download_link_works_for_ten_minutes_and_only_as_signed():
    folder = _folder()
    info = asyncio.run(app_release.latest(folder))
    path = app_release.link(SECRET, info)
    token = path.rsplit("/", 1)[1]
    assert path.startswith("/download/app/") and app_release.apk_for(SECRET, token, folder).name == "plexbie-1.5.0.apk"
    assert app_release.apk_for("x" * 40, token, folder) is None, "another secret"
    assert app_release.apk_for(SECRET, token[:-3] + "AAA", folder) is None, "tampered"
    old = app_release.link(SECRET, info, now=time.time() - app_release.LINK_SECONDS - 1).rsplit("/", 1)[1]
    assert app_release.apk_for(SECRET, old, folder) is None, "run out"
    from portal.auth import sign
    sneaky = sign(SECRET, {"p": "apk", "f": "../.env", "exp": time.time() + 60})
    session_like = sign(SECRET, {"user": "1", "exp": time.time() + 60})
    assert app_release.apk_for(SECRET, sneaky, folder) is None and app_release.apk_for(SECRET, session_like, folder) is None


def test_app_users_are_told_once_per_release_and_not_about_the_first_one_seen():
    from core import notify
    import portal.app_release as module
    store, sent = {}, []

    async def kv_get(ns, key):
        return store.get((ns, key))

    async def kv_set(ns, key, value):
        store[(ns, key)] = value

    async def push_app_to_everyone(**kw):
        sent.append(kw)
        return 1
    saved = module.kv_get, module.kv_set, notify.push_app_to_everyone
    module.kv_get, module.kv_set, notify.push_app_to_everyone = kv_get, kv_set, push_app_to_everyone
    try:
        assert asyncio.run(app_release.announce(_folder("1.5.0", 6))) is False and sent == [], "first seen: noted only"
        assert asyncio.run(app_release.announce(_folder("1.5.0", 6))) is False
        assert asyncio.run(app_release.announce(_folder("1.6.0", 7))) is True
        assert asyncio.run(app_release.announce(_folder("1.6.0", 7))) is False, "once"
    finally:
        module.kv_get, module.kv_set, notify.push_app_to_everyone = saved
    assert sent == [{"title": "Plexbie 1.6.0 is out", "body": "Plexbie tells you when a new version is out.", "url": "/app/update"}]


def test_only_members_get_the_version_and_a_link():
    from test_portal import ADMIN, MEMBER, OK_HEADERS, OUTSIDER, _client
    folder = _folder()
    import os
    saved, saved_secret = app_release.APP_DIR, os.environ.get("WEB_SESSION_SECRET")
    app_release.APP_DIR, os.environ["WEB_SESSION_SECRET"] = folder, SECRET

    async def scenario(user):
        client, _ = _client(user)
        await client.start_server()
        try:
            latest = await client.get("/api/app/latest")
            made = await client.post("/api/app/download-link", headers=OK_HEADERS, data="{}")
            link = (await made.json()).get("url") if made.status == 200 else None
            got = await client.get(link) if link else None
            bad = await client.get("/download/app/not-a-token")
            return (latest.status, await latest.json() if latest.status == 200 else None, made.status,
                    got.status if got else None, got.headers.get("Content-Type") if got else None,
                    await got.read() if got else None, bad.status)
        finally:
            await client.close()
    try:
        for user in (MEMBER, ADMIN):
            status, info, made, got, kind, data, bad = asyncio.run(scenario(user))
            assert status == 200 and info["version"] == "1.5.0" and made == 200
            assert got == 200 and kind == "application/vnd.android.package-archive" and data == b"PK fake apk"
            assert bad == 410
        assert asyncio.run(scenario(None))[0] == 401 and asyncio.run(scenario(OUTSIDER))[0] == 403
        assert asyncio.run(scenario(OUTSIDER))[2] == 403, "no link for outsiders"
    finally:
        app_release.APP_DIR = saved
        if saved_secret is None:
            os.environ.pop("WEB_SESSION_SECRET", None)
        else:
            os.environ["WEB_SESSION_SECRET"] = saved_secret


def test_an_iphone_build_is_offered_only_when_it_checks_out():
    info = asyncio.run(app_release.latest(_folder(ipa=True)))
    assert info["ios"] == {"file": "plexbie-1.5.0.ipa", "sha256": hashlib.sha256(IPA).hexdigest(), "size": len(IPA)}
    assert app_release.public(info)["ios"] == {"size": len(IPA)}, "no file name for members"
    assert asyncio.run(app_release.latest(_folder()))["ios"] is None and app_release.public(asyncio.run(app_release.latest(_folder())))["ios"] is None
    sneaky = asyncio.run(app_release.latest(_folder(ios={"file": "../../.env", "sha256": "0" * 64})))
    missing = asyncio.run(app_release.latest(_folder(ios={"file": "plexbie-1.5.0.ipa", "sha256": "0" * 64})))
    assert sneaky["ios"] is None and missing["ios"] is None and sneaky["version"] == "1.5.0", "Android still offered"


def test_a_source_token_names_the_sign_in_and_nothing_else_passes():
    from portal.auth import sign
    token = app_release.source_token(SECRET, {"user": {"id": "42", "name": "Pat", "via": "discord"}})
    assert app_release.source_session(SECRET, token) == {"via": "discord", "id": "42", "name": "Pat"}
    assert app_release.source_session("x" * 40, token) is None
    old = app_release.source_token(SECRET, {"user": {"id": "42", "via": "plex"}}, now=time.time() - app_release.SOURCE_DAYS * 86400 - 1)
    assert app_release.source_session(SECRET, old) is None, "run out"
    apk_link = app_release.link(SECRET, {"file": "plexbie-1.5.0.apk"}).rsplit("/", 1)[1]
    assert app_release.source_session(SECRET, apk_link) is None, "a download link isn't a source"
    assert app_release.source_session(SECRET, sign(SECRET, {"p": "iossrc", "via": "github", "id": "1", "exp": time.time() + 60})) is None
    folder = _folder(ipa=True)
    assert app_release.ipa_for(True, "plexbie-1.5.0.ipa", folder).read_bytes() == IPA
    assert app_release.ipa_for(False, "plexbie-1.5.0.ipa", folder) is None
    assert app_release.ipa_for(True, "../latest.json", folder) is None and app_release.ipa_for(True, "plexbie-1.4.0.ipa", folder) is None


def test_the_source_lists_the_newest_build_in_sidestores_format():
    info = asyncio.run(app_release.latest(_folder(ipa=True)))
    src = app_release.altstore_source(info, "https://plexbie.example/", "TOKEN")
    app = src["apps"][0]
    assert app["bundleIdentifier"] == "com.plexbie.app" and src["iconURL"] == "https://plexbie.example/brand/plexbie-512.png"
    (v,) = app["versions"]
    assert v["version"] == "1.5.0" and v["buildVersion"] == "6" and v["size"] == len(IPA)
    assert v["downloadURL"] == "https://plexbie.example/download/ios/TOKEN/plexbie-1.5.0.ipa"
    assert v["localizedDescription"].startswith("New in 1.5.0: update notices") and "• Plexbie tells you" in v["localizedDescription"]
    assert app_release.altstore_source(asyncio.run(app_release.latest(_folder())), "https://x", "T")["apps"][0]["versions"] == []


def test_iphone_sources_are_for_members_and_end_when_they_leave():
    import os
    from aiohttp.test_utils import TestClient, TestServer
    from portal.app import build_app
    from test_portal import MEMBER, OK_HEADERS, OUTSIDER, Config, FakeServices, _Actions
    folder = _folder(ipa=True)
    saved, saved_secret = app_release.APP_DIR, os.environ.get("WEB_SESSION_SECRET")
    app_release.APP_DIR, os.environ["WEB_SESSION_SECRET"] = folder, SECRET
    still = {"member": True}

    class FakeAuth:
        """Only describe() matters here; the sign-in pages it would add just answer."""
        mobile = None

        def __getattr__(self, name):
            async def page(*_args, **_kw):
                from aiohttp import web
                return web.Response(status=204)
            return page

        async def describe(self, s):
            assert s == {"via": "plex", "id": "1", "name": "Pat"}
            return {**MEMBER, "member": still["member"]}

    async def scenario(user):
        services = FakeServices(Config())
        services.config.web_session_secret = SECRET

        async def who(request):
            return user
        client = TestClient(TestServer(build_app(services, who=who, readonly=False, dist=None,
                                                 image_cache=tempfile.mkdtemp(), auth=FakeAuth(),
                                                 actions=_Actions(services))))
        await client.start_server()
        try:
            made = await client.post("/api/app/ios-source", headers=OK_HEADERS, data="{}")
            if made.status != 200:
                return made.status, None, None
            links = await made.json()
            path = links["url"].split("://", 1)[1].split("/", 1)[1]
            assert links["sidestore"].startswith("sidestore://source?url=http") and links["altstore"].startswith("altstore://source?url=")
            src = await client.get("/" + path)
            body = await src.json()
            dl = body["apps"][0]["versions"][0]["downloadURL"].split("://", 1)[1].split("/", 1)[1]
            got = await client.get("/" + dl)
            first = (src.status, got.status, await got.read())
            still["member"] = False
            after = (await client.get("/" + path)).status, (await client.get("/" + dl)).status
            still["member"] = True
            forged = (await client.get("/app-source/not.a-token.json")).status
            return made.status, first, (after, forged)
        finally:
            await client.close()
    try:
        made, first, rest = asyncio.run(scenario(MEMBER))
        assert made == 200 and first == (200, 200, IPA), (made, first)
        assert rest == ((410, 410), 410), "removed from Plex: no source, no download"
        assert asyncio.run(scenario(OUTSIDER))[0] == 403 and asyncio.run(scenario(None))[0] == 401
    finally:
        app_release.APP_DIR = saved
        if saved_secret is None:
            os.environ.pop("WEB_SESSION_SECRET", None)
        else:
            os.environ["WEB_SESSION_SECRET"] = saved_secret
