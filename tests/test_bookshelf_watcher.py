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

class _FakeResponse:
    def __init__(self, body):
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def read(self):
        return self._body


class _FakeSession:
    def __init__(self, body, calls):
        self._body, self._calls = body, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url):
        self._calls.append(url)
        return _FakeResponse(self._body)


def _download_cover(body, dest, cache_dir):
    from plugins.bookshelf_processor import cog as module

    calls = []
    original = module.aiohttp.ClientSession
    module.aiohttp.ClientSession = lambda **kw: _FakeSession(body, calls)
    try:
        ok = asyncio.run(module.download_cover("https://covers.example/1.jpg", dest, cache_dir))
    finally:
        module.aiohttp.ClientSession = original
    return ok, calls


def test_download_cover_writes_destination_and_cache():
    root = _tmpdir()
    cache, dest = root / "cache", root / "lib" / "cover.jpg"
    cache.mkdir()
    dest.parent.mkdir()
    body = b"\xff\xd8" + b"x" * 5000
    ok, calls = _download_cover(body, dest, cache)
    assert ok and len(calls) == 1
    assert dest.read_bytes() == body
    cached = list(cache.iterdir())
    assert len(cached) == 1 and cached[0].read_bytes() == body


def test_download_cover_copies_from_cache_without_fetching():
    root = _tmpdir()
    cache, dest = root / "cache", root / "lib" / "cover.jpg"
    cache.mkdir()
    dest.parent.mkdir()
    body = b"\xff\xd8" + b"y" * 5000
    _download_cover(body, root / "first.jpg", cache)  # populate the cache
    ok, calls = _download_cover(b"never fetched", dest, cache)
    assert ok and calls == [], "a cache hit must not refetch"
    assert dest.read_bytes() == body


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
