# path: portal/books.py
"""The book shelves, read from the Audiobookshelf-style library folders.

bookshelf_processor files every download as <library>/<Author>/<Title>/, with a
cover.jpg when one was found. Synchronous (it walks directories); call it via
run_blocking.
"""
import hashlib
from pathlib import Path
from typing import Dict, List, Optional


def book_id(kind: str, author: str, title: str) -> str:
    digest = hashlib.sha1(f"{kind}/{author}/{title}".encode()).hexdigest()[:16]
    return f"shelf-{kind[0]}{digest}"


def scan(audiobooks: str, ebooks: str) -> Dict[str, dict]:
    """{id: book} for every title folder on both shelves."""
    out: Dict[str, dict] = {}
    for kind, root in (("audiobook", audiobooks), ("ebook", ebooks)):
        base = Path(root)
        if not base.is_dir():
            continue
        for author_dir in base.iterdir():
            if not author_dir.is_dir() or author_dir.name.startswith("."):
                continue
            for title_dir in author_dir.iterdir():
                if not title_dir.is_dir() or title_dir.name.startswith("."):
                    continue
                cover = title_dir / "cover.jpg"
                bid = book_id(kind, author_dir.name, title_dir.name)
                out[bid] = {
                    "kind": kind,
                    "id": bid,
                    "title": title_dir.name,
                    "author": author_dir.name,
                    "year": "",
                    "poster": f"/img/book/{bid}?v=2" if cover.is_file() else None,
                    "genres": [],
                    "addedAt": int(title_dir.stat().st_mtime),
                    "availability": "available",
                    "_cover": str(cover) if cover.is_file() else None,
                }
    return out


def public(book: dict) -> dict:
    return {k: v for k, v in book.items() if not k.startswith("_")}


def cover_bytes(path: Optional[str]) -> Optional[bytes]:
    if not path:
        return None
    p = Path(path)
    return p.read_bytes() if p.is_file() else None


def shelf_titles(books: Dict[str, dict]) -> List[str]:
    return [b["title"].casefold() for b in books.values()]
