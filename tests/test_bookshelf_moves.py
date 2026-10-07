# path: tests/test_bookshelf_moves.py
"""Filing a book never replaces, loses or half-moves its files.

Regression coverage for two defects:
  * Every book file was moved to `dest / file.name`, flattening sub-folders.
    A multi-disc audiobook (CD1/01.mp3, CD2/01.mp3) collapsed into one 01.mp3
    holding the last disc, because shutil.move silently replaces an existing
    file, and the source tree holding the only other copy was then deleted.
  * When some moves failed, the source was kept but the hint was still deleted,
    the book was announced and the requester DM'd, and process_item reported
    success. The leftovers were picked up again as a new item and filed into
    "Title (2)" with another announcement.
"""
import asyncio
import contextlib
import errno
import inspect
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import conftest  # noqa: F401

from plugins.bookshelf_processor import cog as shelf

RELEASE = "Ann Author - The Book (Unabridged)"
DISC1 = b"disc1-track1" * 50
DISC2 = b"disc2-track1" * 70


# --- fixtures ----------------------------------------------------------------

class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class _FakeBot:
    """Reads only what process_item reads: services.config and get_channel."""

    def __init__(self):
        self.channel = _Channel()
        self.services = SimpleNamespace(config=SimpleNamespace(updates_channel_id=1))

    def get_channel(self, channel_id):
        return self.channel if channel_id == 1 else None


def _setup(tmp):
    """watch/<release>/CD1/01.mp3 + CD2/01.mp3, its hint, and a library that does not exist yet."""
    root = Path(tmp)
    watch = root / "watch"
    source = watch / RELEASE
    (source / "CD1").mkdir(parents=True)
    (source / "CD2").mkdir()
    (source / "CD1" / "01.mp3").write_bytes(DISC1)
    (source / "CD2" / "01.mp3").write_bytes(DISC2)
    hint = watch / ".plexbie_hint_x.json"
    hint.write_text(json.dumps({
        "nzb_title": RELEASE, "author": "Ann Author", "title": "The Book", "requested_by": "42",
    }))
    return source, hint, root / "lib_a"


def _book_folder(lib):
    return lib / "Ann Author" / "The Book"


@contextlib.contextmanager
def _recorded_dms():
    calls = []
    original = shelf.dm_user_id

    async def fake_dm(bot, services, user_id, **kwargs):
        calls.append(user_id)
        return True

    shelf.dm_user_id = fake_dm
    try:
        yield calls
    finally:
        shelf.dm_user_id = original


@contextlib.contextmanager
def _fail_moves_from(*matches):
    """Fail every move whose source path contains one of `matches`.

    A rename fails with EIO, which sends both shutil.move and the new mover to
    their copy fallback, and the copy then fails with ENOSPC (a full disk).
    """
    real = (os.rename, shutil.copyfileobj, shutil.copyfile, shutil.copy2)

    def hit(path):
        return any(m in str(path) for m in matches)

    def rename(src, dst, *args, **kwargs):
        if hit(src):
            raise OSError(errno.EIO, "Input/output error", str(src))
        return real[0](src, dst, *args, **kwargs)

    def copyfileobj(fsrc, fdst, *args, **kwargs):
        if hit(getattr(fsrc, "name", "")):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real[1](fsrc, fdst, *args, **kwargs)

    def copyfile(src, dst, *args, **kwargs):
        if hit(src):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real[2](src, dst, *args, **kwargs)

    def copy2(src, dst, *args, **kwargs):
        if hit(src):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real[3](src, dst, *args, **kwargs)

    os.rename, shutil.copyfileobj, shutil.copyfile, shutil.copy2 = rename, copyfileobj, copyfile, copy2
    try:
        yield
    finally:
        os.rename, shutil.copyfileobj, shutil.copyfile, shutil.copy2 = real


def _process(source, lib, bot):
    return asyncio.run(shelf.process_item(source, "audiobook", lib, lib.parent / "lib_e", bot=bot))


def _names(paths):
    return [name for _, name in paths]


# --- planning the destination names (pure) ------------------------------------

def test_unique_names_keep_todays_flat_layout():
    src = Path("/watch/Book")
    flat = [src / "01.mp3", src / "02.mp3"]
    assert shelf.plan_book_moves(src, flat, "audiobook") == [(f, f.name) for f in flat]
    nested = [src / "a" / "01.mp3", src / "b" / "02.mp3"]
    assert _names(shelf.plan_book_moves(src, nested, "audiobook")) == ["01.mp3", "02.mp3"]


def test_same_named_tracks_across_disc_folders_get_disc_prefixes():
    src = Path("/watch/Book")
    files = [src / "CD1" / "01.mp3", src / "CD1" / "02.mp3", src / "CD2" / "01.mp3"]
    plan = shelf.plan_book_moves(src, files, "audiobook")
    assert [f for f, _ in plan] == files, "the plan keeps the order it was given"
    assert _names(plan) == ["Disc 01 - 01.mp3", "Disc 01 - 02.mp3", "Disc 02 - 01.mp3"]


def test_disc_numbers_follow_natural_folder_order_without_gaps():
    src = Path("/watch/Book")

    def discs(*folders):
        files = [src / f / "01.mp3" if f else src / "01.mp3" for f in folders]
        return _names(shelf.plan_book_moves(src, files, "audiobook"))

    # sorted() puts CD10 before CD2; the household expects CD2 second.
    assert discs("CD1", "CD10", "CD2") == ["Disc 01 - 01.mp3", "Disc 03 - 01.mp3", "Disc 02 - 01.mp3"]
    assert discs("CD1", "CD3") == ["Disc 01 - 01.mp3", "Disc 02 - 01.mp3"], "ordinals, never gaps"
    assert discs("Part A", "Part B") == ["Disc 01 - 01.mp3", "Disc 02 - 01.mp3"]
    assert discs("CD1", "", "CD2") == ["Disc 02 - 01.mp3", "Disc 01 - 01.mp3", "Disc 03 - 01.mp3"]


def test_case_only_differences_count_as_collisions():
    """On an SMB share Track.mp3 and track.MP3 are the same file."""
    src = Path("/watch/Book")
    plan = shelf.plan_book_moves(src, [src / "a" / "Track.mp3", src / "b" / "track.MP3"], "audiobook")
    assert _names(plan) == ["Disc 01 - Track.mp3", "Disc 02 - track.MP3"]


_ABS_DISC_RE = re.compile(r"\b(disc|cd) ?(\d\d?)\b", re.IGNORECASE)


def _abs_disc_and_track(name):
    """What Audiobookshelf's scanner reads from a file name: the disc from
    'Disc N' / 'CD N', then the first number left once that is removed."""
    stem = name.rsplit(".", 1)[0]
    match = _ABS_DISC_RE.search(stem)
    assert match, f"no disc number in {name!r}"
    rest = stem[:match.start()] + stem[match.end():]
    return int(match.group(2)), int(re.search(r"\d+", rest).group())


def test_planned_names_read_as_consecutive_discs_in_audiobookshelf():
    src = Path("/watch/Book")
    natural = [src / "CD1" / "01.mp3", src / "CD1" / "02.mp3", src / "CD2" / "01.mp3",
               src / "CD2" / "02.mp3", src / "CD10" / "01.mp3"]
    plan = shelf.plan_book_moves(src, sorted(natural), "audiobook")
    parsed = {f: _abs_disc_and_track(name) for f, name in plan}
    discs = sorted({disc for disc, _ in parsed.values()})
    assert discs == list(range(1, len(discs) + 1)), (
        "Audiobookshelf only orders by disc when the disc numbers have no gaps"
    )
    assert sorted(parsed, key=parsed.get) == natural


def test_ebook_collisions_are_prefixed_with_their_folder():
    src = Path("/watch/Book")
    files = [src / "converted" / "Book.epub", src / "retail" / "Book.epub"]
    assert _names(shelf.plan_book_moves(src, files, "ebook")) == [
        "converted - Book.epub", "retail - Book.epub",
    ]


def test_planned_names_are_always_unique():
    src = Path("/watch/Book")
    ebooks = sorted([src / "retail - Book.epub", src / "retail" / "Book.epub", src / "converted" / "Book.epub"])
    assert _names(shelf.plan_book_moves(src, ebooks, "ebook")) == [
        "converted - Book.epub", "retail - Book.epub", "retail - Book (2).epub",
    ]
    nested = [src / "a - b" / "x.epub", src / "a" / "b" / "x.epub"]
    assert _names(shelf.plan_book_moves(src, nested, "ebook")) == ["a - b - x.epub", "a - b - x (2).epub"]
    # Same name but for case, in one folder: possible on the download disk,
    # one file on a case-insensitive library share.
    audio = [src / "CD1" / "Track.mp3", src / "CD1" / "track.MP3", src / "CD2" / "Track.mp3"]
    assert _names(shelf.plan_book_moves(src, audio, "audiobook")) == [
        "Disc 01 - Track.mp3", "Disc 01 - track (2).MP3", "Disc 02 - Track.mp3",
    ]


def test_a_single_file_download_keeps_its_name():
    src = Path("/watch/Book.m4b")
    assert shelf.plan_book_moves(src, [src], "audiobook") == [(src, "Book.m4b")]


# --- never overwrite ----------------------------------------------------------

def test_move_never_replaces_an_existing_file():
    with tempfile.TemporaryDirectory() as tmp:
        src, target = Path(tmp) / "a.mp3", Path(tmp) / "b.mp3"
        src.write_bytes(b"incoming")
        target.write_bytes(b"already here")
        try:
            shelf._move_no_clobber(src, target)
        except FileExistsError:
            pass
        else:
            raise AssertionError("moving onto an existing file must refuse")
        assert src.read_bytes() == b"incoming"
        assert target.read_bytes() == b"already here"


def test_cross_device_copy_never_replaces_and_leaves_no_partial_file():
    real_rename, real_copyfileobj = os.rename, shutil.copyfileobj

    def cross_device(src, dst, *args, **kwargs):
        # Another disk for the move itself; the copy's own rename beside the
        # target stays on one disk.
        if str(src).endswith(".mp3"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst, *args, **kwargs)

    def half_then_full_disk(fsrc, fdst, *args, **kwargs):
        fdst.write(fsrc.read(len(DISC1) // 2))
        raise OSError(errno.ENOSPC, "No space left on device")

    with tempfile.TemporaryDirectory() as tmp:
        src, target = Path(tmp) / "a.mp3", Path(tmp) / "out" / "a.mp3"
        target.parent.mkdir()
        src.write_bytes(DISC1)
        os.utime(src, (1_600_000_000, 1_600_000_000))
        os.rename = cross_device
        try:
            shelf._move_no_clobber(src, target)
        finally:
            os.rename = real_rename
        assert not src.exists()
        assert target.read_bytes() == DISC1
        assert int(target.stat().st_mtime) == 1_600_000_000, "the copy keeps the file's times"

        src2, target2 = Path(tmp) / "b.mp3", Path(tmp) / "out" / "b.mp3"
        src2.write_bytes(DISC1)
        os.rename, shutil.copyfileobj = cross_device, half_then_full_disk
        try:
            shelf._move_no_clobber(src2, target2)
        except OSError:
            pass
        else:
            raise AssertionError("a failed copy must raise")
        finally:
            os.rename, shutil.copyfileobj = real_rename, real_copyfileobj
        assert not target2.exists(), "a half-written copy must not be left behind"
        assert sorted(p.name for p in target2.parent.iterdir()) == ["a.mp3"]
        assert src2.read_bytes() == DISC1


def test_a_copy_only_gets_its_real_name_once_it_is_complete():
    """A restart mid-copy must never leave a short file under a book file's
    name: recovery would take it for the real thing."""
    real_rename, real_copyfileobj = os.rename, shutil.copyfileobj
    written_to = []

    def cross_device(src, dst, *args, **kwargs):
        if str(src).endswith("a.mp3"):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst, *args, **kwargs)

    def watching(fsrc, fdst, *args, **kwargs):
        written_to.append(fdst.name)
        return real_copyfileobj(fsrc, fdst, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
        src, target = Path(tmp) / "a.mp3", Path(tmp) / "out" / "a.mp3"
        target.parent.mkdir()
        src.write_bytes(DISC1)
        os.rename, shutil.copyfileobj = cross_device, watching
        try:
            shelf._move_no_clobber(src, target)
        finally:
            os.rename, shutil.copyfileobj = real_rename, real_copyfileobj
        assert written_to and str(target) not in written_to, f"copied straight into {written_to}"
        assert target.read_bytes() == DISC1
        assert sorted(p.name for p in target.parent.iterdir()) == ["a.mp3"]


def test_a_file_already_in_the_destination_makes_the_move_fail():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "watch" / "Book"
        source.mkdir(parents=True)
        (source / "01.mp3").write_bytes(DISC1)
        (source / "02.mp3").write_bytes(DISC2)
        library = Path(tmp) / "lib"
        dest = library / "Author" / "Book"
        dest.mkdir(parents=True)
        (dest / "02.mp3").write_bytes(b"someone else's file")

        plan = shelf.plan_book_moves(source, sorted(source.iterdir()), "audiobook")
        result = shelf._place_book(plan, dest, source, library)

        assert not result.ok
        assert result.failed_file == "02.mp3"
        assert "already exists" in result.error
        assert (dest / "02.mp3").read_bytes() == b"someone else's file"
        assert (source / "01.mp3").read_bytes() == DISC1, "the file already moved is put back"
        assert sorted(p.name for p in dest.iterdir()) == ["02.mp3"]


# --- end to end through process_item -----------------------------------------

def test_multi_disc_release_files_every_track_then_removes_the_source():
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        with _recorded_dms():
            assert _process(source, lib, _FakeBot()) is True
        book = _book_folder(lib)
        filed = sorted(p.name for p in book.iterdir())
        assert filed == ["Disc 01 - 01.mp3", "Disc 02 - 01.mp3", "metadata.opf"], filed
        assert (book / "Disc 01 - 01.mp3").read_bytes() == DISC1
        assert (book / "Disc 02 - 01.mp3").read_bytes() == DISC2
        assert not source.exists()
        assert not hint.exists()


def _assert_untouched(source, hint, lib, before):
    assert (source / "CD1" / "01.mp3").read_bytes() == DISC1
    assert (source / "CD2" / "01.mp3").read_bytes() == DISC2
    assert shelf._item_signature(source) == before, (
        "the scan loop records a failure against this signature; it must not move"
    )
    assert hint.exists(), "the hint carries the requester; it must outlive a failed move"
    assert not (lib / "Ann Author").exists(), "no half-filed book and no empty author folder"


def test_failed_move_keeps_source_and_hint_and_sends_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        before = shelf._item_signature(source)
        bot = _FakeBot()
        with _recorded_dms() as dms, _fail_moves_from("CD2"):
            filed = _process(source, lib, bot)
        assert filed is False, f"a partly failed move reported {filed!r}"
        _assert_untouched(source, hint, lib, before)
        assert bot.channel.sent == [], "nothing may be announced for a book that did not file"
        assert dms == []


def test_retry_after_a_failed_move_uses_the_same_folder():
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        bot = _FakeBot()
        with _recorded_dms() as dms:
            with _fail_moves_from("CD2"):
                assert _process(source, lib, bot) is False
            assert _process(source, lib, bot) is True
        book = _book_folder(lib)
        assert (book / "Disc 01 - 01.mp3").read_bytes() == DISC1
        assert (book / "Disc 02 - 01.mp3").read_bytes() == DISC2
        assert not (lib / "Ann Author" / "The Book (2)").exists()
        assert len(bot.channel.sent) == 1 and dms == ["42"], "announced once, when it filed"
        assert not hint.exists()


def test_files_that_cannot_be_put_back_are_recovered_next_time():
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        book = _book_folder(lib)
        with _recorded_dms():
            with _fail_moves_from("CD2", "Disc 01"):
                assert _process(source, lib, _FakeBot()) is False
            assert (book / shelf.MOVE_MARKER).exists()
            assert (book / "Disc 01 - 01.mp3").read_bytes() == DISC1
            assert (source / "CD2" / "01.mp3").exists()
            assert hint.exists()

            assert _process(source, lib, _FakeBot()) is True
        assert (book / "Disc 01 - 01.mp3").read_bytes() == DISC1
        assert (book / "Disc 02 - 01.mp3").read_bytes() == DISC2
        assert not (book / shelf.MOVE_MARKER).exists()
        assert not (lib / "Ann Author" / "The Book (2)").exists()
        assert not source.exists()


def _start_earlier_attempt(source, lib):
    """Lay down what an attempt cut short leaves behind: its record and marker,
    with disc 1 already moved into the book folder."""
    files = [source / "CD1" / "01.mp3", source / "CD2" / "01.mp3"]
    plan = shelf.plan_book_moves(source, files, "audiobook")
    book = _book_folder(lib)
    book.mkdir(parents=True)
    shelf._write_filing_record(source, book, {"author": "Ann Author", "title": "The Book"})
    shelf._write_move_marker(book, source, plan)
    os.rename(files[0], book / "Disc 01 - 01.mp3")
    return book


def test_interrupted_copies_in_either_direction_are_discarded_and_redone():
    """A restart mid-copy leaves only a temporary .plexbie-part file: in the
    book folder for a copy in, in the download for a copy back."""
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        book = _start_earlier_attempt(source, lib)
        (book / ("Disc 02 - 01.mp3" + shelf.PART_SUFFIX)).write_bytes(DISC2[:100])
        (source / "CD1" / ("01.mp3" + shelf.PART_SUFFIX)).write_bytes(DISC1[:100])

        with _recorded_dms():
            assert _process(source, lib, _FakeBot()) is True
        assert sorted(p.name for p in book.iterdir()) == ["Disc 01 - 01.mp3", "Disc 02 - 01.mp3", "metadata.opf"]
        assert (book / "Disc 01 - 01.mp3").read_bytes() == DISC1
        assert (book / "Disc 02 - 01.mp3").read_bytes() == DISC2
        assert not (lib / "Ann Author" / "The Book (2)").exists()


def test_a_whole_copy_whose_original_was_not_yet_removed_is_dropped_and_redone():
    """A restart between finishing a copy and deleting its original leaves the
    same file in both places."""
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        book = _start_earlier_attempt(source, lib)
        shutil.copy2(source / "CD2" / "01.mp3", book / "Disc 02 - 01.mp3")

        with _recorded_dms():
            assert _process(source, lib, _FakeBot()) is True
        assert (book / "Disc 01 - 01.mp3").read_bytes() == DISC1
        assert (book / "Disc 02 - 01.mp3").read_bytes() == DISC2
        assert not (book / shelf.MOVE_MARKER).exists()
        assert not (lib / "Ann Author" / "The Book (2)").exists()


def test_recovery_never_deletes_a_copy_that_differs_from_its_original():
    """The book folder holds the whole disc 1 and the download a short file of
    the same name. Recovery cannot know which is right, so it deletes neither,
    and nothing is filed or announced."""
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        book = _start_earlier_attempt(source, lib)
        (source / "CD1" / "01.mp3").write_bytes(DISC1[:100])

        bot = _FakeBot()
        with _recorded_dms() as dms:
            assert _process(source, lib, bot) is False
        assert (book / "Disc 01 - 01.mp3").read_bytes() == DISC1
        assert (source / "CD1" / "01.mp3").read_bytes() == DISC1[:100]
        assert (source / "CD2" / "01.mp3").read_bytes() == DISC2
        assert (book / shelf.MOVE_MARKER).exists()
        assert bot.channel.sent == [] and dms == []
        assert hint.exists()


def _stop_before_marking_complete(source, lib, bot):
    real = shelf._clear_move_marker

    def restarted(dest):
        raise RuntimeError("stopped")

    shelf._clear_move_marker = restarted
    try:
        with _recorded_dms():
            asyncio.run(shelf.process_item(source, "audiobook", lib, lib.parent / "lib_e", bot=bot))
    except RuntimeError:
        pass
    else:
        raise AssertionError("the injected stop did not happen")
    finally:
        shelf._clear_move_marker = real


def test_a_restart_after_every_file_moved_is_finished_on_the_next_attempt():
    """Every file reached the book folder, then the bot stopped before the book
    was marked complete. The download still holds its emptied disc folders."""
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        bot = _FakeBot()
        _stop_before_marking_complete(source, lib, bot)
        book = _book_folder(lib)
        assert (book / shelf.MOVE_MARKER).exists()
        assert shelf.find_book_files(source, "audiobook") == []

        with _recorded_dms() as dms:
            assert _process(source, lib, bot) is True
        assert (book / "Disc 01 - 01.mp3").read_bytes() == DISC1
        assert (book / "Disc 02 - 01.mp3").read_bytes() == DISC2
        assert not (book / shelf.MOVE_MARKER).exists()
        assert not (lib / "Ann Author" / "The Book (2)").exists()
        assert not source.exists() and not hint.exists()
        assert len(bot.channel.sent) == 1 and dms == ["42"]
        assert not list(source.parent.glob(".plexbie_filing_*")), "the filing record goes with the marker"


def test_a_failed_retry_keeps_the_folder_it_left_metadata_in():
    """Stopped after filing, then the retry's move fails: the folder still
    holds the first attempt's metadata.opf, and stays this download's."""
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        bot = _FakeBot()
        _stop_before_marking_complete(source, lib, bot)
        book = _book_folder(lib)
        with _recorded_dms() as dms:
            with _fail_moves_from("CD2"):
                assert _process(source, lib, bot) is False
            assert (book / shelf.MOVE_MARKER).exists()
            assert bot.channel.sent == [] and dms == []
            assert _process(source, lib, bot) is True
        assert not (lib / "Ann Author" / "The Book (2)").exists()
        assert (book / "Disc 01 - 01.mp3").read_bytes() == DISC1
        assert (book / "Disc 02 - 01.mp3").read_bytes() == DISC2
        assert len(bot.channel.sent) == 1 and dms == ["42"]


def test_a_retry_without_a_hint_files_into_the_folder_it_started():
    """With no hint the metadata comes from the files left and a live lookup,
    which can answer differently the second time (here: a series, then none)."""
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        hint.unlink()
        answers = [{"series": "Saga"}, {}]
        real_fetch = shelf.fetch_metadata

        async def lookup(author, title, isbn=None):
            return answers.pop(0) if answers else {}

        shelf.fetch_metadata = lookup
        try:
            with _recorded_dms():
                with _fail_moves_from("CD2", "Disc 01"):
                    assert _process(source, lib, _FakeBot()) is False
                assert _process(source, lib, _FakeBot()) is True
        finally:
            shelf.fetch_metadata = real_fetch
        folders = {p.parent for p in lib.rglob("*.mp3")}
        assert len(folders) == 1, f"the book was split across {sorted(map(str, folders))}"
        book = folders.pop()
        assert book.parent.name == "Saga", "filed where the first attempt started"
        assert (book / "Disc 01 - 01.mp3").read_bytes() == DISC1
        assert (book / "Disc 02 - 01.mp3").read_bytes() == DISC2
        assert not list(lib.rglob(shelf.MOVE_MARKER))


def test_a_metadata_opf_failure_does_not_unfile_a_filed_book():
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        bot = _FakeBot()
        real = shelf.generate_opf

        def full_disk(meta, path):
            raise OSError(errno.ENOSPC, "No space left on device")

        shelf.generate_opf = full_disk
        try:
            with _recorded_dms() as dms:
                assert _process(source, lib, bot) is True
        finally:
            shelf.generate_opf = real
        book = _book_folder(lib)
        assert sorted(p.name for p in book.iterdir()) == ["Disc 01 - 01.mp3", "Disc 02 - 01.mp3"]
        assert not source.exists() and not hint.exists()
        assert len(bot.channel.sent) == 1 and dms == ["42"]


def test_a_filing_record_is_removed_once_its_download_is_gone():
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        gone = source.parent / "Removed Download"
        shelf._write_filing_record(source, lib / "a", {"title": "x"})
        shelf._write_filing_record(gone, lib / "b", {"title": "y"})
        assert len(list(source.parent.glob(".plexbie_filing_*.json"))) == 2

        assert shelf._expire_orphan_filing_records(source.parent) == 1
        assert [p.name for p in source.parent.glob(".plexbie_filing_*.json")] == [
            shelf._filing_record_path(source).name
        ]
        assert hint.exists(), "hints have their own expiry"


def test_a_folder_holding_another_book_still_gets_a_numbered_name():
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "watch" / "Book"
        base = Path(tmp) / "lib" / "Author" / "Book"
        assert shelf._choose_destination(base, source) == base, "a missing folder is used"
        base.mkdir(parents=True)
        assert shelf._choose_destination(base, source) == base, "so is an empty one"

        (base / "01.mp3").write_bytes(b"another copy")
        assert shelf._choose_destination(base, source) == base.parent / "Book (2)"

        (base / "01.mp3").unlink()
        shelf._write_move_marker(base, Path(tmp) / "watch" / "Other", [])
        assert shelf._choose_destination(base, source) == base.parent / "Book (2)"

        shelf._write_move_marker(base, source, [])
        assert shelf._choose_destination(base, source) == base, (
            "this download's own unfinished attempt is picked up, not numbered"
        )


# --- wiring --------------------------------------------------------------------

def test_scan_loop_records_a_failed_move_and_does_not_retry_it_while_unchanged():
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        # Junk is deleted by the first attempt, before the move fails. That
        # alone must not count as the download changing.
        (source / "release.nfo").write_text("nfo")
        cog = object.__new__(shelf.BookshelfProcessorCog)
        cog.bot = _FakeBot()
        cog.services = cog.bot.services
        cog.settle_seconds = 120
        cog.pending = {str(source): (shelf._item_signature(source), datetime.now() - timedelta(seconds=300))}
        cog.failed = {}
        cog.audiobook_watch = source.parent
        cog.ebook_watch = Path(tmp) / "ebooks"
        cog.audiobook_lib = lib
        cog.ebook_lib = Path(tmp) / "lib_e"
        cog.cache_dir = Path(tmp) / "cache"
        cog._last_hint_sweep = None

        calls = []
        real = shelf.process_item

        async def counting(*args, **kwargs):
            calls.append(args[0])
            return await real(*args, **kwargs)

        shelf.process_item = counting
        try:
            with _recorded_dms() as dms, _fail_moves_from("CD2"):
                asyncio.run(shelf.BookshelfProcessorCog.scan_loop.coro(cog))
                assert calls == [source]
                assert cog.failed.get(str(source)) == shelf._item_signature(source)

                for _ in range(2):
                    asyncio.run(shelf.BookshelfProcessorCog.scan_loop.coro(cog))
                assert calls == [source], "an unchanged failed item is not retried"
        finally:
            shelf.process_item = real
        assert cog.bot.channel.sent == [] and dms == []
        assert hint.exists()


def test_moves_and_destination_choice_run_off_the_event_loop():
    source = inspect.getsource(shelf.process_item)
    assert "run_blocking(_place_book" in source
    assert "run_blocking(_choose_destination" in source
    assert "run_blocking(_recover_earlier_attempt" in source
    assert "run_blocking(_read_filing_record" in source
    assert "_place_book(" not in source, "never called on the loop itself"
    assert "failed_moves" not in source


# --- what a hint may carry ------------------------------------------------------

def test_a_meta_file_inside_the_download_is_ignored():
    """Only the hint the bot wrote beside the download counts. A .plexbie_meta.json
    inside it came with the release and could name any book, cover URL or member."""
    with tempfile.TemporaryDirectory() as tmp:
        source, hint, lib = _setup(tmp)
        (source / ".plexbie_meta.json").write_text(json.dumps({
            "author": "Someone Else", "title": "Planted", "cover_url": "http://127.0.0.1:8080/admin",
            "requested_by": "666", "requested_by_plex_name": "victim",
        }))
        covers = []
        real_cover = shelf.download_cover

        async def recorded_cover(url, dest, cache_dir=None):
            covers.append(url)
            return False

        shelf.download_cover = recorded_cover
        try:
            with _recorded_dms() as dms:
                assert _process(source, lib, _FakeBot()) is True
        finally:
            shelf.download_cover = real_cover
        assert _book_folder(lib).is_dir(), "filed under the bot's own hint"
        assert not (lib / "Someone Else").exists()
        assert dms == ["42"], "only the member who asked is told"
        assert covers == []
        assert not hint.exists(), "the bot's own hint is the one consumed"


def test_hint_fields_of_the_wrong_type_or_size_are_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".plexbie_hint_x.json"
        path.write_text(json.dumps({
            "nzb_title": RELEASE, "title": ["not", "text"], "author": "A" * 5000,
            "series": "Saga", "series_index": '1"/><evil/>', "year": "next spring",
            "isbn": {"x": 1}, "cover_url": "file:///etc/passwd",
            "requested_by": "<@everyone>", "requested_by_plex_id": 12.5, "requested_by_plex_name": 7,
        }))
        hint = shelf._read_hint(path)
    assert hint["series"] == "Saga"
    for key in ("title", "author", "series_index", "year", "isbn", "cover_url",
                "requested_by", "requested_by_plex_id", "requested_by_plex_name"):
        assert hint.get(key) is None, f"{key} kept: {hint.get(key)!r}"


def test_well_formed_hint_fields_are_kept():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".plexbie_hint_x.json"
        path.write_text(json.dumps({
            "nzb_title": RELEASE, "title": "The Book", "author": "Ann Author", "year": 2011,
            "isbn": "9780000000001", "cover_url": "https://covers.openlibrary.org/b/olid/OL1M-M.jpg",
            "requested_by": 123456789012345678, "requested_by_plex_id": "998877",
            "requested_by_plex_name": "ann",
        }))
        hint = shelf._read_hint(path)
    assert (hint["title"], hint["author"], hint["year"], hint["isbn"]) == ("The Book", "Ann Author", 2011, "9780000000001")
    assert hint["cover_url"] == "https://covers.openlibrary.org/b/olid/OL1M-M.jpg"
    assert hint["requested_by"] == 123456789012345678
    assert (hint["requested_by_plex_id"], hint["requested_by_plex_name"]) == ("998877", "ann")


def test_a_hint_that_is_not_an_object_is_not_used():
    with tempfile.TemporaryDirectory() as tmp:
        watch = Path(tmp)
        (watch / ".plexbie_hint_x.json").write_text(json.dumps([RELEASE]))
        assert shelf._find_hint_file(watch, RELEASE) is None
        assert shelf._read_hint(watch / ".plexbie_hint_x.json") is None


def test_series_index_is_escaped_in_metadata_opf():
    import xml.etree.ElementTree as ET

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "metadata.opf"
        shelf.generate_opf({"title": "T", "author": "A", "series": "S", "series_index": '1"/><x a="'}, path)
        tree = ET.parse(path)
    values = [m.get("content") for m in tree.iter("{http://www.idpf.org/2007/opf}meta")
              if m.get("name") == "calibre:series_index"]
    assert values == ['1"/><x a="']
