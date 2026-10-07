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
        from portal.cache import TTLCache
        self.now, self.cache, self.shelves = {}, TTLCache(), []

    async def video(self, media, seasons):
        return dict(self.now.get(media["title"], {"stage": "searching"}))

    async def book(self, media, titles):
        self.shelves.append(titles)
        return dict(self.now.get(media["title"], {"stage": "searching"}))


class Services:
    class config:
        bookshelf_audiobook_library = bookshelf_ebook_library = ""


async def _request(key, title, user_id=42, status="approved", when=T0):
    from database.kv_store import kv_set
    from database.request_store import REQUESTS_NAMESPACE
    await kv_set(REQUESTS_NAMESPACE, key, {"user_id": user_id, "status": status, "timestamp": when.isoformat(),
                                           "media": {"title": title, "media_type": "movie", "id": sum(map(ord, title))}})


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


def test_two_requests_for_the_same_season_are_one_notification():
    """The Boys S1, asked for here and again in Seerr (which the bot records as a
    request of its own): one notification on the phone, not two."""
    async def steps(p, tick):
        await _request("100", "The Boys", when=T0)
        await _request("232", "The Boys", when=T0 + timedelta(minutes=40))
        p.now["The Boys"] = {"stage": "downloading", "percent": 28}
        await tick(T0 + timedelta(minutes=41))
    sent = _run(steps)
    assert [(d["op"], d["id"]) for d in sent] == [("show", "232")]


def test_every_update_carries_when_it_was_sent():
    """A phone that was offline can get its updates late and out of order: each one
    says when it was sent (milliseconds), so a late "show" can't undo an "end"."""
    async def steps(p, tick):
        await _request("100", "Sintel")
        await _request("200", "Spring")
        p.now["Sintel"] = p.now["Spring"] = {"stage": "downloading", "percent": 5}
        await tick(T0)
        p.now["Sintel"] = {"stage": "available"}
        from database.kv_store import kv_delete
        from database.request_store import REQUESTS_NAMESPACE
        await kv_delete(REQUESTS_NAMESPACE, "200")
        await tick(T0 + timedelta(minutes=1))
    sent = _run(steps)
    start = int(T0.timestamp() * 1000)
    assert sorted((d["op"], d["id"], d.get("ts")) for d in sent) == [
        ("end", "100", start + 60_000), ("end", "200", start + 60_000),
        ("show", "100", start), ("show", "200", start)]


def test_the_bookshelf_is_read_through_progress_cache_and_a_failed_rescan_keeps_the_last():
    """Whether a book is on the shelf: the folders are scanned at most every 15 minutes,
    cached with the rest of what progress knows, and a rescan that fails keeps the
    last good list rather than skipping the book."""
    import portal.books as books
    import portal.cache as cache
    scans = []

    def scan(audiobooks, ebooks):
        scans.append(1)
        if len(scans) > 2:
            raise OSError("shelf unmounted")
        return {"b1": {"title": "The Hobbit"}}
    saved = books.scan, cache.time
    books.scan = scan
    try:
        async def steps(p, tick):
            from database.kv_store import kv_set
            from database.request_store import REQUESTS_NAMESPACE
            await kv_set(REQUESTS_NAMESPACE, "100", {"user_id": 42, "status": "approved", "timestamp": T0.isoformat(),
                                                     "media_type": "audiobook",
                                                     "media": {"title": "The Hobbit", "open_library_key": "OL1W"}})
            await tick(T0)
            await tick(T0 + timedelta(minutes=1))
            assert len(scans) == 1 and p.shelves == [["the hobbit"], ["the hobbit"]], "one scan in 15 minutes"
            other = Progress()                                          # another cache: its own scan
            await live_progress.tick(Services, other, now=T0 + timedelta(minutes=2))
            assert len(scans) == 2 and other.shelves == [["the hobbit"]]
            real = cache.time.monotonic
            cache.time = type("Later", (), {"monotonic": staticmethod(lambda: real() + 901)})
            await tick(T0 + timedelta(minutes=17))
            assert len(scans) == 3 and p.shelves == [["the hobbit"]] * 3, "the failed rescan serves the last list"
        _run(steps)
    finally:
        books.scan, cache.time = saved
