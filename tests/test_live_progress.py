# path: tests/test_live_progress.py
"""Live progress (core/live_progress.py): a request's download kept on its requester's
Android phone, updating itself, from downloading until it's on Plex, and never left
holding the notification bar."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401

os.environ["APP_PUSH"] = "expo"

from core import live_progress, notify
from test_app_push import TOKEN, OTHER, _Expo, _db

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class Progress:
    """Stands in for portal.progress: what each title is doing right now."""

    def __init__(self):
        self.now = {}

    async def video(self, media, seasons):
        return dict(self.now.get(media["title"], {"stage": "searching"}))

    async def book(self, media, titles):
        return dict(self.now.get(media["title"], {"stage": "searching"}))


class Services:
    class config:
        bookshelf_audiobook_library = bookshelf_ebook_library = ""


async def _request(key, title, user_id=42, status="approved", when=T0):
    from database.kv_store import kv_set
    from database.request_store import REQUESTS_NAMESPACE
    await kv_set(REQUESTS_NAMESPACE, key, {"user_id": user_id, "status": status, "timestamp": when.isoformat(),
                                           "media": {"title": title, "media_type": "movie", "id": 1}})


def _run(steps):
    """steps(progress, tick) runs inside one database; returns what Expo was sent."""
    expo = _Expo(lambda m: {"status": "ok", "id": "t"})
    try:
        async def body():
            await notify.register_app(TOKEN, "android", plex_account_id=None, plex_name=None, discord_id="42", live=True)
            p = Progress()

            async def tick(at):
                return await live_progress.tick(Services, p, now=at)
            await steps(p, tick)
        _db(body)
    finally:
        expo.close()
    return [m["data"] for m in expo.sent]


def test_a_download_shows_updates_in_steps_and_ends_on_plex():
    async def steps(p, tick):
        await _request("100", "Sintel")
        p.now["Sintel"] = {"stage": "upcoming"}
        await tick(T0)                                                  # not out yet: nothing
        p.now["Sintel"] = {"stage": "downloading", "percent": 10, "detail": "About 4 min left"}
        await tick(T0 + timedelta(minutes=1))
        p.now["Sintel"]["percent"] = 12
        await tick(T0 + timedelta(minutes=2))                           # under STEP: waits
        p.now["Sintel"]["percent"] = 40
        await tick(T0 + timedelta(minutes=3))
        await tick(T0 + timedelta(minutes=14))                          # no move: keepalive
        p.now["Sintel"] = {"stage": "importing", "percent": 100}
        await tick(T0 + timedelta(minutes=15))
        p.now["Sintel"] = {"stage": "available"}
        await tick(T0 + timedelta(minutes=16))
        await tick(T0 + timedelta(minutes=17))                          # over: nothing more
    sent = _run(steps)
    assert [(d["op"], d.get("stage"), d.get("percent")) for d in sent] == [
        ("show", "downloading", 10), ("show", "downloading", 40), ("show", "downloading", 40),
        ("show", "importing", 100), ("end", None, None)]
    first = sent[0]
    assert first["plexbie"] == "live" and first["id"] == "100" and first["slot"] == 1 and first["title"] == "Sintel"
    assert first["text"] == "Downloading, 10%. About 4 min left"


def test_a_stalled_download_comes_down_and_returns_when_it_moves():
    async def steps(p, tick):
        await _request("100", "Sintel")
        p.now["Sintel"] = {"stage": "downloading", "percent": 50}
        await tick(T0)
        for minutes in (30, 60, 90, 121, 150):                         # stuck at 50%
            await tick(T0 + timedelta(minutes=minutes))
        p.now["Sintel"]["percent"] = 60
        await tick(T0 + timedelta(minutes=180))
    sent = _run(steps)
    assert [(d["op"], d.get("percent")) for d in sent] == [
        ("show", 50), ("show", 50), ("show", 50), ("show", 50), ("end", None), ("show", 60)]


def test_only_the_requesters_phone_and_only_with_live_progress_on():
    expo = _Expo(lambda m: {"status": "ok", "id": "t"})
    try:
        async def body():
            await notify.register_app(TOKEN, "android", plex_account_id=None, plex_name=None, discord_id="42", live=False)
            await notify.register_app(OTHER, "android", plex_account_id=None, plex_name=None, discord_id="7", live=True)
            await _request("100", "Sintel", user_id=42)
            p = Progress()
            p.now["Sintel"] = {"stage": "downloading", "percent": 50}
            return await live_progress.tick(Services, p, now=T0)
        sent = _db(body)
    finally:
        expo.close()
    assert sent == 0 and not expo.sent


def test_a_declined_or_vanished_request_ends_its_notification():
    async def steps(p, tick):
        await _request("100", "Sintel")
        await _request("200", "Spring")
        p.now["Sintel"] = p.now["Spring"] = {"stage": "downloading", "percent": 5}
        await tick(T0)
        from database.kv_store import kv_delete
        from database.request_store import REQUESTS_NAMESPACE
        await _request("100", "Sintel", status="declined")
        await kv_delete(REQUESTS_NAMESPACE, "200")
        await tick(T0 + timedelta(minutes=1))
        from database.kv_store import kv_get_all
        assert await kv_get_all(live_progress.NAMESPACE) == {}
    sent = _run(steps)
    assert sorted((d["op"], d["id"]) for d in sent) == [("end", "100"), ("end", "200"), ("show", "100"), ("show", "200")]
