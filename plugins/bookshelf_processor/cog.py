# path: plugins/bookshelf_processor/cog.py
"""
Bookshelf Processor Plugin - Watches SABnzbd download directories for new ebooks
and audiobooks, parses usenet release names, fetches metadata and cover art, then
moves them into Audiobookshelf-compatible directory structures.

Ported from the standalone bookshelf-processor Docker container.
"""

import contextlib
import errno
import ipaddress
import json
import os
import re
import shutil
import socket
import hashlib
import zipfile
from dataclasses import dataclass, field
from html import escape as html_escape
from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
from yarl import URL
import discord
from discord.ext import commands, tasks

from core.blocking import run_blocking
from core.logging import get_logger
from core.admin_mirror import dm_user_id

logger = get_logger(__name__)

# ─── Constants ───────────────────────────────────────────────────────────────

JUNK_EXTENSIONS = {
    ".nfo", ".diz", ".txt", ".url", ".html", ".htm", ".cue", ".m3u",
    ".log", ".sfv", ".nzb", ".par2", ".jpg_original", ".png_original",
}

# Release clutter that is never part of a book: unextracted archives and their
# parts, scene check files, programs and shortcuts, Windows folder files. Like
# junk it goes with the download rather than being filed beside the book.
CLUTTER_EXTENSIONS = {
    ".rar", ".zip", ".7z", ".srr", ".srs", ".exe", ".lnk", ".bat", ".cmd", ".scr",
}
CLUTTER_NAMES = {"thumbs.db", "desktop.ini"}
#: Archive parts (.r00, .001) and files renamed by a par2 repair (file.mp3.1).
CLUTTER_SUFFIX_RE = re.compile(r"^\.(?:r\d{2}|\d+)$", re.IGNORECASE)
#: Folders that hold NAS or macOS metadata (.AppleDouble, @eaDir, __MACOSX).
METADATA_FOLDER_RE = re.compile(r"^(?:[.@]|__MACOSX$)")

# Words that mark an ebook file as an extra beside the book (a sample chapter,
# the errata) rather than another book in a pack.
EXTRA_WORDS = {
    "sample", "excerpt", "errata", "preview", "teaser", "appendix", "appendices",
    "bonus", "extra", "extras", "companion", "supplement", "booklet",
}

# What Audiobookshelf reads, plus formats it stores but cannot open (.ape, and
# most of the ebook formats after .cbr): filed with the book either way.
EBOOK_EXTENSIONS = {
    ".epub", ".azw3", ".mobi", ".pdf", ".cbz", ".cbr",
    ".azw", ".kfx", ".djvu", ".fb2", ".lit", ".rtf",
}
AUDIOBOOK_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".ogg", ".flac", ".wma", ".aac",
    ".opus", ".oga", ".wav", ".aiff", ".ape", ".mka", ".mp4",
    ".webm", ".webma", ".awb", ".caf", ".mpeg", ".mpg",
}

NOISE_TOKENS = {
    "retail", "epub", "ebook", "mobi", "azw3", "pdf", "audiobook",
    "mp3", "m4b", "m4a", "unabridged", "abridged", "unabr", "abr",
    "a novel", "a memoir", "novel", "fiction", "nonfiction", "non fiction",
}

# Audio bitrate patterns like "64k", "128k", "320kbps", "1064k"
BITRATE_RE = re.compile(r"\b\d{2,4}k(?:bps)?\b", re.IGNORECASE)

# Track numbering in embedded titles like "Title 01-10", "Title 1/10", "Title 01 of 10"
TRACK_NUM_RE = re.compile(r"\s*\d{1,3}\s*[-/]\s*\d{1,3}\s*$")
TRACK_OF_RE = re.compile(r"\s*\d{1,3}\s+of\s+\d{1,3}\s*$", re.IGNORECASE)

# Filesize patterns like "387.97 MB", "1.2 GB", "500 KB"
FILESIZE_RE = re.compile(r"\s*-?\s*\d+\.?\d*\s*(?:MB|GB|KB|bytes)\b", re.IGNORECASE)

RELEASE_GROUP_RE = re.compile(r"[_\-][A-Za-z][A-Za-z0-9_]{1,20}$")
YEAR_BRACKET_RE = re.compile(r"\[?\(?((?:19|20)\d{2})\)?\]?")
BOOK_NUM_RE = re.compile(
    r"[,\s]*\b(?:Book|Vol(?:ume)?\.?|Part|Bk\.?|#)\s*(\d+)\b", re.IGNORECASE
)
SERIES_IN_BRACKETS_RE = re.compile(r"\[([^\]]+)\s+(\d+)\]")

# Pattern for "Author-Series.Index-Title" format common in usenet audiobooks
# e.g., "Pierce.Brown-Red.Rising.01-Red.Rising"
DASH_DELIMITED_RE = re.compile(
    r"^([^-]+)-([^-]+?)\.?(\d{1,2})-(.+)$"
)

# Common title words that should NOT be treated as author names
TITLE_WORDS = {
    "the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "is",
    "by", "with", "from", "or", "not", "but", "all", "no", "my", "his",
    "her", "our", "its", "new", "old", "big", "how", "why", "what",
}

API_TIMEOUT = 10

#: Largest cover image accepted. Real covers run to a few MB (the largest seen
#: was 6.8 MB); anything far beyond that is not a cover.
COVER_MAX_BYTES = 15 * 1024 * 1024

#: Redirects followed for one cover. Every hop is checked like the first URL.
COVER_MAX_REDIRECTS = 5


# ─── Name Parsing ────────────────────────────────────────────────────────────


def parse_release_name(name: str) -> dict:
    """
    Parse a usenet release name into author, title, year, series, series_index.

    Handles formats like:
      - "Andy.Weir.Project.Hail.Mary.A.Novel.2021.Retail.EPUB.eBook-BitBook"
      - "Stephen King - The Shining (Unabridged)"
      - "Martha Wells-[The Murderbot Diaries 01]-All Systems Red"
      - "Project.Hail.Mary.by.Andy.Weir"
    """
    original = name
    result = {
        "author": "",
        "title": "",
        "year": None,
        "series": None,
        "series_index": None,
    }

    # ── Try dash-delimited format FIRST (before any substitution) ──
    # e.g., "Pierce.Brown-Red.Rising.01-Red.Rising.Unabr-64k.[2014]"
    dash_delim = DASH_DELIMITED_RE.match(name)
    if dash_delim:
        raw_author = dash_delim.group(1).replace(".", " ").replace("_", " ").strip()
        raw_series = dash_delim.group(2).replace(".", " ").replace("_", " ").strip()
        series_idx = int(dash_delim.group(3))
        raw_title = dash_delim.group(4).replace(".", " ").replace("_", " ").strip()

        # Extract year from title portion
        year_match = YEAR_BRACKET_RE.search(raw_title)
        if year_match:
            result["year"] = year_match.group(1)
            raw_title = raw_title[: year_match.start()] + raw_title[year_match.end() :]

        # Clean noise from title
        raw_title = BITRATE_RE.sub("", raw_title)
        for token in NOISE_TOKENS:
            raw_title = re.sub(r"\b" + re.escape(token) + r"\b", "", raw_title, flags=re.IGNORECASE)
        raw_title = re.sub(r"[\[\](){}]", "", raw_title)
        raw_title = re.sub(r"\s+", " ", raw_title).strip()

        result["author"] = _clean_name(raw_author)
        result["series"] = _clean_name(raw_series)
        result["series_index"] = series_idx
        # Use the series name as title if cleaned title is same or empty
        cleaned_title = _clean_name(raw_title)
        if cleaned_title and cleaned_title.lower() != raw_series.lower():
            result["title"] = cleaned_title
        else:
            result["title"] = _clean_name(raw_series)

        if not result["author"]:
            result["author"] = "Unknown"

        logger.debug(f"Parsed (dash-delimited) '{original}' -> {result}")
        return result

    # ── Standard parsing path ──

    # Strip release group tag at end
    name = RELEASE_GROUP_RE.sub("", name)

    # Remove content in parentheses that's noise (Unabridged, mp3, etc.)
    name = re.sub(
        r"\((?:unabridged|abridged|unabr|mp3|m4b|epub|retail|audiobook)[^)]*\)",
        "",
        name,
        flags=re.IGNORECASE,
    )

    # Check for series info in brackets like [Series Name 01]
    bracket_match = SERIES_IN_BRACKETS_RE.search(name)
    if bracket_match:
        result["series"] = bracket_match.group(1).strip()
        result["series_index"] = int(bracket_match.group(2))
        name = SERIES_IN_BRACKETS_RE.sub("", name)

    # Remove square bracket content that looks like format info or year
    name = re.sub(r"\[(?:epub|mobi|mp3|m4b|pdf|audiobook)[^\]]*\]", "", name, flags=re.IGNORECASE)

    # Extract year (handles [2014], (2014), and bare 2014)
    year_match = YEAR_BRACKET_RE.search(name)
    if year_match:
        result["year"] = year_match.group(1)
        name = name[: year_match.start()] + name[year_match.end() :]

    # Remove remaining square/round brackets
    name = re.sub(r"[\[\]()]", " ", name)

    # Replace dots and underscores with spaces
    name = name.replace(".", " ").replace("_", " ")

    # Remove bitrate patterns (64k, 128kbps, 1064k, etc.)
    name = BITRATE_RE.sub("", name)

    # Remove filesize patterns (387.97 MB, 1.2 GB, etc.)
    name = FILESIZE_RE.sub("", name)

    # Remove noise tokens
    for token in NOISE_TOKENS:
        name = re.sub(r"\b" + re.escape(token) + r"\b", "", name, flags=re.IGNORECASE)

    # Clean up extra whitespace
    name = re.sub(r"\s+", " ", name).strip()

    # ── Try dash separator (most reliable) ──
    # "Author - Title" or "Author-Title" with spaces around dash
    dash_match = re.match(r"^(.+?)\s*[-\u2013\u2014]\s+(.+)$", name)
    if dash_match:
        result["author"] = _clean_name(dash_match.group(1))
        title_part = dash_match.group(2)
    else:
        # ── Try "by Author" pattern ──
        by_match = re.search(r"\bby\s+(.+)$", name, re.IGNORECASE)
        if by_match:
            result["author"] = _clean_name(by_match.group(1))
            title_part = name[: by_match.start()].strip()
        else:
            # ── Heuristic: first 2-3 words are author if they look like names ──
            words = name.split()
            author_words, title_words_list = _split_author_title(words)
            result["author"] = _clean_name(" ".join(author_words))
            title_part = " ".join(title_words_list)

    # Extract book number from title if not already found
    if result["series_index"] is None:
        book_match = BOOK_NUM_RE.search(title_part)
        if book_match:
            result["series_index"] = int(book_match.group(1))
            title_part = BOOK_NUM_RE.sub("", title_part)

    result["title"] = _clean_name(title_part)

    # If we still don't have an author, use "Unknown"
    if not result["author"]:
        result["author"] = "Unknown"
    if not result["title"]:
        result["title"] = original

    logger.debug(f"Parsed '{original}' -> {result}")
    return result


def _split_author_title(words: list[str]) -> tuple[list[str], list[str]]:
    """
    Heuristic split: first N capitalized words that look like a person name
    become the author, rest becomes the title.
    """
    if len(words) <= 2:
        return words[:1], words[1:] if len(words) > 1 else words

    # Try 2-word and 3-word author candidates
    for n in [2, 3]:
        if n > len(words):
            continue
        candidate = words[:n]
        rest = words[n:]

        # Check if candidate looks like a person name:
        # - All words capitalized (or single letter like "J" "K")
        # - No common title words
        all_name_like = all(
            (w[0].isupper() or len(w) <= 2) and w.lower() not in TITLE_WORDS
            for w in candidate
        )
        # The remaining words should start with something that looks like a title
        has_rest = len(rest) > 0

        if all_name_like and has_rest:
            return candidate, rest

    # Fallback: first 2 words are author
    return words[:2], words[2:]


def _clean_name(name: str) -> str:
    """Clean up a parsed name string."""
    # Remove trailing/leading punctuation and whitespace
    name = re.sub(r"^[\s\-\u2013\u2014,.:;]+|[\s\-\u2013\u2014,.:;]+$", "", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    # Title case if all lower or all upper
    if name == name.lower() or name == name.upper():
        name = name.title()
    return name


# ─── Embedded Metadata Extraction ────────────────────────────────────────────


def extract_embedded_metadata(file_path: Path) -> dict | None:
    """
    Extract metadata embedded in the file itself.
    - M4B/M4A: MP4 tags (title, artist, album/series, year, cover, description)
    - EPUB: OPF metadata (title, creator, identifier/ISBN, date, series)
    - MP3: ID3 tags
    Returns a dict with keys matching our metadata format, or None.
    """
    suffix = file_path.suffix.lower()

    if suffix in {".m4b", ".m4a"}:
        return _extract_m4b_metadata(file_path)
    elif suffix == ".epub":
        return _extract_epub_metadata(file_path)
    elif suffix == ".mp3":
        return _extract_mp3_metadata(file_path)

    return None


def _extract_m4b_metadata(file_path: Path) -> dict | None:
    """Extract metadata from M4B/M4A (MP4 container) files."""
    try:
        from mutagen.mp4 import MP4

        tags = MP4(str(file_path))
        meta = {}

        # Title
        title = tags.get("\xa9nam")
        if title:
            meta["title"] = str(title[0])

        # Author/Artist
        artist = tags.get("\xa9ART")
        if artist:
            meta["author"] = str(artist[0])

        # Album (sometimes the series name for audiobooks, but often just the book title)
        album = tags.get("\xa9alb")
        if album:
            album_str = str(album[0])
            # Clean edition markers
            album_clean = re.sub(
                r"\s*\((?:Unabridged|Abridged|Unabr|Audio(?:book)?)\)\s*$",
                "", album_str, flags=re.IGNORECASE
            ).strip()
            # Only treat as series if album is meaningfully different from title
            # (not just the same name or the same name + edition info)
            title_str = meta.get("title", "").lower()
            if album_clean and title_str and album_clean.lower() != title_str:
                # Make sure it's not just a minor variation (e.g., "Title: Subtitle" vs "Title")
                if not (album_clean.lower().startswith(title_str) or title_str.startswith(album_clean.lower())):
                    meta["series"] = album_clean

        # Year
        year = tags.get("\xa9day")
        if year:
            year_str = str(year[0])[:4]
            if year_str.isdigit():
                meta["year"] = year_str

        # Track number - do NOT use as series index
        # Track numbers in audiobooks represent chapters/parts, not book number
        # Series index should only come from API lookups or explicit metadata

        # Genre
        genre = tags.get("\xa9gen")
        if genre:
            meta["genre"] = str(genre[0])

        # Description/Comment
        desc = tags.get("desc") or tags.get("\xa9cmt")
        if desc:
            meta["description"] = str(desc[0])[:500]

        # Embedded cover art
        covr = tags.get("covr")
        if covr and covr[0]:
            meta["embedded_cover"] = bytes(covr[0])

        if meta.get("title") or meta.get("author"):
            logger.info(f"Embedded M4B metadata: title={meta.get('title')}, "
                        f"author={meta.get('author')}, series={meta.get('series')}")
            return meta

    except Exception as e:
        logger.debug(f"Failed to read M4B metadata from {file_path.name}: {e}")

    return None


def _extract_epub_metadata(file_path: Path) -> dict | None:
    """Extract metadata from EPUB files (ZIP with OPF inside)."""
    try:
        with zipfile.ZipFile(str(file_path)) as z:
            # Find the OPF file
            opf_name = None
            for name in z.namelist():
                if name.endswith(".opf"):
                    opf_name = name
                    break

            if not opf_name:
                return None

            content = z.read(opf_name).decode("utf-8", errors="ignore")
            meta = {}

            # Title
            title_match = re.search(r"<dc:title[^>]*>([^<]+)</dc:title>", content)
            if title_match:
                meta["title"] = title_match.group(1).strip()

            # Author
            creator_match = re.search(r"<dc:creator[^>]*>([^<]+)</dc:creator>", content)
            if creator_match:
                meta["author"] = creator_match.group(1).strip()

            # ISBN - look in dc:identifier tags
            identifiers = re.findall(
                r"<dc:identifier[^>]*>([^<]+)</dc:identifier>", content
            )
            for ident in identifiers:
                ident = ident.strip()
                # Check for ISBN-13 or ISBN-10 pattern
                isbn_clean = re.sub(r"[^0-9X]", "", ident.upper())
                if len(isbn_clean) in {10, 13} and isbn_clean.isdigit():
                    meta["isbn"] = isbn_clean
                    break
                # Also check for "urn:isbn:..." format
                isbn_match = re.search(r"(?:isbn[:\s]*)(\d{10,13})", ident, re.I)
                if isbn_match:
                    meta["isbn"] = isbn_match.group(1)
                    break

            # Date/Year
            date_match = re.search(r"<dc:date[^>]*>([^<]+)</dc:date>", content)
            if date_match:
                year = date_match.group(1).strip()[:4]
                if year.isdigit():
                    meta["year"] = year

            # Publisher
            pub_match = re.search(r"<dc:publisher[^>]*>([^<]+)</dc:publisher>", content)
            if pub_match:
                meta["publisher"] = pub_match.group(1).strip()

            # Calibre series metadata
            series_match = re.search(
                r'name="calibre:series"\s*content="([^"]+)"', content
            )
            if series_match:
                meta["series"] = series_match.group(1).strip()

            series_idx_match = re.search(
                r'name="calibre:series_index"\s*content="([^"]+)"', content
            )
            if series_idx_match:
                try:
                    meta["series_index"] = int(float(series_idx_match.group(1)))
                except ValueError:
                    pass

            if meta.get("title") or meta.get("author") or meta.get("isbn"):
                logger.info(f"Embedded EPUB metadata: title={meta.get('title')}, "
                            f"author={meta.get('author')}, isbn={meta.get('isbn')}, "
                            f"series={meta.get('series')}")
                return meta

    except Exception as e:
        logger.debug(f"Failed to read EPUB metadata from {file_path.name}: {e}")

    return None


def _extract_mp3_metadata(file_path: Path) -> dict | None:
    """Extract metadata from MP3 files using mutagen."""
    try:
        from mutagen.id3 import ID3

        tags = ID3(str(file_path))
        meta = {}

        # Title
        if "TIT2" in tags:
            meta["title"] = str(tags["TIT2"])
        # Artist/Author
        if "TPE1" in tags:
            meta["author"] = str(tags["TPE1"])
        # Album (series)
        if "TALB" in tags:
            album = str(tags["TALB"])
            if meta.get("title") and album.lower() != meta["title"].lower():
                meta["series"] = album
        # Year
        if "TDRC" in tags:
            year = str(tags["TDRC"])[:4]
            if year.isdigit():
                meta["year"] = year
        # Track number
        if "TRCK" in tags:
            trk = str(tags["TRCK"]).split("/")[0]
            if trk.isdigit() and meta.get("series"):
                meta["series_index"] = int(trk)

        # Embedded cover
        if "APIC:" in tags:
            meta["embedded_cover"] = tags["APIC:"].data

        if meta.get("title") or meta.get("author"):
            logger.info(f"Embedded MP3 metadata: title={meta.get('title')}, "
                        f"author={meta.get('author')}")
            return meta

    except Exception as e:
        logger.debug(f"Failed to read MP3 metadata from {file_path.name}: {e}")

    return None


def _clean_embedded_title(title: str) -> str:
    """Clean track numbering and noise from embedded metadata titles.
    E.g., 'Echopraxia 01-10' -> 'Echopraxia', 'Title 3 of 12' -> 'Title'
    """
    title = TRACK_NUM_RE.sub("", title)
    title = TRACK_OF_RE.sub("", title)
    # Also strip leading track numbers like "01 - Title"
    title = re.sub(r"^\d{1,3}\s*[-_.]\s*", "", title)
    return title.strip()


def extract_metadata_from_files(files: list[Path]) -> dict | None:
    """
    Extract embedded metadata from book/audio files.
    For multi-file audiobooks (many MP3s), checks multiple files and picks
    the best metadata (most complete). Cleans track numbering from titles.
    """
    best_meta = None
    best_score = 0

    for idx, f in enumerate(files):
        meta = extract_embedded_metadata(f)
        if not meta:
            continue

        # Clean track numbering from title
        if meta.get("title"):
            meta["title"] = _clean_embedded_title(meta["title"])

        # Score: how many useful fields does this have?
        score = sum(1 for k in ["title", "author", "series", "year", "isbn"]
                    if meta.get(k))
        # Bonus for having an ISBN (most reliable identifier)
        if meta.get("isbn"):
            score += 3
        # Bonus for embedded cover
        if meta.get("embedded_cover"):
            score += 1

        if score > best_score:
            best_meta = meta
            best_score = score

        # If we have title + author + ISBN, that's as good as it gets
        if meta.get("title") and meta.get("author") and meta.get("isbn"):
            break

        # For multi-file audio, only check first 3 files
        if idx >= 2:
            break

    return best_meta


# ─── Metadata Fetching (async) ───────────────────────────────────────────────


async def fetch_metadata(author: str, title: str, isbn: str = None) -> dict:
    """Fetch metadata from Open Library, falling back to Google Books.
    If an ISBN is provided, try an exact ISBN lookup first for best results."""

    # If we have an ISBN, try exact lookup first (most reliable)
    if isbn:
        meta = await _try_open_library_isbn(isbn)
        if meta and meta.get("title"):
            logger.info(f"ISBN lookup succeeded: {isbn} -> {meta.get('title')}")
            return meta
        meta = await _try_google_books_isbn(isbn)
        if meta and meta.get("title"):
            logger.info(f"ISBN Google lookup succeeded: {isbn} -> {meta.get('title')}")
            return meta

    meta = await _try_open_library(author, title)
    if not meta or not meta.get("title"):
        meta_gb = await _try_google_books(author, title)
        if meta_gb and meta_gb.get("title"):
            # Merge: prefer Google Books if Open Library had nothing
            if not meta:
                meta = meta_gb
            else:
                for k, v in meta_gb.items():
                    if v and not meta.get(k):
                        meta[k] = v

    if not meta:
        logger.warning(f"No metadata found for '{author}' - '{title}'")
        meta = {}

    return meta


async def _get_json(url: str, params: dict | None = None):
    """GET a book-metadata API with the usual timeout. Raises on a bad status."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as session:
        async with session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()


def _series_from_subjects(subjects) -> tuple | None:
    """(series, number) from Open Library subjects like "Discworld #3" or "Expanse Book 2"."""
    for subj in subjects or []:
        match = re.search(r"(.+?)(?:\s*#\s*|\s+Book\s+)(\d+)", subj)
        if match:
            return match.group(1).strip(), int(match.group(2))
    return None


def _google_cover(vol: dict) -> str | None:
    """The largest cover Google Books has for a volume, over https and at full zoom."""
    links = vol.get("imageLinks", {})
    for key in ["extraLarge", "large", "medium", "small", "thumbnail"]:
        if key in links:
            return re.sub(r"zoom=\d", "zoom=1", links[key].replace("http:", "https:"))
    return None


async def _try_open_library_isbn(isbn: str) -> dict | None:
    """Look up a book by ISBN on Open Library (exact match)."""
    try:
        book = await _get_json(f"https://openlibrary.org/isbn/{isbn}.json")
    except Exception as e:
        logger.debug(f"Open Library ISBN lookup failed: {e}")
        return None

    meta = {
        "title": book.get("title", ""),
        "year": str(book.get("publish_date", ""))[-4:] if book.get("publish_date") else None,
        "cover_url": f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg",
        "isbn": isbn,
    }
    # The author and the series live on their own records.
    author_key = ((book.get("authors") or [{}])[0]).get("key", "")
    if author_key:
        try:
            meta["author"] = (await _get_json(f"https://openlibrary.org{author_key}.json")).get("name", "")
        except Exception:
            pass
    work_key = ((book.get("works") or [{}])[0]).get("key", "")
    if work_key:
        try:
            series = _series_from_subjects((await _get_json(f"https://openlibrary.org{work_key}.json")).get("subjects"))
            if series:
                meta["series"], meta["series_index"] = series
        except Exception:
            pass

    if meta.get("year") and not meta["year"].isdigit():
        meta["year"] = None
    return meta if meta.get("title") else None


async def _try_google_books_isbn(isbn: str) -> dict | None:
    """Look up a book by ISBN on Google Books."""
    try:
        data = await _get_json("https://www.googleapis.com/books/v1/volumes", {"q": f"isbn:{isbn}", "maxResults": "1"})
    except Exception as e:
        logger.debug(f"Google Books ISBN lookup failed: {e}")
        return None
    items = data.get("items", [])
    if not items:
        return None
    vol = items[0].get("volumeInfo", {})
    meta = {
        "title": vol.get("title", ""),
        "author": (vol.get("authors") or [""])[0],
        "year": vol.get("publishedDate", "")[:4] or None,
        "cover_url": _google_cover(vol),
        "isbn": isbn,
    }
    return meta if meta.get("title") else None


async def _try_open_library(author: str, title: str) -> dict | None:
    """Search Open Library for book metadata and cover."""
    params = {"title": title, "limit": "3"}
    if author and author != "Unknown":
        params["author"] = author
    try:
        data = await _get_json("https://openlibrary.org/search.json", params)
    except Exception as e:
        logger.debug(f"Open Library lookup failed: {e}")
        return None
    if not data.get("docs"):
        return None

    doc = _best_match(data["docs"], author, title) or data["docs"][0]
    cover_id = doc.get("cover_i")
    series = _series_from_subjects(doc.get("subject"))
    meta = {
        "title": doc.get("title", title),
        "author": (doc.get("author_name") or [author])[0],
        "year": str(doc["first_publish_year"]) if doc.get("first_publish_year") else None,
        "cover_url": f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None,
        "series": series[0] if series else None,
        "series_index": series[1] if series else None,
    }
    logger.info(f"Open Library match: {meta['author']} - {meta['title']}")
    return meta


async def _try_google_books(author: str, title: str) -> dict | None:
    """Search Google Books API for metadata and cover."""
    query = f"intitle:{title}"
    if author and author != "Unknown":
        query += f"+inauthor:{author}"
    try:
        data = await _get_json("https://www.googleapis.com/books/v1/volumes", {"q": query, "maxResults": "3"})
    except Exception as e:
        logger.debug(f"Google Books lookup failed: {e}")
        return None
    items = data.get("items", [])
    if not items:
        return None

    vol = items[0].get("volumeInfo", {})
    meta = {
        "title": vol.get("title", title),
        "author": (vol.get("authors") or [author])[0],
        "year": vol.get("publishedDate", "")[:4] or None,
        "cover_url": _google_cover(vol),
        "series": None,
        "series_index": None,
    }
    series_info = vol.get("seriesInfo", {})
    if series_info:
        meta["series"] = series_info.get("shortSeriesBookTitle", "")
        pos = series_info.get("bookDisplayNumber", "")
        if pos and pos.isdigit():
            meta["series_index"] = int(pos)

    logger.info(f"Google Books match: {meta['author']} - {meta['title']}")
    return meta


def _best_match(docs: list, author: str, title: str) -> dict | None:
    """Pick the best matching document from search results."""
    author_lower = author.lower()
    title_lower = title.lower()

    for doc in docs:
        doc_title = doc.get("title", "").lower()
        doc_authors = [a.lower() for a in doc.get("author_name", [])]

        title_match = title_lower in doc_title or doc_title in title_lower
        author_match = any(author_lower in a or a in author_lower for a in doc_authors)

        if title_match and author_match:
            return doc

    # Fallback: just title match
    for doc in docs:
        doc_title = doc.get("title", "").lower()
        if title_lower in doc_title or doc_title in title_lower:
            return doc

    return None


def _write_file_atomically(path: Path, content: bytes) -> None:
    """Blocking: write through a temporary name in the same folder, so a full
    disk or a restart never leaves a half-written file under the real name."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_bytes(content)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_cover(content: bytes, dest: Path, cache_path: Path | None) -> None:
    """Save to cache and destination. Blocking, so call via run_blocking.

    dest is in the library on the array through shfs, not the cache SSD.
    Measured 2026-09-30: a 6.8 MB cover took 137 ms median and 388 ms max to
    write, all of it with the event loop stopped when done inline.
    """
    if cache_path:
        _write_file_atomically(cache_path, content)
    _write_file_atomically(dest, content)


def _cover_from_cache(cache_path: Path, dest: Path) -> bool:
    """Blocking: copy a cached cover to dest. False when there is none, or the
    cached file is not an image (a cache written before covers were checked)."""
    try:
        content = cache_path.read_bytes()
    except FileNotFoundError:
        return False
    if not _looks_like_image(content):
        return False
    _write_file_atomically(dest, content)
    return True


def _looks_like_image(content: bytes) -> bool:
    """JPEG, PNG or WebP, judged by the first bytes rather than by what the server says."""
    return (content.startswith(b"\xff\xd8\xff")
            or content.startswith(b"\x89PNG\r\n\x1a\n")
            or (content[:4] == b"RIFF" and content[8:12] == b"WEBP"))


_SITE_LOCAL_V6 = ipaddress.ip_network("fec0::/10")
_IPV4_IN_V6 = (ipaddress.ip_network("::/96"), ipaddress.ip_network("64:ff9b::/96"))


def _is_public_address(host: str) -> bool:
    """True for an IP address on the internet; False for the LAN, this host,
    link-local, shared (CGNAT/Tailscale), reserved and multicast ranges, or a name."""
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        if ip in _SITE_LOCAL_V6:
            return False
        # Judge an IPv6 address that carries an IPv4 one (mapped, compatible,
        # NAT64, 6to4) by the IPv4 address it reaches.
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour:
            ip = ip.sixtofour
        elif any(ip in net for net in _IPV4_IN_V6):
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global and not ip.is_multicast


def _cover_url_allowed(url: str) -> bool:
    """http(s) only, and never an address that is not public. A host given by
    name is checked when it is resolved (see _PublicResolver)."""
    try:
        # The host as aiohttp will connect to it: yarl normalises it first, so
        # an address written in fullwidth digits is checked as plain 127.0.0.1.
        parts = URL(url)
        host = parts.raw_host
    except (ValueError, TypeError):
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return True  # a name
    return _is_public_address(host)


class _PublicResolver(AbstractResolver):
    """Resolves cover hosts to public addresses only.

    The cover URL comes from book lookups and the request hint. Checking the
    name here, where the connection is made, also covers a redirect to a name
    on the LAN and a name that changes address between a check and the fetch.
    """

    def __init__(self):
        self._resolver = DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        found = await self._resolver.resolve(host, port, family)
        public = [entry for entry in found if _is_public_address(entry["host"])]
        if not public:
            raise OSError(errno.EACCES, f"{host} has no public address")
        return public

    async def close(self):
        await self._resolver.close()


async def _fetch_cover(session, url: str) -> bytes | None:
    """The body of a cover image at url, or None (logged) when it is refused.

    Redirects are followed here rather than by aiohttp, so every hop is held to
    the same rules as the first URL.
    """
    for _ in range(COVER_MAX_REDIRECTS + 1):
        async with session.get(url, allow_redirects=False) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                target = urljoin(url, location) if location else ""
                if not _cover_url_allowed(target):
                    logger.warning(f"Cover redirect refused: {url} -> {location}")
                    return None
                url = target
                continue
            resp.raise_for_status()

            kind = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if kind and not kind.startswith("image/") and kind != "application/octet-stream":
                logger.warning(f"Cover is not an image ({kind}), skipping: {url}")
                return None
            if resp.content_length and resp.content_length > COVER_MAX_BYTES:
                logger.warning(f"Cover too large ({resp.content_length} bytes), skipping: {url}")
                return None
            content = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                content += chunk
                if len(content) > COVER_MAX_BYTES:
                    logger.warning(f"Cover larger than {COVER_MAX_BYTES} bytes, skipping: {url}")
                    return None
            return bytes(content)
    logger.warning(f"Cover redirected more than {COVER_MAX_REDIRECTS} times, skipping: {url}")
    return None


async def download_cover(url: str, dest: Path, cache_dir: Path | None = None) -> bool:
    """Download cover art, using cache to avoid redundant fetches.

    Only public http(s) hosts are fetched, and only a JPEG, PNG or WebP image
    up to COVER_MAX_BYTES is written (or cached).
    """
    if dest.exists():
        logger.debug(f"Cover already exists: {dest}")
        return True

    if not _cover_url_allowed(url):
        logger.warning(f"Cover URL refused (only public http(s) hosts are fetched): {url}")
        return False

    try:
        # Check cache
        if cache_dir:
            cache_key = hashlib.md5(url.encode()).hexdigest()
            cache_path = cache_dir / f"{cache_key}.jpg"

            # dest is on the array via shfs: off the loop (see _write_cover).
            if await run_blocking(_cover_from_cache, cache_path, dest):
                logger.debug(f"Cover from cache: {dest}")
                return True
        else:
            cache_path = None

        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)
        connector = aiohttp.TCPConnector(resolver=_PublicResolver())
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            content = await _fetch_cover(session, url)
        if content is None:
            return False

        # Check that we got an actual image (not a placeholder)
        if len(content) < 1000:
            logger.warning(f"Cover too small ({len(content)} bytes), skipping: {url}")
            return False
        if not _looks_like_image(content):
            logger.warning(f"Cover is not a JPEG, PNG or WebP image, skipping: {url}")
            return False

        await run_blocking(_write_cover, content, dest, cache_path)
        logger.info(f"Cover downloaded: {dest.name} ({len(content)} bytes)")
        return True

    except Exception as e:
        logger.warning(f"Cover download failed: {e}")
        return False


def generate_opf(meta: dict, output_path: Path):
    """Generate a metadata.opf file in Dublin Core format for Audiobookshelf."""
    title = html_escape(meta.get("title", "Unknown"))
    author = html_escape(meta.get("author", "Unknown"))

    series_meta = ""
    if meta.get("series"):
        series_meta += f'    <meta name="calibre:series" content="{html_escape(meta["series"])}"/>\n'
    if meta.get("series_index") is not None:
        series_meta += f'    <meta name="calibre:series_index" content="{html_escape(str(meta["series_index"]))}"/>\n'

    year_meta = ""
    if meta.get("year"):
        year_meta = f"    <dc:date>{html_escape(str(meta['year']))}</dc:date>\n"

    opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>{title}</dc:title>
    <dc:creator opf:role="aut">{author}</dc:creator>
{year_meta}{series_meta}  </metadata>
</package>
"""
    output_path.write_text(opf, encoding="utf-8")
    logger.debug(f"Wrote metadata: {output_path}")


# ─── File Organization ───────────────────────────────────────────────────────


def sanitize_dirname(name: str) -> str:
    """Remove filesystem-unsafe characters from a directory name."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name)
    name = name.strip(". ")
    return name or "Unknown"


def find_book_files(source: Path, media_type: str) -> list[Path]:
    """Recursively find all valid book/audio files."""
    extensions = AUDIOBOOK_EXTENSIONS if media_type == "audiobook" else EBOOK_EXTENSIONS
    files = []
    for f in source.rglob("*"):
        if f.is_file() and f.suffix.lower() in extensions:
            files.append(f)
    return sorted(files)


def _natural_key(value: str) -> list:
    """Sort key that puts CD2 before CD10."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def _repeated_names(book_files: list[Path]) -> set[str]:
    """File names (casefolded) that more than one book file shares.

    Casefolded because the library may be on a case-insensitive share (SMB),
    where Track.mp3 and track.MP3 are the same file.
    """
    seen, repeated = set(), set()
    for f in book_files:
        name = f.name.casefold()
        (repeated if name in seen else seen).add(name)
    return repeated


def _same_book(a: Path, b: Path) -> bool:
    """Whether two ebook files of one format look like copies of one book."""
    key_a, key_b = _normalise_for_match(a.stem), _normalise_for_match(b.stem)
    if key_a != key_b:
        return key_a in key_b or key_b in key_a
    # One name in two folders is two copies (retail/, converted/), unless the
    # folders differ only by a number, as in "Book 1/book.epub", "Book 2/book.epub".
    folder_a, folder_b = _normalise_for_match(a.parent.name), _normalise_for_match(b.parent.name)
    return folder_a == folder_b or re.sub(r"\d+", "", folder_a) != re.sub(r"\d+", "", folder_b)


def _separate_books(book_files: list[Path]) -> list[str]:
    """Names of the ebook files that look like different books; [] for one book.

    One book often comes in several formats (Book.epub, Book.mobi) or copies
    (retail/Book.epub, Book (retail).epub). Two files of the same format whose
    names, letters and digits only, neither contain the other are taken for two
    books, as in a pack of a series. Those are filed by hand, not as one item.
    Extras named as such (a sample chapter, the errata) are filed with the book
    and never count as another one.
    """
    by_format = {}
    for f in book_files:
        if EXTRA_WORDS.isdisjoint(re.findall(r"[a-z]+", f.stem.lower())):
            by_format.setdefault(f.suffix.lower(), []).append(f)
    for files in by_format.values():
        if any(not _same_book(a, b) for a in files for b in files):
            return sorted(f.name for f in files)
    return []


def plan_book_moves(source_path: Path, book_files: list[Path], media_type: str) -> list[tuple[Path, str]]:
    """(source file, name in the destination) for every book file, in order.

    The destination is one flat folder. Names are kept as they are unless two
    files share one, as multi-disc releases do (CD1/01.mp3, CD2/01.mp3). Then
    every file is renamed so none can replace another:

    - audiobooks: "Disc NN - <name>". NN is the position of the file's folder in
      natural order (the top level first), not a number read from the folder
      name: Audiobookshelf reads "Disc NN" from file names but only orders by
      it when the numbers have no gaps, so CD1 + CD3 must become 01 + 02.
    - ebooks: "<folder> - <name>", because the folder ("retail", "converted")
      is what tells the copies apart. Top-level files keep their names.

    A renamed file that would still share a name with an earlier one (a/b/x and
    "a - b"/x, or a top-level "retail - x" next to retail/x) gets " (2)", " (3)"
    before its extension, so every planned name is unique.
    """
    if book_files == [source_path]:
        return [(source_path, source_path.name)]
    if not _repeated_names(book_files):
        return [(f, f.name) for f in book_files]

    def folder(f: Path) -> str:
        return f.parent.relative_to(source_path).as_posix() if f.parent != source_path else ""

    if media_type != "audiobook":
        plan = [
            (f, f"{sanitize_dirname(folder(f).replace('/', ' - '))} - {f.name}" if folder(f) else f.name)
            for f in book_files
        ]
    else:
        folders = sorted({folder(f) for f in book_files}, key=lambda name: (name != "", _natural_key(name)))
        width = max(2, len(str(len(folders))))
        disc = {name: str(index).zfill(width) for index, name in enumerate(folders, start=1)}
        plan = [(f, f"Disc {disc[folder(f)]} - {f.name}") for f in book_files]

    taken, unique = set(), []
    for f, name in plan:
        candidate, counter = name, 2
        while candidate.casefold() in taken:
            candidate = f"{Path(name).stem} ({counter}){Path(name).suffix}"
            counter += 1
        taken.add(candidate.casefold())
        unique.append((f, candidate))
    return unique


def find_existing_covers(source: Path) -> list[Path]:
    """Find any cover images already in the source directory."""
    covers = []
    for f in source.rglob("*"):
        if f.is_file() and f.suffix.lower() in {".jpg", ".jpeg", ".png"}:
            # Skip tiny files (likely thumbnails or junk)
            if f.stat().st_size > 5000:
                covers.append(f)
    return covers


def build_destination(library: Path, meta: dict) -> Path:
    """
    Build Audiobookshelf-compatible path:
      Author/Title/          (no series)
      Author/Series/Title/   (with series)
    """
    author = sanitize_dirname(meta.get("author", "Unknown"))
    title = sanitize_dirname(meta.get("title", "Unknown"))

    if meta.get("series"):
        series = sanitize_dirname(meta["series"])
        return library / author / series / title
    else:
        return library / author / title


def _usable_folder(path: Path) -> bool:
    """Blocking: whether path is an existing folder, given as an absolute path.

    A blank setting reads as ".", the container's working directory, and "/" is
    the container itself: neither is a mapped volume, so both count as missing.
    """
    return path.is_absolute() and path != Path(path.anchor) and path.is_dir()


def _item_signature(path: Path) -> tuple:
    """A cheap fingerprint of an item: (file count, total bytes, newest mtime).

    Blocking (it stats the tree), so call it via run_blocking. Used two ways:
    to tell whether a download is still being written, and to tell whether a
    previously-failed item has changed enough to be worth retrying.
    """
    try:
        if not path.exists():
            # Distinct from an empty directory (0, 0, 0): a vanished item must not
            # compare equal to one that is merely empty, or the failed-item
            # bookkeeping would treat a disappear-and-reappear as "unchanged".
            return (-1, -1, -1)

        if path.is_file():
            stat = path.stat()
            return (1, stat.st_size, int(stat.st_mtime))

        count = 0
        total = 0
        newest = 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    stat = child.stat()
                    count += 1
                    total += stat.st_size
                    newest = max(newest, int(stat.st_mtime))
            except OSError:
                # Vanished mid-walk - the tree is changing, which is itself the
                # answer we care about.
                continue
        return (count, total, newest)
    except OSError as e:
        logger.debug(f"Could not fingerprint {path}: {e}")
        return (-1, -1, -1)


#: Written into a book's destination folder before its files are moved in, and
#: removed once the book is completely filed. While it is there the folder holds
#: an unfinished attempt: it names the download and every planned move, so the
#: next attempt can move those files back and start again in the same folder.
MOVE_MARKER = ".plexbie_incomplete.json"


@dataclass
class PlaceResult:
    """What _place_book did. On failure, `stranded` names files that could not be
    moved back and are still in the destination."""
    ok: bool
    failed_file: str | None = None
    error: str | None = None
    stranded: list[str] = field(default_factory=list)


#: Suffix of the temporary name a copy is written under. A file only gets its
#: real name once it is complete, so a copy cut short by a restart is never
#: mistaken for the file itself.
PART_SUFFIX = ".plexbie-part"


def _part_path(path: Path) -> Path:
    return path.with_name(path.name + PART_SUFFIX)


def _move_no_clobber(src: Path, target: Path) -> None:
    """Blocking: move src to target, refusing to replace anything already there.

    shutil.move silently replaces an existing target on POSIX, whether it renames
    or copies. Like shutil.move this renames when it can and copies when it
    cannot (another disk, or a share that refuses). The copy is written under a
    temporary name and renamed to target only once it is complete and on disk,
    and src is removed only after that. On any failure the copy is removed again
    and src is left as it was.
    """
    # Not atomic: something could create target between this check and the
    # rename. Python has no rename-without-replace (renameat2 RENAME_NOREPLACE),
    # and target is in a folder only this book is being filed into.
    if os.path.lexists(target):
        raise FileExistsError(errno.EEXIST, "already exists in the destination", str(target))
    try:
        os.rename(src, target)
        return
    except OSError:
        pass

    part = _part_path(target)
    try:
        # The temporary name is ours alone; a leftover from a copy cut short is
        # simply written over.
        with open(src, "rb") as fin, open(part, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
            fout.flush()
            os.fsync(fout.fileno())
        shutil.copystat(src, part)
        if os.path.lexists(target):
            raise FileExistsError(errno.EEXIST, "already exists in the destination", str(target))
        os.rename(part, target)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
    try:
        os.unlink(src)
    except BaseException:
        # Leave one copy, not two: the new one is not in any list of moves.
        with contextlib.suppress(OSError):
            target.unlink(missing_ok=True)
        raise


def _write_move_marker(dest: Path, source_path: Path, plan) -> None:
    """Blocking: record the planned moves in dest (see MOVE_MARKER)."""
    marker = dest / MOVE_MARKER
    tmp = dest / f"{MOVE_MARKER}.tmp"
    payload = {
        "source": str(source_path),
        "moves": [[str(src), name] for src, name in plan],
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(tmp, "w") as handle:
            json.dump(payload, handle)
        os.replace(tmp, marker)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_move_marker(dest: Path) -> dict | None:
    """Blocking: the MOVE_MARKER in dest, or None when there is none."""
    try:
        with open(dest / MOVE_MARKER) as handle:
            marker = json.load(handle)
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def _clear_move_marker(dest: Path) -> None:
    """Blocking: mark the book in dest as completely filed."""
    (dest / MOVE_MARKER).unlink(missing_ok=True)


def _choose_destination(base_dest: Path, source_path: Path) -> Path:
    """Blocking: base_dest, or "base (2)", "base (3)" ... when it holds another book.

    A folder is usable when it does not exist, is empty, or holds this
    download's own unfinished attempt, which is picked up rather than pushed
    aside into a numbered copy.
    """
    dest, counter = base_dest, 2
    while dest.exists() and any(dest.iterdir()):
        marker = _read_move_marker(dest)
        if marker and marker.get("source") == str(source_path):
            break
        dest = base_dest.parent / f"{base_dest.name} ({counter})"
        counter += 1
    return dest


def _restore_moves(dest: Path, moves) -> list[str]:
    """Blocking: move files back from dest to where they came from.

    `moves` is (original path, name in dest) pairs. Returns the names that could
    not be put back. Copies cut short are only ever under their temporary
    PART_SUFFIX name, and those are deleted. A file present under its real name
    in both places was copied whole but its original not yet removed; the copy
    in dest is dropped, unless the two differ in size, when neither is touched.
    """
    stranded = []
    for src, name in moves:
        target = dest / name
        try:
            for part in (_part_path(target), _part_path(Path(src))):
                if os.path.lexists(part):
                    part.unlink()
                    logger.warning(f"Removed an unfinished copy left by an earlier attempt: {part.name}")
            if not os.path.lexists(target):
                continue
            if os.path.lexists(src):
                if os.lstat(src).st_size != os.lstat(target).st_size:
                    raise FileExistsError(errno.EEXIST, "a different file is at the original path", src)
                target.unlink()
                logger.warning(f"Removed a second copy left by an earlier attempt: {name}")
                continue
            Path(src).parent.mkdir(parents=True, exist_ok=True)
            _move_no_clobber(target, Path(src))
        except Exception as e:
            stranded.append(name)
            logger.error(f"Could not move {name} back to {src}: {e}; it is still in {dest}")
    return stranded


def _recover_earlier_attempt(dest: Path, source_path: Path) -> list[str] | None:
    """Blocking: put back the files of this download's unfinished earlier attempt.

    Returns None when dest holds no such attempt, otherwise the names that are
    still stranded in dest (empty when everything went back). The marker stays,
    so dest remains this download's folder until the next placement replaces it.
    """
    marker = _read_move_marker(dest)
    if not marker or marker.get("source") != str(source_path):
        return None
    logger.warning(
        f"Found an unfinished earlier attempt for {source_path.name} in {dest}; moving its files back first"
    )
    return _restore_moves(dest, marker.get("moves", []))


def _log_stranded(source_path: Path, dest: Path, stranded: list[str]) -> None:
    logger.error(
        f"Not filed: {source_path.name}. {len(stranded)} file(s) from an earlier attempt could not "
        f"be moved back from {dest} ({', '.join(stranded)}); the next attempt tries again."
    )


def _filing_record_path(source_path: Path) -> Path:
    digest = hashlib.sha1(source_path.name.encode()).hexdigest()[:16]
    return source_path.parent / f".plexbie_filing_{digest}.json"


def _write_filing_record(source_path: Path, dest: Path, final: dict) -> None:
    """Blocking: note, next to the download, where and as what it is being filed.

    Without it a retry finds its earlier attempt only by working the metadata
    out again. With no hint that comes from the files still in the download and
    a live lookup, either of which can differ the second time, and the book
    would be split across two folders. Best effort: without the record a retry
    still finds the attempt whenever the metadata comes out the same.
    """
    record = _filing_record_path(source_path)
    tmp = record.with_name(record.name + ".tmp")
    try:
        with open(tmp, "w") as handle:
            json.dump({"source": str(source_path), "dest": str(dest), "final": final}, handle, default=str)
        os.replace(tmp, record)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        logger.warning(f"Could not note where {source_path.name} is being filed: {e}")


def _read_filing_record(source_path: Path) -> tuple[Path, dict] | None:
    """Blocking: (dest, metadata) of this download's unfinished earlier attempt.

    None unless the record exists and its folder still holds this download's
    MOVE_MARKER; a record outliving its marker is stale and ignored.
    """
    try:
        with open(_filing_record_path(source_path)) as handle:
            record = json.load(handle)
        if record.get("source") != str(source_path) or not isinstance(record.get("final"), dict):
            return None
        dest = Path(record["dest"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    marker = _read_move_marker(dest)
    if not marker or marker.get("source") != str(source_path):
        return None
    return dest, record["final"]


def _write_kept_record(source_path: Path, dest: Path) -> None:
    """Blocking: note that a filed download was kept, and as what it was left.

    It holds only files that could not be moved in beside the book. While it is
    unchanged, a restart must not take it for a new download and process it
    again. Best effort: without the note it is processed once more, finds no
    book in it and is set aside the same way.
    """
    record = _filing_record_path(source_path)
    try:
        with open(record, "w") as handle:
            json.dump({"source": str(source_path), "dest": str(dest),
                       "kept": list(_item_signature(source_path))}, handle)
    except OSError as e:
        logger.debug(f"Could not note that {source_path.name} was kept: {e}")


def _kept_signature(source_path: Path) -> tuple | None:
    """Blocking: the signature a kept download had when it was filed, if noted."""
    try:
        with open(_filing_record_path(source_path)) as handle:
            record = json.load(handle)
        if record.get("source") != str(source_path) or not isinstance(record.get("kept"), list):
            return None
        return tuple(record["kept"])
    except (OSError, ValueError, AttributeError):
        return None


def _clear_filing_record(source_path: Path) -> None:
    """Blocking: forget where the download was being filed."""
    _filing_record_path(source_path).unlink(missing_ok=True)


def _expire_orphan_filing_records(watch_dir: Path) -> int:
    """Blocking: delete filing records whose download is gone. Returns the count.

    A record is only left behind when a book did not finish filing; once the
    download itself is removed there is nothing left for it to recover.
    """
    removed = 0
    for candidate in watch_dir.glob(".plexbie_filing_*.json"):
        try:
            with open(candidate) as handle:
                source = json.load(handle).get("source")
        except (OSError, ValueError, AttributeError):
            source = None
        try:
            if source and os.path.lexists(source):
                continue
            candidate.unlink()
            removed += 1
            logger.info(f"Removed the filing record of a download that is gone: {candidate.name}")
        except OSError as e:
            logger.debug(f"Could not remove filing record {candidate.name}: {e}")
    return removed


def _remove_empty_folders(folders) -> None:
    """Blocking: rmdir each folder, deepest first, leaving any that are not empty."""
    for folder in folders:
        try:
            folder.rmdir()
        except OSError:
            pass


def _place_book(plan, dest: Path, source_path: Path, library: Path) -> PlaceResult:
    """Blocking: move every planned file into dest, or leave everything as it was.

    Stops at the first file that cannot be moved, moves back the ones already
    moved and removes the folders it created, so the download is intact for a
    retry. Files that cannot be moved back stay listed in the MOVE_MARKER for
    the next attempt. On success the marker stays until the caller has written
    the cover and metadata. Any marker already in dest has been recovered by
    then and is replaced.

    Folders are only ever created below the library folder, which must exist:
    a missing one is a volume that is not mapped, and creating it would file
    the book inside the container.
    """
    if not _usable_folder(library):
        return PlaceResult(False, error=f"the library folder {library} does not exist")
    resuming = os.path.lexists(dest / MOVE_MARKER)
    created = []
    folder = dest
    while folder != library and folder != folder.parent and not folder.exists():
        created.append(folder)
        folder = folder.parent

    try:
        for folder in reversed(created):
            folder.mkdir(exist_ok=True)
        _write_move_marker(dest, source_path, plan)
    except Exception as e:
        _remove_empty_folders(created)
        return PlaceResult(False, error=str(e))

    done = []
    for src, name in plan:
        try:
            _move_no_clobber(src, dest / name)
        except Exception as e:
            rel = src.relative_to(source_path).as_posix() if src != source_path else src.name
            if isinstance(e, FileExistsError):
                logger.error(f"Not moving {rel}: {name} already exists in {dest}")
                reason = "already exists in the destination"
            else:
                logger.error(f"Failed to move {rel} to {dest / name}: {e}")
                reason = str(e)
            break
        done.append((str(src), name))
        logger.debug(f"Moved: {name}")
    else:
        return PlaceResult(True)

    if done:
        logger.warning(f"Moving {len(done)} file(s) back to {source_path.name} after the failed move")
    stranded = _restore_moves(dest, done)
    # Retrying an attempt cut short after filing, the folder can still hold that
    # attempt's cover and metadata.opf. Then the marker stays: without it the
    # retry would take the folder for another book's and file into "Title (2)".
    leftovers = resuming and any(p.name != MOVE_MARKER for p in dest.iterdir())
    if not stranded and not leftovers:
        _clear_move_marker(dest)
        _remove_empty_folders(created)
    return PlaceResult(False, rel, reason, stranded)


def _adopt_existing_cover(existing_covers, dest: Path) -> bool:
    """Blocking: move the largest cover found in the source into `dest`."""
    try:
        best = max(existing_covers, key=lambda c: c.stat().st_size if c.exists() else 0)
    except ValueError:
        return False

    if not best.exists():
        return False
    try:
        shutil.move(str(best), str(dest / "cover.jpg"))
        logger.info(f"Used existing cover: {best.name}")
        return True
    except Exception as e:
        logger.debug(f"Could not use existing cover {best.name}: {e}")
        return False


def _file_leftovers(source_path: Path, dest: Path) -> list[str]:
    """Blocking: move what is left in a filed download in beside its book.

    Called once every book file is out, so anything still there other than
    junk came with the book: a companion PDF, a booklet, other artwork. Junk,
    release clutter (archives, Thumbs.db), hidden files, anything in a NAS or
    macOS metadata folder and a release's own .opf (the book's metadata.opf
    replaces it) are left to go with the folder. A name already taken in dest gets " (2)",
    " (3)" before its extension. Returns the files that could not be moved,
    relative to the download; while there are any, the download must be kept.
    """
    taken = {p.name.casefold() for p in dest.iterdir()}
    kept = []
    for f in sorted(source_path.rglob("*")):
        suffix = f.suffix.lower()
        if (not f.is_file() or f.name.startswith(".")
                or suffix in JUNK_EXTENSIONS or suffix in CLUTTER_EXTENSIONS or suffix == ".opf"
                or f.name.casefold() in CLUTTER_NAMES or CLUTTER_SUFFIX_RE.match(suffix)
                or any(METADATA_FOLDER_RE.match(part) for part in f.relative_to(source_path).parts[:-1])):
            continue
        name, counter = f.name, 2
        while name.casefold() in taken:
            name = f"{f.stem} ({counter}){f.suffix}"
            counter += 1
        try:
            _move_no_clobber(f, dest / name)
        except Exception as e:
            kept.append(f.relative_to(source_path).as_posix())
            logger.error(f"Could not move {f.name} in beside the book in {dest}: {e}")
            continue
        taken.add(name.casefold())
        logger.info(f"Filed {f.relative_to(source_path).as_posix()} beside the book as {name}")
    return kept


def _remove_source(source_path: Path) -> None:
    """Blocking: delete the source once everything has been moved out of it."""
    try:
        if source_path.is_dir():
            shutil.rmtree(source_path)
            logger.debug(f"Removed source directory: {source_path.name}")
        elif source_path.is_file():
            source_path.unlink()
            logger.debug(f"Removed source file: {source_path.name}")
    except Exception as e:
        logger.warning(f"Could not remove source {source_path.name}: {e}")


HINT_MAX_AGE_DAYS = 14

#: How often the scan loop sweeps expired hint files. The expiry itself is
#: HINT_MAX_AGE_DAYS, so sweeping on every 10-second tick meant ~17,000 directory
#: scans a day to enforce a fortnightly deadline.
HINT_SWEEP_INTERVAL_SECONDS = 3600


def _normalise_for_match(value: str) -> str:
    """Lowercase and strip everything but letters and digits.

    SABnzbd sanitises the NZB title when it creates the folder, so the two are
    rarely byte-identical - but they normalise to the same string.
    """
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _expire_stale_hints(watch_dir: Path) -> int:
    """Blocking: delete hint files older than HINT_MAX_AGE_DAYS. Returns the count.

    A hint is written when a download is submitted and removed when it is
    consumed, so one that outlives its download stays forever. Sweeping on a
    schedule (rather than only when an item is processed) means a watch directory
    that sits empty still gets tidied.
    """
    removed = 0
    cutoff = datetime.now().timestamp() - HINT_MAX_AGE_DAYS * 86400
    for candidate in watch_dir.glob(".plexbie_hint_*.json"):
        try:
            mtime = candidate.stat().st_mtime
            if mtime >= cutoff:
                continue
            age_days = (datetime.now().timestamp() - mtime) / 86400
            candidate.unlink()
            removed += 1
            logger.info(
                f"Removed stale hint file ({age_days:.0f} days old): {candidate.name}"
            )
        except OSError as e:
            logger.debug(f"Could not expire hint {candidate.name}: {e}")
    return removed


def _find_hint_file(watch_dir: Path, item_name: str):
    """Locate the hint file belonging to `item_name`, expiring stale ones.

    Blocking (globs and reads files), so call via run_blocking.

    The previous test was `nzb_title in item_name or item_name in nzb_title` - a
    substring match in both directions - and hints were never cleaned up unless
    successfully consumed. A download that never completed left its hint forever,
    so an unrelated later book whose name merely overlapped inherited that hint's
    author, title, series and cover. Because a hint short-circuits all metadata
    extraction, nothing downstream would notice the mismatch.

    Now: exact match on the normalised name, and anything older than
    HINT_MAX_AGE_DAYS is deleted rather than left to mismatch.
    """
    target = _normalise_for_match(item_name)
    if not target:
        return None

    now = datetime.now().timestamp()
    matches = []

    for candidate in watch_dir.glob(".plexbie_hint_*.json"):
        try:
            mtime = candidate.stat().st_mtime
            age_days = (now - mtime) / 86400
            if age_days > HINT_MAX_AGE_DAYS:
                candidate.unlink()
                logger.info(
                    f"Removed stale hint file ({age_days:.0f} days old): {candidate.name}"
                )
                continue

            with open(candidate) as handle:
                payload = json.load(handle)
        except Exception as e:
            logger.debug(f"Ignoring unreadable hint {candidate.name}: {e}")
            continue

        nzb_title = payload.get("nzb_title") if isinstance(payload, dict) else None
        if isinstance(nzb_title, str) and _normalise_for_match(nzb_title) == target:
            matches.append((mtime, candidate))

    if not matches:
        return None

    # Newest wins, so the choice is deterministic rather than glob-order dependent.
    matches.sort(key=lambda pair: pair[0], reverse=True)
    if len(matches) > 1:
        logger.warning(f"{len(matches)} hint files match {item_name}; using the newest")
    return matches[0][1]


#: Longest text taken from a hint field. Titles, names and cover URLs from a
#: book request are far shorter; anything longer did not come from one.
HINT_TEXT_MAX = 500
HINT_URL_MAX = 2048


def _hint_text(value, limit: int = HINT_TEXT_MAX) -> str | None:
    """value when it is a non-empty string of at most `limit` characters, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= limit else None


def _hint_number(value, digits: int):
    """value when it is a whole number of at most `digits` digits (an int, or a
    string of digits as some writers store ids), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value < 10 ** digits else None
    if isinstance(value, str) and re.fullmatch(rf"\d{{1,{digits}}}", value):
        return value
    return None


def _read_hint(path: Path) -> dict | None:
    """Blocking: the metadata in a hint file, with every field checked.

    None when the file cannot be read or is not a JSON object. A field of the
    wrong type or size is dropped (None) rather than trusted: the title and
    author name the library folder, the cover URL is fetched, and the requester
    fields decide who is told the book arrived.
    """
    try:
        with open(path) as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as e:
        logger.warning(f"Could not read hint file {path.name}: {e}")
        return None
    if not isinstance(payload, dict):
        logger.warning(f"Ignoring hint file {path.name}: not a JSON object")
        return None

    cover_url = _hint_text(payload.get("cover_url"), HINT_URL_MAX)
    if cover_url and not cover_url.lower().startswith(("http://", "https://")):
        cover_url = None
    isbn = _hint_text(payload.get("isbn"), 17)
    if isbn and not re.fullmatch(r"[0-9Xx-]{10,17}", isbn):
        isbn = None
    plex_id = payload.get("requested_by_plex_id")
    if not (isinstance(plex_id, str) and re.fullmatch(r"[\w-]{1,64}", plex_id)):
        plex_id = _hint_number(plex_id, 20)

    return {
        "title": _hint_text(payload.get("title")),
        "author": _hint_text(payload.get("author")),
        "year": _hint_number(payload.get("year"), 4),
        "series": _hint_text(payload.get("series")),
        "series_index": _hint_number(payload.get("series_index"), 4),
        "cover_url": cover_url,
        "isbn": isbn,
        # A Discord user id.
        "requested_by": _hint_number(payload.get("requested_by"), 20),
        "requested_by_plex_id": plex_id,
        "requested_by_plex_name": _hint_text(payload.get("requested_by_plex_name")),
    }


# ─── Main Processing Pipeline ────────────────────────────────────────────────


async def process_item(
    source_path: Path,
    media_type: str,
    audiobook_lib: Path,
    ebook_lib: Path,
    cache_dir: Path | None = None,
    bot=None,
):
    """
    Process a single download (folder or file) into Audiobookshelf format.
    media_type: 'audiobook' or 'ebook'

    Metadata priority (file contents first, folder name last):
      1. Hint file (.plexbie_hint_*.json beside the download, written by the
         book request) — if present, skip all parsing/extraction
      2. Embedded file metadata (M4B/EPUB/MP3 tags, ISBN)
      3. API lookup using embedded data (ISBN exact match, then title+author)
      4. Folder name parsing (only if files contain no metadata)
    """
    if source_path.name.startswith("."):
        return True  # hidden/control file - nothing to do, not a failure

    logger.info(f"{'=' * 60}")
    logger.info(f"Processing {media_type}: {source_path.name}")

    # A missing library folder is a volume that is not mapped. Filing anyway
    # would create it inside the container, where the book is lost when the
    # container is recreated, and the download would already be gone.
    library = audiobook_lib if media_type == "audiobook" else ebook_lib
    if not await run_blocking(_usable_folder, library):
        logger.error(f"Not filed: {source_path.name}. The {media_type} library folder '{library}' does not "
                     f"exist; check that it is mapped. The download and its hint are untouched.")
        return False

    # ── Check for hint files from Plexbie media_requests ──
    hint_used = False
    hint = None

    # Only .plexbie_hint_<name>.json in the watch directory, which the
    # media_requests cog writes. Anything inside the download folder came with
    # the release and is never taken as a hint.
    hint_file = await run_blocking(
        _find_hint_file, source_path.parent, source_path.name
    )

    if hint_file:
        hint = await run_blocking(_read_hint, hint_file)
        if hint is not None:
            logger.info(f"Using hint file metadata: {hint['title']} by {hint['author']}")

            # Use hint data directly — skip all parsing and extraction
            final = {
                "author": hint["author"] or "Unknown",
                "title": hint["title"] or source_path.name,
                "year": hint["year"],
                "series": hint["series"],
                "series_index": hint["series_index"],
                "cover_url": hint["cover_url"],
                "isbn": hint["isbn"],
            }
            hint_used = True
        else:
            logger.warning("Failed to read hint file, falling back to extraction")

    # An earlier attempt that did not finish (files that could not be moved back,
    # or a restart part-way through) noted where it was filing this download.
    # Put its files back first, so the plan below covers the whole book, and
    # file into that same folder under the same metadata: worked out again from
    # whatever is left in the download, it could differ and split the book.
    earlier = await run_blocking(_read_filing_record, source_path)
    if earlier:
        dest, final = earlier
        stranded = await run_blocking(_recover_earlier_attempt, dest, source_path)
        if stranded:
            _log_stranded(source_path, dest, stranded)
            return False

    # 1. Find book files first
    single_file = source_path.is_file()
    if single_file:
        book_files = [source_path]
        existing_covers = []
    else:
        # Directory walks - blocking, so off the loop. Nothing is deleted until
        # the book is filed: a download with no book in it (text files only,
        # say) is left exactly as it came.
        book_files = await run_blocking(find_book_files, source_path, media_type)
        existing_covers = await run_blocking(find_existing_covers, source_path)

    if not book_files:
        logger.warning(f"No valid {media_type} files found in {source_path.name}, skipping")
        return False

    if media_type == "ebook" and not earlier:
        separate = _separate_books(book_files)
        if separate:
            shown = ", ".join(separate[:5]) + (f" and {len(separate) - 5} more" if len(separate) > 5 else "")
            logger.warning(
                f"Not filed: {source_path.name} holds {len(separate)} different books ({shown}). "
                f"File them by hand, or move each one into the watch folder on its own to file "
                f"it separately. The download and its hint are untouched."
            )
            return False

    logger.info(f"Found {len(book_files)} {media_type} file(s)")

    embedded_cover_data = None

    if not hint_used and not earlier:
        # 2. READ THE FILES - extract embedded metadata (this is our primary source)
        # Parses tags with mutagen and unzips EPUBs - reads every file.
        embedded = await run_blocking(extract_metadata_from_files, book_files)
        isbn = None

        if embedded:
            embedded_cover_data = embedded.pop("embedded_cover", None)
            isbn = embedded.get("isbn")
            embedded.pop("genre", None)
            embedded.pop("description", None)
            logger.info(f"FILE METADATA -> Title: {embedded.get('title')}, "
                        f"Author: {embedded.get('author')}, "
                        f"Series: {embedded.get('series')}, "
                        f"ISBN: {isbn}, Year: {embedded.get('year')}")
        else:
            logger.info("No embedded metadata found in files")

        # 3. Build metadata - file contents take absolute priority
        final = {
            "author": None,
            "title": None,
            "year": None,
            "series": None,
            "series_index": None,
            "cover_url": None,
        }

        # Apply embedded metadata first
        if embedded:
            for key in ["author", "title", "year", "series", "series_index"]:
                if embedded.get(key):
                    final[key] = embedded[key]

        # 4. Use API to enrich (fill gaps, get cover art)
        # Use embedded title+author for the search, NOT the folder name
        search_author = final.get("author") or ""
        search_title = final.get("title") or ""

        # Only fall back to folder name parsing if files gave us NOTHING
        if not search_author and not search_title and not isbn:
            logger.info("No file metadata available, falling back to folder name parsing")
            parsed = parse_release_name(source_path.name)
            search_author = parsed.get("author", "")
            search_title = parsed.get("title", "")
            # Apply parsed values as fallback for any gaps
            for key in ["author", "title", "year", "series", "series_index"]:
                if not final.get(key) and parsed.get(key):
                    final[key] = parsed[key]
            logger.info(f"FOLDER PARSE -> Author: {search_author}, Title: {search_title}")

        # API lookup - use ISBN if available (exact match), otherwise title+author
        if isbn or (search_author and search_title):
            api_meta = await fetch_metadata(search_author, search_title, isbn=isbn)
        elif search_title:
            api_meta = await fetch_metadata("", search_title, isbn=isbn)
        else:
            api_meta = None

        # Merge API results - only fill gaps, never override file metadata
        if api_meta:
            for key in ["author", "title", "year", "series", "series_index", "cover_url"]:
                api_val = api_meta.get(key)
                if api_val and not final.get(key):
                    final[key] = api_val

        # Last resort defaults
        if not final.get("author"):
            final["author"] = "Unknown"
        if not final.get("title"):
            # Try to get something from the folder name
            parsed = parse_release_name(source_path.name)
            final["title"] = parsed.get("title") or source_path.name

    logger.info(f"FINAL -> Author: {final['author']}, Title: {final['title']}, "
                f"Series: {final.get('series')}, Index: {final.get('series_index')}")

    # 4. Build destination path. A folder holding another book gets a numbered
    # name; one holding this download's own unfinished attempt is reused.
    if not earlier:
        base_dest = build_destination(library, final)
        dest = await run_blocking(_choose_destination, base_dest, source_path)
        if dest != base_dest:
            logger.warning(f"Destination exists, using: {dest}")

        # The same unfinished attempt, found by its folder when the note beside
        # the download is missing. Planned from the leftovers alone, the files
        # put back would be deleted with the source, so look again first.
        stranded = await run_blocking(_recover_earlier_attempt, dest, source_path)
        if stranded:
            _log_stranded(source_path, dest, stranded)
            return False
        if stranded is not None and not single_file:
            book_files = await run_blocking(find_book_files, source_path, media_type)
            if not book_files:
                logger.warning(f"No valid {media_type} files found in {source_path.name}, skipping")
                return False

    plan = plan_book_moves(source_path, book_files, media_type)
    repeated = _repeated_names(book_files)
    if repeated:
        style = "'Disc NN - <name>'" if media_type == "audiobook" else "'<folder> - <name>'"
        logger.info(
            f"{len(repeated)} file name(s) repeat across sub-folders of {source_path.name}; "
            f"filing every file as {style} so none replaces another"
        )

    # 5. Move book files, off the event loop. An audiobook is routinely several GB
    # across many files, and a move falls back to a byte-for-byte copy
    # whenever the rename cannot be done in place (which on a pooled or FUSE-backed share
    # happens whenever source and destination land on different disks). Run inline
    # this held the loop - and the Discord heartbeat - for the whole copy.
    #
    # All or nothing: if any file cannot be moved, the ones already moved are put
    # back and nothing below runs. The hint is kept, nothing is announced, and
    # the scan loop records the failure against the download as it was left.
    await run_blocking(_write_filing_record, source_path, dest, final)
    result = await run_blocking(_place_book, plan, dest, source_path, library)
    if not result.ok:
        what = (f"{result.failed_file} could not be moved to {dest}" if result.failed_file
                else f"{dest} could not be prepared")
        if result.stranded:
            after = (f"{len(result.stranded)} file(s) could not be put back and are still in {dest} "
                     f"({', '.join(result.stranded)}); the next attempt moves them back before filing.")
        else:
            after = ("Everything already moved was put back; the download and its hint are untouched "
                     "and nothing was announced. It is tried again when the download changes or "
                     "Plexbie restarts.")
        logger.error(f"Not filed: {source_path.name}. {what}: {result.error}. {after}")
        return False

    # 6. Handle cover art
    cover_done = False

    # First, try embedded cover from the file itself (only if not using hint)
    if embedded_cover_data and not cover_done:
        try:
            await run_blocking((dest / "cover.jpg").write_bytes, embedded_cover_data)
            cover_done = True
            logger.info(f"Used embedded cover art ({len(embedded_cover_data)} bytes)")
        except Exception as e:
            logger.debug(f"Failed to write embedded cover: {e}")

    # Second, check for existing cover in source directory
    if not cover_done and existing_covers:
        cover_done = await run_blocking(_adopt_existing_cover, existing_covers, dest)

    # If no existing cover, download one (works for both hint and normal paths)
    if not cover_done and final.get("cover_url"):
        cover_done = await download_cover(final["cover_url"], dest / "cover.jpg", cache_dir=cache_dir)

    if not cover_done:
        logger.warning(f"No cover art available for {final['title']}")

    # 7. Generate metadata.opf. Every file is already in place, so like the
    # cover this is best effort: failing here would leave a whole book unfiled
    # and unannounced over a file Audiobookshelf can do without.
    try:
        await run_blocking(generate_opf, final, dest / "metadata.opf")
    except Exception as e:
        logger.warning(f"Could not write metadata.opf for {final['title']}: {e}")

    # The book is completely filed. A restart before this point leaves the
    # marker and the filing record; while the download is still in the watch
    # folder, the retry moves everything back and files it again into this same
    # folder. A single-file download has nothing left to retry from: its book
    # stays complete in dest with the marker until someone removes it.
    await run_blocking(_clear_move_marker, dest)
    await run_blocking(_clear_filing_record, source_path)

    # 8. Clean up source - every book file has been moved out of it. Anything
    # else that came with the book, junk aside, is filed beside it first; if any
    # of that cannot be moved the download is kept rather than deleted with it.
    # It holds no book files any more, so it is never filed a second time.
    kept = []
    if not single_file:
        kept = await run_blocking(_file_leftovers, source_path, dest)
    if kept:
        logger.warning(
            f"Kept {source_path.name}: {len(kept)} file(s) that came with the book could not be "
            f"moved to {dest} ({', '.join(kept)}). Move them by hand, then remove the download."
        )
        await run_blocking(_write_kept_record, source_path, dest)
    else:
        await run_blocking(_remove_source, source_path)

    # 9. Clean up hint file if used
    if hint_used and hint_file.exists():
        try:
            hint_file.unlink()
            logger.debug("Removed hint file after successful processing")
        except Exception:
            pass

    logger.info(f"Complete: {final['author']} / {final['title']} -> {dest}")

    # ── Send Discord notifications ──
    if bot:
        try:
            # 1. Send "New Book Added" embed to updates channel
            updates_channel_id = getattr(getattr(getattr(bot, "services", None), "config", None), "updates_channel_id", None)
            if updates_channel_id:
                channel = bot.get_channel(updates_channel_id)
                if channel:
                    from datetime import timezone as _tz
                    format_emoji = "📖" if media_type == "ebook" else "🎧"
                    format_label = "Ebook" if media_type == "ebook" else "Audiobook"

                    embed = discord.Embed(
                        title=f"{format_emoji} New {format_label} Added to Library!",
                        description=f"**{final['title']}** by {final['author']}",
                        color=discord.Color.blue(),
                        timestamp=datetime.now(_tz.utc),
                    )
                    embed.add_field(name="Author", value=final['author'], inline=True)
                    embed.add_field(name="Format", value=f"{format_emoji} {format_label}", inline=True)
                    if final.get('year'):
                        embed.add_field(name="Year", value=str(final['year']), inline=True)
                    if final.get('series'):
                        series_text = final['series']
                        if final.get('series_index'):
                            series_text += f" #{final['series_index']}"
                        embed.add_field(name="Series", value=series_text, inline=True)
                    embed.set_footer(text="Added to Audiobookshelf")

                    # Attach cover image if available
                    cover_path = dest / "cover.jpg"
                    if cover_path.exists():
                        file = discord.File(str(cover_path), filename="cover.jpg")
                        embed.set_thumbnail(url="attachment://cover.jpg")
                        await channel.send(embed=embed, file=file)
                    else:
                        await channel.send(embed=embed)

                    logger.info(f"Sent notification to updates channel for: {final['title']}")

            # 2. DM the requester if hint file had a user ID. (This used `services`
            # without defining it: the NameError was swallowed below, so requesters
            # were never told their book had arrived.)
            services = getattr(bot, "services", None)
            if hint_used and hint is not None and not hint.get("requested_by") and (
                    hint.get("requested_by_plex_id") or hint.get("requested_by_plex_name")):
                from core.notify import notify_member
                label = "ebook" if media_type == "ebook" else "audiobook"
                await notify_member(
                    services, title=f"Your {label} is ready: {final['title']}",
                    body=f"{final['title']} by {final['author']} is now on Audiobookshelf. "
                         f"Ready to {'read' if media_type == 'ebook' else 'listen'}.",
                    url="/app/schedule", plex_account_id=hint.get("requested_by_plex_id"),
                    plex_name=hint.get("requested_by_plex_name"), context=f"bookshelf item ready for {final['title']}")
            if hint_used and hint is not None:
                requester_id = hint.get("requested_by")
                if requester_id:
                    from datetime import timezone as _tz
                    format_emoji = "📖" if media_type == "ebook" else "🎧"
                    format_label = "ebook" if media_type == "ebook" else "audiobook"

                    dm_embed = discord.Embed(
                        title=f"📗 Your {format_label.title()} is Ready!",
                        description=f"**{final['title']}** by {final['author']} is now available in Audiobookshelf!",
                        color=discord.Color.green(),
                        timestamp=datetime.now(_tz.utc),
                    )
                    dm_embed.add_field(name="Format", value=f"{format_emoji} {format_label.title()}", inline=True)
                    if final.get('series'):
                        series_text = final['series']
                        if final.get('series_index'):
                            series_text += f" #{final['series_index']}"
                        dm_embed.add_field(name="Series", value=series_text, inline=True)
                    dm_embed.set_footer(text="Enjoy your reading! 📚")

                    if await dm_user_id(bot, services, requester_id, context=f"bookshelf item ready for {final['title']}", embed=dm_embed):
                        logger.info(f"Sent DM to user {requester_id} about {final['title']}")

        except Exception as e:
            logger.error(f"Error sending notifications: {e}", exc_info=True)

    return True

# ─── Discord Cog ─────────────────────────────────────────────────────────────


class BookshelfProcessorCog(commands.Cog):
    """Watches SABnzbd download directories and organizes ebooks/audiobooks
    for Audiobookshelf. Replaces the standalone bookshelf-processor container."""

    #: Watch and library folders already reported missing, so each is reported
    #: once rather than on every 10-second scan. Replaced, never mutated.
    _reported_missing: frozenset = frozenset()

    def __init__(self, bot: commands.Bot, services):
        self.bot = bot
        self.services = services

        cfg = services.config
        self.audiobook_watch = Path(cfg.bookshelf_audiobook_watch)
        self.ebook_watch = Path(cfg.bookshelf_ebook_watch)
        self.audiobook_lib = Path(cfg.bookshelf_audiobook_library)
        self.ebook_lib = Path(cfg.bookshelf_ebook_library)
        self.settle_seconds = cfg.bookshelf_settle_seconds

        # When hint files were last swept - see HINT_SWEEP_INTERVAL_SECONDS.
        self._last_hint_sweep = None

        # Cache directory for cover art
        self.cache_dir = Path(cfg.bookshelf_cache_dir)

        # Items waiting to settle: {path_str: (signature, datetime_last_changed)}
        # The signature is refreshed every scan; the timer restarts whenever it
        # changes, so an item is only processed once it has stopped being written.
        self.pending: dict[str, tuple] = {}

        # Items that could not be processed, keyed by the signature they had when
        # they failed: {path_str: signature}. Skipped while unchanged, so a
        # permanently broken item is attempted once rather than every scan
        # forever, but genuinely gains a retry the moment its contents change.
        self.failed: dict[str, tuple] = {}

    async def cog_load(self):
        """Called when the cog is loaded. Start the watcher loop."""
        logger.info("Bookshelf Processor loading")
        logger.info(f"Audiobook watch:   {self.audiobook_watch}")
        logger.info(f"Ebook watch:       {self.ebook_watch}")
        logger.info(f"Audiobook library: {self.audiobook_lib}")
        logger.info(f"Ebook library:     {self.ebook_lib}")
        logger.info(f"Settle time:       {self.settle_seconds}s")

        # A library only matters beside a watch folder that exists: a household
        # that does not use the bookshelf has neither.
        for watch, library, media_type in ((self.audiobook_watch, self.audiobook_lib, "audiobook"),
                                           (self.ebook_watch, self.ebook_lib, "ebook")):
            watching = await run_blocking(_usable_folder, watch)
            self._note_folder(watch, f"{media_type} watch", watching)
            if watching:
                self._note_folder(library, f"{media_type} library", await run_blocking(_usable_folder, library))

        # Ensure cache directory exists
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Note anything already sitting in the watch dirs, but do NOT process it
        # here - it goes through the same settle wait as anything new. Processing
        # inline meant a restart mid-download (including every deploy) handled a
        # partial download immediately, with no grace period at all.
        await self._seed_existing_items()

        # Start the scan loop
        self.scan_loop.start()

    async def cog_unload(self):
        """Called when the cog is unloaded. Stop the watcher loop."""
        self.scan_loop.cancel()
        logger.info("Bookshelf Processor stopped")

    def _note_folder(self, path: Path, what: str, present: bool) -> None:
        """Report a missing watch or library folder once, and its return."""
        key = (what, str(path))
        if present:
            if key in self._reported_missing:
                self._reported_missing = self._reported_missing - {key}
                logger.info(f"The {what} folder {path} is back")
            return
        if key in self._reported_missing:
            return
        self._reported_missing = self._reported_missing | {key}
        if not path.is_absolute() or path == Path(path.anchor):
            problem = f"is set to '{path}', which is not a mapped folder (it must be a full path below /)"
        else:
            problem = f"{path} does not exist"
        if what.endswith("library"):
            logger.error(f"The {what} folder {problem}; check that it is mapped. "
                         f"Finished downloads wait until it is there, and it is never created.")
        else:
            logger.info(f"The {what} folder {problem}; nothing is picked up from it")

    async def _seed_existing_items(self):
        """Record anything already in the watch dirs so the scan loop settles it.

        Deliberately does not process anything: an item present at startup is
        indistinguishable from one that arrived a second ago, and may well be a
        download still in flight.
        """
        seeded = 0
        now = datetime.now()
        for watch_dir in (self.audiobook_watch, self.ebook_watch):
            if not await run_blocking(_usable_folder, watch_dir):
                continue
            for path in sorted(watch_dir.iterdir()):
                if path.name.startswith("."):
                    continue
                signature = await run_blocking(_item_signature, path)
                if await run_blocking(_kept_signature, path) == signature:
                    # Filed already, and kept for files that could not be moved.
                    self.failed[str(path)] = signature
                    continue
                self.pending[str(path)] = (signature, now)
                seeded += 1

        if seeded:
            logger.info(
                f"Found {seeded} existing item(s) in watch directories; they will "
                f"be processed after the {self.settle_seconds}s settle period"
            )
        else:
            logger.info("No existing items in watch directories")

    @tasks.loop(seconds=10)
    async def scan_loop(self):
        """Scan watch directories every 10 seconds for items ready to process."""
        now = datetime.now()
        seen_paths = set()

        # Decided once per tick so both watch directories sweep together.
        sweep_hints = (
            self._last_hint_sweep is None
            or (now - self._last_hint_sweep).total_seconds() >= HINT_SWEEP_INTERVAL_SECONDS
        )
        if sweep_hints:
            self._last_hint_sweep = now

        for watch_dir, media_type in [
            (self.audiobook_watch, "audiobook"),
            (self.ebook_watch, "ebook"),
        ]:
            if not await run_blocking(_usable_folder, watch_dir):
                self._note_folder(watch_dir, f"{media_type} watch", False)
                continue
            self._note_folder(watch_dir, f"{media_type} watch", True)

            # Expire stale hints here rather than only when an item happens to be
            # processed in this directory - orphaned hints outlive the download
            # they were written for, and a watch dir can sit empty for months.
            # Throttled: the hints expire after HINT_MAX_AGE_DAYS, so there is no
            # reason to go looking for them on every 10-second tick.
            if sweep_hints:
                await run_blocking(_expire_stale_hints, watch_dir)
                await run_blocking(_expire_orphan_filing_records, watch_dir)

            for path in await run_blocking(lambda d=watch_dir: list(d.iterdir())):
                if path.name.startswith("."):
                    continue

                path_str = str(path)
                seen_paths.add(path_str)
                signature = await run_blocking(_item_signature, path)

                # Skip a known-bad item until its contents actually change.
                previous_failure = self.failed.get(path_str)
                if previous_failure is not None:
                    if previous_failure == signature:
                        continue
                    logger.info(
                        f"{path.name} changed since it last failed - retrying"
                    )
                    del self.failed[path_str]

                known = self.pending.get(path_str)
                if known is None:
                    self.pending[path_str] = (signature, now)
                    logger.info(f"New {media_type} detected: {path.name}")
                elif known[0] != signature:
                    # Still being written: restart the settle timer.
                    self.pending[path_str] = (signature, now)
                    logger.debug(f"{path.name} still changing, settle timer reset")

        # Forget anything that has disappeared from the watch dirs.
        for path_str in list(self.pending):
            if path_str not in seen_paths:
                del self.pending[path_str]
        for path_str in list(self.failed):
            if path_str not in seen_paths:
                del self.failed[path_str]

        # Collect items whose signature has been stable for the settle period.
        # While their library folder is missing they stay pending, and are filed
        # once it is back: a missing library is a volume that is not mapped.
        settled = []
        library_ready = {}
        for path_str, (signature, last_changed) in list(self.pending.items()):
            if (now - last_changed).total_seconds() < self.settle_seconds:
                continue
            path = Path(path_str)
            if str(path).startswith(str(self.audiobook_watch)):
                media_type = "audiobook"
            else:
                media_type = "ebook"
            if media_type not in library_ready:
                library = self.audiobook_lib if media_type == "audiobook" else self.ebook_lib
                library_ready[media_type] = await run_blocking(_usable_folder, library)
                self._note_folder(library, f"{media_type} library", library_ready[media_type])
            if not library_ready[media_type]:
                continue
            settled.append((path, media_type, last_changed))
            del self.pending[path_str]

        for path, media_type, last_changed in settled:
            library = self.audiobook_lib if media_type == "audiobook" else self.ebook_lib
            if not library_ready[media_type]:
                # Gone while an earlier item in this batch was being filed.
                self.pending[str(path)] = (await run_blocking(_item_signature, path), last_changed)
                continue
            try:
                processed = await process_item(
                    path, media_type,
                    self.audiobook_lib, self.ebook_lib,
                    cache_dir=self.cache_dir,
                    bot=self.bot,
                )
            except Exception as e:
                logger.error(f"Failed to process {media_type} {path.name}: {e}", exc_info=True)
                processed = False

            if not processed and path.exists() and not await run_blocking(_usable_folder, library):
                # The library went away while this batch was being filed: the
                # item waits for it like any other, rather than counting as failed.
                library_ready[media_type] = False
                self._note_folder(library, f"{media_type} library", False)
                self.pending[str(path)] = (await run_blocking(_item_signature, path), last_changed)
            elif processed and path.exists():
                # Filed, and kept for files that could not be moved in beside the
                # book (already logged): it holds no book, so leave it be.
                self.failed[str(path)] = await run_blocking(_item_signature, path)
            elif not processed and path.exists():
                # Record the failure against the item as processing left it, so
                # it is not reprocessed (and re-logged) on every scan forever.
                # One empty folder previously produced 30,931 processing cycles
                # and 26 MB of log output. Not the signature from before: a
                # failed attempt can already have changed the download (files
                # of an earlier attempt moved back into it), and that alone
                # would look like a change worth retrying.
                self.failed[str(path)] = await run_blocking(_item_signature, path)
                logger.warning(
                    f"Not retrying {path.name} until its contents change "
                    f"(remove it from the watch directory to stop this notice)"
                )

    @scan_loop.before_loop
    async def before_scan_loop(self):
        """Wait for the bot to be ready before starting the scan loop."""
        await self.bot.wait_until_ready()


