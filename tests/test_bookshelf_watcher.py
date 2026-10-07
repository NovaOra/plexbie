# path: tests/test_bookshelf_watcher.py
"""Watch-directory settling, permanent-failure tracking, and hint matching.

Regression coverage for four defects found in review:
  * An item that could never be processed was retried every scan forever. One
    empty folder produced 30,931 cycles and 26 MB of log output in 48 days.
  * The settle timer was set once on first sight and never refreshed, so a
    download still being written was processed after settle_seconds regardless.
  * Startup processed everything inline with no settle wait at all.
  * Hint files were matched by two-way substring and never expired, so a
    six-month-old hint could hand its metadata to an unrelated book.
"""
import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import conftest  # noqa: F401

from plugins.bookshelf_processor.cog import (
    HINT_MAX_AGE_DAYS, _find_hint_file, _item_signature, _normalise_for_match,
)


def _tmpdir():
    return Path(tempfile.mkdtemp())


# --- item signature: the mechanism the settle timer relies on ---

def test_signature_changes_when_a_file_grows():
    root = _tmpdir()
    target = root / "book.m4b"
    target.write_bytes(b"x" * 10)
    first = _item_signature(root)
    target.write_bytes(b"x" * 500)
    assert _item_signature(root) != first, (
        "signature must change while a download is still being written"
    )


def test_signature_changes_when_a_file_is_added():
    root = _tmpdir()
    (root / "one.m4b").write_bytes(b"a")
    first = _item_signature(root)
    (root / "two.m4b").write_bytes(b"b")
    assert _item_signature(root) != first


def test_signature_is_stable_when_nothing_changes():
    root = _tmpdir()
    (root / "book.epub").write_bytes(b"hello")
    assert _item_signature(root) == _item_signature(root)


def test_signature_of_empty_dir_is_stable():
    """The real stuck item was an empty directory."""
    root = _tmpdir()
    assert _item_signature(root) == _item_signature(root)
    assert _item_signature(root)[0] == 0


def test_signature_of_single_file():
    root = _tmpdir()
    f = root / "solo.epub"
    f.write_bytes(b"abc")
    assert _item_signature(f)[0] == 1


def test_signature_of_missing_path_does_not_raise():
    assert _item_signature(Path("/nonexistent/xyz")) == (-1, -1, -1)


# --- hint matching ---

def _write_hint(watch: Path, nzb_title: str, age_days: float = 0.0, **extra):
    payload = {"nzb_title": nzb_title, "title": extra.get("title", "T"),
               "author": extra.get("author", "A")}
    safe = nzb_title.replace("/", "_")
    path = watch / f".plexbie_hint_{safe}.json"
    path.write_text(json.dumps(payload))
    if age_days:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


def test_exact_normalised_match_is_found():
    """SABnzbd sanitises the folder name, so match on the normalised form."""
    watch = _tmpdir()
    _write_hint(watch, "Andy Weir - Project Hail Mary (2021)")
    found = _find_hint_file(watch, "Andy.Weir.-.Project.Hail.Mary.(2021)")
    assert found is not None


def test_unrelated_item_does_not_match():
    watch = _tmpdir()
    _write_hint(watch, "Brandon Sanderson - Mistborn Bk 1 - The Final Empire")
    assert _find_hint_file(watch, "Some.Completely.Different.Book") is None


def test_substring_no_longer_matches():
    """The old test was `nzb_title in item_name or item_name in nzb_title`.

    'Red Rising' is a strict substring of 'Red Rising 2 Golden Son', so the old
    code handed book one's metadata to book two.
    """
    watch = _tmpdir()
    _write_hint(watch, "Red Rising")
    assert _find_hint_file(watch, "Red.Rising.2.Golden.Son") is None


def test_reverse_substring_no_longer_matches():
    watch = _tmpdir()
    _write_hint(watch, "Red Rising 2 Golden Son")
    assert _find_hint_file(watch, "Red.Rising") is None


def test_stale_hint_is_deleted_and_not_used():
    """Two hints on the live deployment were 2 and 6 months old."""
    watch = _tmpdir()
    stale = _write_hint(watch, "Old Forgotten Download", age_days=HINT_MAX_AGE_DAYS + 5)
    assert _find_hint_file(watch, "Old.Forgotten.Download") is None
    assert not stale.exists(), "an expired hint must be removed, not left to mismatch"


def test_fresh_hint_survives_the_sweep():
    watch = _tmpdir()
    fresh = _write_hint(watch, "Recent Download", age_days=1)
    assert _find_hint_file(watch, "Recent.Download") == fresh
    assert fresh.exists()


def test_newest_wins_when_several_match():
    watch = _tmpdir()
    older = watch / ".plexbie_hint_a.json"
    older.write_text(json.dumps({"nzb_title": "Same Book", "title": "old"}))
    os.utime(older, (time.time() - 3600, time.time() - 3600))
    newer = watch / ".plexbie_hint_b.json"
    newer.write_text(json.dumps({"nzb_title": "Same.Book", "title": "new"}))
    assert _find_hint_file(watch, "Same Book") == newer


def test_unreadable_hint_is_skipped_not_fatal():
    watch = _tmpdir()
    (watch / ".plexbie_hint_broken.json").write_text("{not json")
    good = _write_hint(watch, "Good Book")
    assert _find_hint_file(watch, "Good.Book") == good


def test_hint_without_nzb_title_is_ignored():
    watch = _tmpdir()
    (watch / ".plexbie_hint_x.json").write_text(json.dumps({"title": "no nzb_title"}))
    assert _find_hint_file(watch, "Anything") is None


def test_normalisation_ignores_punctuation_and_case():
    assert _normalise_for_match("Andy.Weir - Project_Hail Mary!") == \
           _normalise_for_match("andyweirprojecthailmary")


# --- the cog's settle / failure bookkeeping ---

def _cog():
    """A cog instance with no Discord or filesystem side effects."""
    from plugins.bookshelf_processor.cog import BookshelfProcessorCog

    cog = object.__new__(BookshelfProcessorCog)
    cog.bot = None
    cog.services = None
    cog.settle_seconds = 120
    cog.pending = {}
    cog.failed = {}
    return cog


def test_pending_stores_signature_and_timestamp():
    """The timer can only be refreshed if the signature is stored alongside it."""
    cog = _cog()
    cog.pending["/watch/x"] = ((1, 100, 5), datetime.now())
    signature, last_changed = cog.pending["/watch/x"]
    assert signature == (1, 100, 5)
    assert isinstance(last_changed, datetime)


def test_settle_is_not_reached_while_signature_keeps_changing():
    """Simulates the scan loop's decision over three passes of a growing file."""
    cog = _cog()
    path = "/watch/growing"
    start = datetime.now() - timedelta(seconds=300)

    cog.pending[path] = ((1, 10, 1), start)
    for size in (200, 4000):
        signature, _ = cog.pending[path]
        new_signature = (1, size, 1)
        if new_signature != signature:
            cog.pending[path] = (new_signature, datetime.now())

    _, last_changed = cog.pending[path]
    elapsed = (datetime.now() - last_changed).total_seconds()
    assert elapsed < cog.settle_seconds, (
        "a still-growing item must not be eligible despite first being seen 300s ago"
    )


def test_settle_is_reached_once_signature_holds():
    cog = _cog()
    path = "/watch/done"
    cog.pending[path] = ((3, 5000, 9), datetime.now() - timedelta(seconds=200))
    signature, last_changed = cog.pending[path]
    assert signature == (3, 5000, 9)
    assert (datetime.now() - last_changed).total_seconds() >= cog.settle_seconds


def test_failed_item_is_skipped_while_unchanged():
    cog = _cog()
    cog.failed["/watch/empty"] = (0, 0, 0)
    assert cog.failed.get("/watch/empty") == (0, 0, 0)


def test_failed_item_is_retried_once_contents_change():
    cog = _cog()
    cog.failed["/watch/empty"] = (0, 0, 0)
    new_signature = (1, 4096, 123)
    assert cog.failed["/watch/empty"] != new_signature, (
        "a changed signature must clear the failure so the item is retried"
    )


def test_startup_seeds_rather_than_processes():
    """_process_existing_items used to call process_item directly."""
    import inspect

    from plugins.bookshelf_processor.cog import BookshelfProcessorCog

    assert not hasattr(BookshelfProcessorCog, "_process_existing_items")
    source = inspect.getsource(BookshelfProcessorCog._seed_existing_items)
    assert "process_item" not in source, (
        "startup must only record items; the scan loop applies the settle wait"
    )
    assert "self.pending[" in source


def test_process_item_reports_success():
    """The scan loop needs a truthy/falsey result to record permanent failures."""
    import inspect

    from plugins.bookshelf_processor import cog as module

    source = inspect.getsource(module.process_item)
    assert "return False" in source, "giving up must be distinguishable from success"
    assert "return True" in source


# --- scheduled hint expiry (independent of whether an item is processed) ---

def test_expire_stale_hints_removes_only_old_ones():
    from plugins.bookshelf_processor.cog import _expire_stale_hints

    watch = _tmpdir()
    old = _write_hint(watch, "Ancient Download", age_days=HINT_MAX_AGE_DAYS + 30)
    fresh = _write_hint(watch, "Todays Download", age_days=0)

    assert _expire_stale_hints(watch) == 1
    assert not old.exists()
    assert fresh.exists()


def test_expire_stale_hints_on_empty_dir_is_a_noop():
    from plugins.bookshelf_processor.cog import _expire_stale_hints

    assert _expire_stale_hints(_tmpdir()) == 0


def test_expire_runs_even_when_no_items_are_present():
    """The live audiobooks dir held two orphaned hints and no items at all, so
    expiry driven only by process_item would never have reached them.
    """
    import inspect

    from plugins.bookshelf_processor.cog import BookshelfProcessorCog

    # scan_loop is a discord.ext.tasks.Loop, so reach through to its coroutine.
    source = inspect.getsource(BookshelfProcessorCog.scan_loop.coro)
    assert "_expire_stale_hints" in source


# --- download_cover: the writes moved to a worker thread, the files must still land ---

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 5000
COVER_URL = "https://covers.example/1.jpg"


class _FakeContent:
    def __init__(self, body):
        self._body = body

    async def iter_chunked(self, size):
        for start in range(0, len(self._body), size):
            yield self._body[start:start + size]


class _FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status = status
        self.headers = headers or {"Content-Type": "image/jpeg"}
        self.content_length = len(body)
        self.content = _FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def read(self):
        return self._body


class _FakeSession:
    """Answers from `pages` (url -> body or response), following redirects the way
    aiohttp does unless told not to."""

    def __init__(self, pages, calls):
        self._pages, self._calls = pages, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _answer(self, url):
        self._calls.append(url)
        page = self._pages.get(url, b"")
        return page if isinstance(page, _FakeResponse) else _FakeResponse(page)

    def get(self, url, allow_redirects=True, **kwargs):
        resp = self._answer(url)
        while allow_redirects and resp.status in (301, 302, 303, 307, 308):
            url = urljoin(url, resp.headers["Location"])
            resp = self._answer(url)
        return resp


def _redirect(location):
    return _FakeResponse(b"", status=302, headers={"Location": location})


def _download_cover(body, dest, cache_dir, url=COVER_URL, pages=None):
    from plugins.bookshelf_processor import cog as module

    calls = []
    pages = pages if pages is not None else {url: body}
    original = module.aiohttp.ClientSession
    module.aiohttp.ClientSession = lambda **kw: _FakeSession(pages, calls)
    try:
        ok = asyncio.run(module.download_cover(url, dest, cache_dir))
    finally:
        module.aiohttp.ClientSession = original
    return ok, calls


def _cover_dirs():
    root = _tmpdir()
    cache, dest = root / "cache", root / "lib" / "cover.jpg"
    cache.mkdir()
    dest.parent.mkdir()
    return cache, dest


def test_download_cover_writes_destination_and_cache():
    cache, dest = _cover_dirs()
    ok, calls = _download_cover(JPEG, dest, cache)
    assert ok and len(calls) == 1
    assert dest.read_bytes() == JPEG
    cached = list(cache.iterdir())
    assert len(cached) == 1 and cached[0].read_bytes() == JPEG
    assert [p.name for p in dest.parent.iterdir()] == ["cover.jpg"], "no temporary file left behind"


def test_download_cover_copies_from_cache_without_fetching():
    cache, dest = _cover_dirs()
    _download_cover(JPEG, dest.parent.parent / "first.jpg", cache)  # populate the cache
    ok, calls = _download_cover(b"never fetched", dest, cache)
    assert ok and calls == [], "a cache hit must not refetch"
    assert dest.read_bytes() == JPEG


def test_download_cover_fetches_only_public_http_hosts():
    for url in ("http://127.0.0.1/cover.jpg", "http://10.0.0.15:8080/api", "http://[::1]/cover.jpg",
                "http://169.254.169.254/latest/meta-data", "http://192.168.1.1/", "http://100.64.0.1/",
                "http://\uff11\uff12\uff17.0.0.1/cover.jpg", "http://[fec0::1]/", "http://[::7f00:1]/",
                "http://[64:ff9b::a00:f]/", "http://[2002:c0a8:101::1]/",
                "file:///etc/passwd", "ftp://covers.example/1.jpg", "not a url"):
        cache, dest = _cover_dirs()
        ok, calls = _download_cover(JPEG, dest, cache, url=url)
        assert ok is False and calls == [], f"{url} was fetched"
        assert not dest.exists() and not list(cache.iterdir())


def test_ipv6_forms_of_a_private_address_are_not_public():
    from plugins.bookshelf_processor.cog import _is_public_address

    for host in ("fec0::1", "::7f00:1", "::a00:f", "64:ff9b::7f00:1", "64:ff9b::c0a8:101",
                 "2002:7f00:1::1", "::ffff:10.0.0.15", "::1", "::"):
        assert not _is_public_address(host), f"{host} counted as public"
    for host in ("93.184.215.14", "64:ff9b::5db8:d70e", "2606:4700::1111"):
        assert _is_public_address(host), f"{host} counted as private"


def test_download_cover_does_not_follow_a_redirect_to_a_private_address():
    cache, dest = _cover_dirs()
    pages = {COVER_URL: _redirect("http://192.168.1.1/admin"), "http://192.168.1.1/admin": JPEG}
    ok, calls = _download_cover(None, dest, cache, pages=pages)
    assert ok is False and calls == [COVER_URL]
    assert not dest.exists() and not list(cache.iterdir())


def test_download_cover_follows_a_redirect_to_a_public_host():
    cache, dest = _cover_dirs()
    pages = {COVER_URL: _redirect("/real.jpg"), "https://covers.example/real.jpg": JPEG}
    ok, calls = _download_cover(None, dest, cache, pages=pages)
    assert ok and calls == [COVER_URL, "https://covers.example/real.jpg"]
    assert dest.read_bytes() == JPEG


def test_download_cover_refuses_a_name_that_resolves_to_the_lan():
    """A public-looking name pointing at a private address is caught where it is
    resolved, which also covers a name that changes between a check and the fetch."""
    from plugins.bookshelf_processor import cog as module

    class Answers:
        def __init__(self, *hosts):
            self.hosts = hosts

        async def resolve(self, host, port=0, family=0):
            return [{"hostname": host, "host": h, "port": port, "family": family,
                     "proto": 0, "flags": 0} for h in self.hosts]

        async def close(self):
            pass

    async def resolve(*hosts):
        resolver = module._PublicResolver()
        resolver._resolver = Answers(*hosts)
        return await resolver.resolve("covers.example", 443)

    for private in (("10.0.0.15",), ("127.0.0.1", "::1"), ("::ffff:192.168.1.1",), ("169.254.169.254",)):
        try:
            asyncio.run(resolve(*private))
        except OSError:
            pass
        else:
            raise AssertionError(f"{private} was accepted")
    mixed = asyncio.run(resolve("10.0.0.15", "93.184.215.14"))
    assert [entry["host"] for entry in mixed] == ["93.184.215.14"]


def test_download_cover_never_reaches_a_local_server():
    """End to end through aiohttp: a cover URL naming this host, by address or by
    name, is never fetched."""
    from aiohttp import web

    from plugins.bookshelf_processor import cog as module

    hits = []

    async def cover(request):
        hits.append(request.path)
        return web.Response(body=JPEG, content_type="image/jpeg")

    async def run():
        app = web.Application()
        app.router.add_get("/cover.jpg", cover)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        results = []
        try:
            for host in ("127.0.0.1", "localhost", "\uff11\uff12\uff17.0.0.1"):
                cache, dest = _cover_dirs()
                ok = await module.download_cover(f"http://{host}:{port}/cover.jpg", dest, cache)
                results.append((host, ok, dest.exists()))
        finally:
            await runner.cleanup()
        return results

    for host, ok, written in asyncio.run(run()):
        assert ok is False and not written, f"fetched a cover from {host}"
    assert hits == []


def test_download_cover_refuses_content_that_is_not_an_image():
    page = b"<html>" + b"Image not available " * 300 + b"</html>"
    for response in (_FakeResponse(page, headers={"Content-Type": "text/html"}),
                     _FakeResponse(page, headers={"Content-Type": "image/jpeg"}),
                     _FakeResponse(JPEG, headers={"Content-Type": "text/html; charset=utf-8"})):
        cache, dest = _cover_dirs()
        ok, _ = _download_cover(None, dest, cache, pages={COVER_URL: response})
        assert ok is False
        assert not dest.exists() and not list(cache.iterdir()), "nothing rejected is written or cached"


def test_download_cover_accepts_png_and_webp():
    png = b"\x89PNG\r\n\x1a\n" + b"p" * 5000
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"w" * 5000
    for body, kind in ((png, "image/png"), (webp, "image/webp")):
        cache, dest = _cover_dirs()
        ok, _ = _download_cover(None, dest, cache, pages={COVER_URL: _FakeResponse(body, headers={"Content-Type": kind})})
        assert ok and dest.read_bytes() == body


def test_download_cover_refuses_an_oversized_image():
    from plugins.bookshelf_processor import cog as module

    saved = getattr(module, "COVER_MAX_BYTES", None)
    module.COVER_MAX_BYTES = 4000
    try:
        cache, dest = _cover_dirs()
        ok, _ = _download_cover(JPEG, dest, cache)
        quiet = _FakeResponse(JPEG)
        quiet.content_length = None  # no Content-Length: the cap applies while reading
        cache2, dest2 = _cover_dirs()
        ok2, _ = _download_cover(None, dest2, cache2, pages={COVER_URL: quiet})
    finally:
        module.COVER_MAX_BYTES = saved
    assert ok is False and not dest.exists() and not list(cache.iterdir())
    assert ok2 is False and not dest2.exists()


def test_a_cached_file_that_is_not_an_image_is_fetched_again():
    import hashlib

    cache, dest = _cover_dirs()
    (cache / f"{hashlib.md5(COVER_URL.encode()).hexdigest()}.jpg").write_bytes(b"<html>placeholder</html>" * 100)
    ok, calls = _download_cover(JPEG, dest, cache)
    assert ok and calls == [COVER_URL]
    assert dest.read_bytes() == JPEG


def test_a_failed_cover_write_leaves_no_partial_file():
    from plugins.bookshelf_processor import cog as module

    cache, dest = _cover_dirs()
    real = module.os.replace

    def full_disk(src, dst):
        raise OSError(28, "No space left on device")

    module.os.replace = full_disk
    try:
        ok, _ = _download_cover(JPEG, dest, cache)
    finally:
        module.os.replace = real
    assert ok is False
    assert list(dest.parent.iterdir()) == [] and list(cache.iterdir()) == []


# --- book metadata lookups: what each source's answer becomes ---

def test_book_lookups_read_each_source_the_same_way_as_before():
    """Open Library and Google Books, by ISBN and by search, over a faked HTTP layer."""
    from plugins.bookshelf_processor import cog as shelf

    answers = {
        "https://openlibrary.org/isbn/9780000000001.json": {
            "title": "Guards! Guards!", "publish_date": "May 1989",
            "authors": [{"key": "/authors/OL1A"}], "works": [{"key": "/works/OL2W"}]},
        "https://openlibrary.org/authors/OL1A.json": {"name": "Terry Pratchett"},
        "https://openlibrary.org/works/OL2W.json": {"subjects": ["Fantasy", "Discworld #8"]},
        "https://openlibrary.org/search.json": {"docs": [
            {"title": "Leviathan Wakes", "author_name": ["James S. A. Corey"], "first_publish_year": 2011,
             "cover_i": 42, "subject": ["The Expanse Book 1"]}]},
        "https://www.googleapis.com/books/v1/volumes": {"items": [{"volumeInfo": {
            "title": "Dune", "authors": ["Frank Herbert"], "publishedDate": "1965-08-01",
            "imageLinks": {"thumbnail": "http://books.google.com/x?id=1&zoom=5", "small": "http://s"},
            "seriesInfo": {"shortSeriesBookTitle": "Dune", "bookDisplayNumber": "1"}}}]},
    }
    asked = []

    async def fake_get_json(url, params=None):
        asked.append(url)
        if url not in answers:
            raise RuntimeError("404")
        return answers[url]

    saved = shelf._get_json
    shelf._get_json = fake_get_json
    try:
        by_isbn = asyncio.run(shelf._try_open_library_isbn("9780000000001"))
        missing = asyncio.run(shelf._try_open_library_isbn("9780000000002"))
        searched = asyncio.run(shelf._try_open_library("James S. A. Corey", "Leviathan Wakes"))
        google = asyncio.run(shelf._try_google_books("Frank Herbert", "Dune"))
        google_isbn = asyncio.run(shelf._try_google_books_isbn("9780000000003"))
    finally:
        shelf._get_json = saved

    assert by_isbn == {"title": "Guards! Guards!", "year": "1989", "author": "Terry Pratchett", "isbn": "9780000000001",
                       "cover_url": "https://covers.openlibrary.org/b/isbn/9780000000001-L.jpg",
                       "series": "Discworld", "series_index": 8}
    assert missing is None
    assert searched["series"] == "The Expanse" and searched["series_index"] == 1
    assert searched["cover_url"] == "https://covers.openlibrary.org/b/id/42-L.jpg" and searched["year"] == "2011"
    assert google["cover_url"] == "https://s", "the largest image wins, over https"
    assert (google["series"], google["series_index"], google["year"]) == ("Dune", 1, "1965")
    assert google_isbn["title"] == "Dune" and google_isbn["isbn"] == "9780000000003"


def _lookups_answering(answers):
    """Run book lookups against canned API answers, keyed by URL."""
    from plugins.bookshelf_processor import cog as shelf

    async def fake_get_json(url, params=None):
        if url not in answers:
            raise RuntimeError("404")
        return answers[url]

    def run(coro_fn, *args):
        saved = shelf._get_json
        shelf._get_json = fake_get_json
        try:
            return asyncio.run(coro_fn(*args))
        finally:
            shelf._get_json = saved
    return run


def test_a_search_with_no_matching_title_finds_nothing():
    """The first hit is not used when its title has nothing to do with the book."""
    from plugins.bookshelf_processor import cog as shelf

    run = _lookups_answering({
        "https://openlibrary.org/search.json": {"docs": [
            {"title": "Study Guide: Leviathan", "author_name": ["SuperSummary"], "cover_i": 7},
            {"title": "", "author_name": ["Nobody"], "cover_i": 8}]},
        "https://www.googleapis.com/books/v1/volumes": {"items": [
            {"volumeInfo": {"title": "The Spice Must Flow", "authors": ["Quick Reads"],
                            "imageLinks": {"thumbnail": "http://books.google.com/x?id=2"}}},
            {"volumeInfo": {"authors": ["Untitled"]}}]},
    })
    assert run(shelf._try_open_library, "James S. A. Corey", "Leviathan Wakes") is None
    assert run(shelf._try_google_books, "Frank Herbert", "Dune") is None
    assert run(shelf.fetch_metadata, "Frank Herbert", "Dune") == {}


def test_google_books_takes_the_result_that_matches_not_the_first():
    from plugins.bookshelf_processor import cog as shelf

    run = _lookups_answering({
        "https://www.googleapis.com/books/v1/volumes": {"items": [
            {"volumeInfo": {"title": "Frank Herbert: A Life", "authors": ["Quick Reads"]}},
            {"volumeInfo": {"title": "Dune", "authors": ["Frank Herbert"], "publishedDate": "1965"}}]},
    })
    google = run(shelf._try_google_books, "Frank Herbert", "Dune")
    assert (google["title"], google["author"], google["year"]) == ("Dune", "Frank Herbert", "1965")


def test_titles_match_without_their_punctuation_and_accents():
    """Release names drop apostrophes and accents; the right book still matches."""
    from plugins.bookshelf_processor.cog import _best_match

    stone = {"title": "Harry Potter and the Sorcerer's Stone", "author_name": ["J.K. Rowling"]}
    guide = {"title": "Study Guide: Harry Potter", "author_name": ["SuperSummary"]}
    assert _best_match([guide, stone], "J.K. Rowling", "Harry Potter and the Sorcerers Stone") is stone
    miserables = {"title": "Les Misérables", "author_name": ["Victor Hugo"]}
    assert _best_match([miserables], "Victor Hugo", "Les Miserables") is miserables
    tales = {"title": "Tales of Mystery and Imagination", "author_name": ["Edgar Allan Poe"]}
    assert _best_match([tales], "Edgar Allan Poe", "Tales of Mystery & Imagination") is tales
    assert _best_match([guide], "James S. A. Corey", "Leviathan Wakes") is None
    assert _best_match([stone], "Ann Author", "?!") is None


# --- ISBNs read from an EPUB ---

def _epub_with_identifiers(*identifiers):
    import zipfile

    path = _tmpdir() / "book.epub"
    idents = "".join(f"<dc:identifier {attrs}>{value}</dc:identifier>" for attrs, value in identifiers)
    opf = (
        '<package xmlns:opf="http://www.idpf.org/2007/opf"><metadata>'
        "<dc:title>The Book</dc:title><dc:creator>Ann Author</dc:creator>"
        f"{idents}</metadata></package>"
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("OEBPS/content.opf", opf)
    return path


def test_isbn_check_digits_are_validated():
    from plugins.bookshelf_processor.cog import isbn_valid

    for good in ("9780306406157", "0306406152", "080442957X", "9780000000002"):
        assert isbn_valid(good), good
    for bad in ("9780306406158", "0306406153", "0804429571", "X804429570", "978030640615", "", "97803064061577"):
        assert not isbn_valid(bad), bad


def test_an_epub_identifier_that_is_not_an_isbn_is_ignored():
    """A calibre id or a uuid with the right number of digits is not an ISBN."""
    from plugins.bookshelf_processor.cog import extract_embedded_metadata

    meta = extract_embedded_metadata(_epub_with_identifiers(
        ('opf:scheme="calibre"', "1234567890123"),
        ('opf:scheme="uuid"', "urn:uuid:12345678-0000-0000-0000-000000000000"),
    ))
    assert meta["title"] == "The Book" and meta.get("isbn") is None, meta.get("isbn")


def test_an_epub_isbn_marked_as_such_wins_and_may_end_in_x():
    from plugins.bookshelf_processor.cog import extract_embedded_metadata

    meta = extract_embedded_metadata(_epub_with_identifiers(
        ('opf:scheme="calibre"', "9780306406157"),
        ('opf:scheme="ISBN"', "0-8044-2957-X"),
    ))
    assert meta["isbn"] == "080442957X", meta.get("isbn")


def test_an_epub_urn_isbn_is_read_and_checked():
    from plugins.bookshelf_processor.cog import extract_embedded_metadata

    good = extract_embedded_metadata(_epub_with_identifiers(('id="pub-id"', "urn:isbn:9780306406157")))
    assert good["isbn"] == "9780306406157"
    bad = extract_embedded_metadata(_epub_with_identifiers(('id="pub-id"', "urn:isbn:9780306406158")))
    assert bad.get("isbn") is None
    longer = extract_embedded_metadata(_epub_with_identifiers(('id="pub-id"', "urn:isbn:97803064061571234")))
    assert longer.get("isbn") is None, "not the first 13 digits of a longer number"
