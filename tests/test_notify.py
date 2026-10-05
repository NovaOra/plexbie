# path: tests/test_notify.py
"""Members without Discord still hear what Discord members are DMed.

Someone who joined with an invite link has no DM to receive. Before this, the
inactivity warning was skipped for them with a log line, so they could be
removed from Plex without ever being told. These tests pin the replacement:
phone/browser alerts first, email as the fallback, never an exception.
"""
import asyncio
import base64
import json
import pathlib
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import conftest  # noqa: F401
from helpers import FakeServices

from core import notify
from core.config import Config


async def _init(path):
    import database.session as session_module
    session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


def _db(body):
    async def scenario():
        await _init(pathlib.Path(tempfile.mkdtemp()) / "n.db")
        return await body()
    return asyncio.run(scenario())


SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "BPkey", "auth": "authsecret"}}


def test_discord_formatting_becomes_plain_words():
    assert notify.plain("✅ **Good news!** <@1234> `Dune` is __here__") == "✅ Good news! Dune is here"


def test_alert_subscriptions_are_kept_per_person_and_validated():
    async def body():
        bad = await notify.subscribe({"endpoint": "http://insecure", "keys": {}}, plex_account_id="1", plex_name="a", discord_id=None)
        ok = await notify.subscribe(SUB, plex_account_id="77", plex_name="Mom", discord_id=None)
        mine = await notify.subscriptions_for(plex_account_id="77")
        theirs = await notify.subscriptions_for(plex_account_id="78")
        # Someone whose plex.tv username is also "Mom" (another account) gets nothing by
        # that name; Plexbie has no row called Mom, so the name leads nowhere.
        by_name = await notify.subscriptions_for(plex_name="Mom")
        await notify.unsubscribe(SUB["endpoint"], plex_account_id="77", discord_id=None)
        gone = await notify.subscriptions_for(plex_account_id="77")
        return bad, ok, len(mine), len(theirs), len(by_name), len(gone)

    assert _db(body) == (False, True, 1, 0, 0, 0)


def test_only_the_owner_or_the_same_browser_can_change_a_subscription():
    """Knowing a browser's push address isn't enough to turn its alerts off or take
    them over; the browser itself (same keys) may change hands, e.g. a shared tablet."""
    async def body():
        await notify.subscribe(SUB, plex_account_id="77", plex_name="Mom", discord_id=None)
        await notify.unsubscribe(SUB["endpoint"], plex_account_id="78", discord_id="5")      # not hers
        still = len(await notify.subscriptions_for(plex_account_id="77"))
        stolen = await notify.subscribe({**SUB, "keys": {"p256dh": "BPother", "auth": "other"}},
                                        plex_account_id="78", plex_name="X", discord_id=None)
        hers = len(await notify.subscriptions_for(plex_account_id="77"))
        shared = await notify.subscribe(SUB, plex_account_id="78", plex_name="Dad", discord_id=None)  # same browser
        moved = (len(await notify.subscriptions_for(plex_account_id="77")), len(await notify.subscriptions_for(plex_account_id="78")))
        return still, stolen, hers, shared, moved

    assert _db(body) == (1, False, 1, True, (0, 1))


def test_alert_subscriptions_are_capped_per_member_and_in_size():
    async def body():
        for i in range(14):
            sub = {"endpoint": f"https://fcm.googleapis.com/fcm/send/{i}", "keys": {"p256dh": "BPkey", "auth": "a"}}
            assert await notify.subscribe(sub, plex_account_id="5", plex_name="x", discord_id=None)
        kept = sorted(r["subscription"]["endpoint"].rsplit("/", 1)[1] for _, r in
                      await notify.subscriptions_for(plex_account_id="5"))
        huge = {"endpoint": "https://fcm.googleapis.com/" + "a" * 5000, "keys": {"p256dh": "BPkey", "auth": "a"}}
        fat_key = {"endpoint": "https://fcm.googleapis.com/x", "keys": {"p256dh": "B" * 1000, "auth": "a"}}
        return kept, await notify.subscribe(huge, plex_account_id="5", plex_name="x", discord_id=None), \
            await notify.subscribe(fat_key, plex_account_id="5", plex_name="x", discord_id=None)

    kept, huge, fat = _db(body)
    assert len(kept) == notify.MAX_SUBS and "13" in kept and "0" not in kept, kept
    assert huge is False and fat is False


def test_push_first_then_email_then_nothing_and_never_raises():
    sent = []
    config = Config()
    config.smtp_host, config.smtp_username, config.smtp_password = "smtp.example", "me@example.com", "x"
    services = FakeServices(config)

    async def body():
        from database.session import get_session
        from plugins.user_mgmt.models import PlexUser
        async with get_session() as s:                 # Plexbie's row for Mom: her account is 77
            s.add(PlexUser(plex_username="Mom", plex_user_id=77))
            await s.commit()
        await notify.subscribe(SUB, plex_account_id="77", plex_name="Mom", discord_id=None)
        real_push, real_email = notify._push, notify._email

        async def push_ok(subs, payload):
            sent.append(("push", payload["title"]))
            return len(subs)

        async def push_none(subs, payload):
            return 0

        async def email(cfg, to, subject, text):
            if to:
                sent.append(("email", to))
            return bool(to)

        async def boom(*a, **k):
            raise RuntimeError("smtp down")
        try:
            notify._push, notify._email = push_ok, email
            first = await notify.notify_member(services, title="Hi", body="b", plex_name="Mom", email="mom@example.com")
            notify._push = push_none
            second = await notify.notify_member(services, title="Hi", body="b", plex_name="Mom", email="mom@example.com")
            third = await notify.notify_member(services, title="Hi", body="b", plex_name="Nobody")
            notify._email = boom
            fourth = await notify.notify_member(services, title="Hi", body="b", plex_name="Mom", email="mom@example.com")
        finally:
            notify._push, notify._email = real_push, real_email
        return first, second, third, fourth

    assert _db(body) == ("push", "email", "none", "none")
    assert sent == [("push", "Hi"), ("email", "mom@example.com")]


def test_old_tracked_media_still_loads_and_website_requesters_count():
    from core.media_tracking import TrackedMedia
    old = TrackedMedia.from_dict({"tmdb_id": 1, "media_type": "movie", "title": "X", "requester_user_id": 5})
    web = TrackedMedia(tmdb_id=2, media_type="movie", title="Y", requester_plex_name="Mom")
    nobody = TrackedMedia(tmdb_id=3, media_type="movie", title="Z")
    assert old.should_notify_for_movie_arrival() and web.should_notify_for_movie_arrival()
    assert not nobody.should_notify_for_movie_arrival()


def test_a_plex_sign_in_name_never_reaches_another_tracked_person():
    """A Plex sign-in carries the plex.tv username its owner chose. When it matches
    someone else's tracked row ("Kids"), alerts and emails for that sign-in must
    follow the account id, not land on the other person's browsers or inbox."""
    from database.session import get_session
    from plugins.user_mgmt.models import PlexUser
    kids_browser = {**SUB, "endpoint": "https://fcm.googleapis.com/fcm/send/kids"}

    async def body():
        async with get_session() as s:
            s.add(PlexUser(plex_username="Kids", plex_user_id=900, discord_id=999, plex_email="kids@example.com"))
            await s.commit()
        await notify.subscribe(kids_browser, plex_account_id=None, plex_name="Kids", discord_id="999")
        attacker_subs = await notify.subscriptions_for(plex_account_id="555", plex_name="Kids")
        attacker_email = await notify.email_for("Kids", "555")
        own_subs = await notify.subscriptions_for(plex_name="Kids")      # a name from Plexbie's own rows
        own_email = await notify.email_for("Kids")
        return attacker_subs, attacker_email, own_subs, own_email

    attacker_subs, attacker_email, own_subs, own_email = _db(body)
    assert attacker_subs == [] and attacker_email is None, "a chosen name must not pick up another person"
    assert len(own_subs) == 1 and own_email == "kids@example.com", "Plexbie's own names still resolve"


def test_tracking_saved_by_an_older_version_still_loads():
    """Fields removed since (is_season_pack, notification_*, air_date...) are ignored, not fatal:
    a TypeError here used to empty the store and lose every requester mapping on the next save."""
    from core.media_tracking import TrackedMedia
    old = TrackedMedia.from_dict({
        "tmdb_id": 7, "media_type": "tv", "title": "Severance", "season_number": 2, "requester_user_id": 5,
        "is_season_pack": True, "notification_message_id": 1, "notification_channel_id": 2,
        "download_start_timestamp": "2026-01-01T00:00:00+00:00", "some_future_field": "x",
        "episodes": [{"episode_number": 1, "downloaded": True, "air_date": "2026-01-01", "available_in_plex": True}],
    })
    assert old.requester_user_id == 5 and old.episodes[0].available_in_plex
    assert TrackedMedia.from_dict(old.to_dict()).to_dict() == old.to_dict(), "and it round-trips"


def test_discord_requests_never_get_website_alerts():
    from database.request_store import save_request
    from plugins.media_requests import cog as media_requests
    calls = []

    async def body():
        real = notify.notify_member

        async def record(services, **kw):
            calls.append(kw["plex_name"])
            return "push"
        notify.notify_member = record
        try:
            await save_request(1, user_id=42, media={"id": 1, "media_type": "movie", "title": "Discord ask"})
            await save_request(2, user_id=None, media={"id": 2, "media_type": "movie", "title": "Web ask"},
                               extra={"plex_account_id": "77", "requester_name": "Mom"})
            await media_requests._notify_web_requester(None, 1, True, "**Approved**")
            await media_requests._notify_web_requester(None, 2, True, "**Approved**")
        finally:
            notify.notify_member = real

    _db(body)
    assert calls == ["Mom"]


def test_a_real_alert_is_encrypted_for_the_phone_that_asked_for_it():
    """End to end through pywebpush, decrypted again with the phone's own keys."""
    try:
        import http_ece
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError:
        print("  (skipped: pywebpush not installed in this image)")
        return

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["headers"] = {k.lower(): v for k, v in self.headers.items()}
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(201)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    phone = ec.generate_private_key(ec.SECP256R1())
    point = phone.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = b"0123456789abcdef"
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    sub = {"endpoint": f"http://127.0.0.1:{server.server_port}/push", "keys": {"p256dh": b64(point), "auth": b64(auth)}}

    async def body():
        keys = await notify.vapid_keys()
        again = await notify.vapid_keys()
        status = notify._push_blocking(sub, json.dumps({"title": "Ready to watch: Dune"}), keys["private"])
        return keys, again, status

    keys, again, status = _db(body)
    server.shutdown()
    assert status == 201 and keys == again
    assert received["headers"]["content-encoding"] == "aes128gcm"
    assert received["headers"]["authorization"].startswith("vapid t=")
    plain = http_ece.decrypt(received["body"], private_key=phone, auth_secret=auth, version="aes128gcm")
    assert json.loads(plain) == {"title": "Ready to watch: Dune"}


def test_push_subscriptions_must_point_at_a_browser_push_service():
    """Any https:// address was accepted, so a member could make the server send
    requests to services on the home network."""
    from core.notify import _valid_subscription
    keys = {"p256dh": "k", "auth": "a"}
    for good in ("https://fcm.googleapis.com/fcm/send/x", "https://updates.push.services.mozilla.com/wpush/v2/x",
                 "https://web.push.apple.com/x", "https://wns2-par02p.notify.windows.com/w/?token=x"):
        assert _valid_subscription({"endpoint": good, "keys": keys}), good
    for bad in ("https://192.168.1.20:8443/", "https://localhost/x", "http://fcm.googleapis.com/x",
                "https://fcm.googleapis.com.evil.example/x", "https://evilpush.apple.com.example/x",
                "https://fcm.googleapis.com:8443/x"):
        assert not _valid_subscription({"endpoint": bad, "keys": keys}), bad
