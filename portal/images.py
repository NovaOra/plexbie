# path: portal/images.py
"""Posters and covers, fetched by the server and cached on disk.

Visitors' browsers never contact TMDB, Open Library or Plex: the page asks this
proxy, which fetches from an allow-listed source, adds the Plex token server
side, and keeps a copy. Only fixed URL shapes are accepted, so the route cannot
be turned into a general-purpose fetcher.

Only signed-in visitors get images (portal/app.py), each address is limited to
IMAGE_TRIES a minute, Plex widths snap to a few sizes, and the cache is capped
at WEB_IMAGE_CACHE_MB: past that, the images used least recently are deleted.
Otherwise anyone could fill the disk by asking for every cover there is.
"""
import asyncio
import hashlib
import os
import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlencode

from aiohttp import web

from core.blocking import run_blocking
from core.logging import get_logger
from portal import books as shelf
from portal.plex import PLEX_ART_PATH

logger = get_logger(__name__)

#: Bumped when cached images must be refetched by browsers. 2: fixed truncated downloads.
IMAGE_VERSION = "2"

TMDB_FILE = re.compile(r"^[A-Za-z0-9_-]{6,64}\.(jpg|png)$")
TMDB_SIZES = {"w185", "w342", "w780", "w1280"}
AVATAR_HASH = re.compile(r"^(a_)?[0-9a-f]{32}$")
OL_FILE = re.compile(r"^(\d{1,12})-(S|M|L)\.jpg$")
MAX_BYTES = 8 * 1024 * 1024
#: The widths the site asks Plex for; anything else snaps up to the next one.
PLEX_WIDTHS = (185, 342, 780, 1280, 1600)
IMAGE_TRIES = (600, 60)         # images per address per minute
PRUNE_EVERY = 100               # check the cache size after this many new images
# private: the browser may keep them, but not Cloudflare's edge, which would then
# hand members-only images to anyone.
CACHE_HEADERS = {"Cache-Control": "private, max-age=604800, immutable", "X-Content-Type-Options": "nosniff"}


def _read(path: Path) -> Optional[Tuple[bytes, str]]:
    if not path.is_file():
        return None
    meta = path.with_suffix(".type")
    ctype = meta.read_text().strip() if meta.is_file() else "image/jpeg"
    body = path.read_bytes()
    try:
        os.utime(path)                  # "used recently", for pruning
    except OSError:
        pass
    return body, ctype


def _prune(root: Path, limit: int) -> int:
    """Delete the least recently used images until the cache is under `limit`
    bytes (to 90% of it, so this doesn't run on every new image). Returns the
    number deleted."""
    files = []
    total = 0
    for f in root.rglob("*"):
        if f.is_file() and f.suffix not in (".type", ".part"):
            st = f.stat()
            files.append((st.st_mtime, st.st_size, f))
            total += st.st_size
    if total <= limit:
        return 0
    removed = 0
    for _, size, f in sorted(files):
        if total <= limit * 0.9:
            break
        for victim in (f, f.with_suffix(".type")):
            try:
                victim.unlink()
            except OSError:
                pass
        total -= size
        removed += 1
    return removed


def _write(path: Path, body: bytes, ctype: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(body)
    path.with_suffix(".type").write_text(ctype)
    tmp.replace(path)


class ImageProxy:
    def __init__(self, services, data, cache_dir: str, max_mb: int = 500):
        self.services = services
        self.data = data
        self.dir = Path(cache_dir)
        self.max_bytes = max(50, int(max_mb)) * 1024 * 1024
        self._new = 0

    async def _fetch(self, url: str, key: str) -> web.Response:
        response = await self._fetch_once(url, key)
        if response.status in (502, 504):
            # A first request for a resized Plex image sometimes fails while Plex
            # is still generating it; one retry gets the finished image.
            await asyncio.sleep(0.4)
            response = await self._fetch_once(url, key)
        return response

    async def _fetch_once(self, url: str, key: str) -> web.Response:
        key = f"v{IMAGE_VERSION}/{key}" if not key.startswith("v") else key
        path = self.dir / hashlib.sha1(key.encode()).hexdigest()[:2] / hashlib.sha1(key.encode()).hexdigest()
        cached = await run_blocking(_read, path)
        if cached:
            return web.Response(body=cached[0], content_type=cached[1], headers=CACHE_HEADERS)
        try:
            async with self.services.http_session.get(url, timeout=15) as r:
                ctype = r.headers.get("Content-Type", "").split(";")[0]
                if r.status != 200 or not ctype.startswith("image/"):
                    return web.Response(status=404)
                # Read to the end. A single content.read(n) returns whatever has
                # arrived so far, which cut large backdrops off part-way and then
                # cached the broken copy.
                chunks, total = [], 0
                async for chunk in r.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > MAX_BYTES:
                        return web.Response(status=413)
                    chunks.append(chunk)
                body = b"".join(chunks)
                expected = r.headers.get("Content-Length")
                if expected and expected.isdigit() and int(expected) != len(body):
                    logger.info(f"portal image {key}: got {len(body)} of {expected} bytes; not caching")
                    return web.Response(status=502)
        except Exception as e:
            logger.info(f"portal image fetch failed for {key}: {e}")
            return web.Response(status=502)
        try:
            await run_blocking(_write, path, body, ctype)
            self._new += 1
            if self._new % PRUNE_EVERY == 1:
                removed = await run_blocking(_prune, self.dir, self.max_bytes)
                if removed:
                    logger.info(f"portal image cache: removed {removed} images used least recently")
        except OSError as e:
            logger.warning(f"portal image cache write failed: {e}")
        return web.Response(body=body, content_type=ctype, headers=CACHE_HEADERS)

    async def tmdb(self, request: web.Request) -> web.Response:
        size, name = request.match_info["size"], request.match_info["file"]
        if size not in TMDB_SIZES or not TMDB_FILE.match(name):
            return web.Response(status=404)
        return await self._fetch(f"https://image.tmdb.org/t/p/{size}/{name}", f"tmdb/{size}/{name}")

    async def openlibrary(self, request: web.Request) -> web.Response:
        m = OL_FILE.match(request.match_info["file"])
        if not m:
            return web.Response(status=404)
        return await self._fetch(f"https://covers.openlibrary.org/b/id/{m.group(1)}-{m.group(2)}.jpg?default=false", f"ol/{m.group(0)}")

    async def plex(self, request: web.Request) -> web.Response:
        path = request.query.get("p", "")
        try:
            asked = int(request.query.get("w", "342"))
        except ValueError:
            asked = 342
        width = next((w for w in PLEX_WIDTHS if w >= asked), PLEX_WIDTHS[-1])
        cfg = self.services.config
        if not PLEX_ART_PATH.match(path) or not cfg.plex_url or not cfg.plex_token:
            return web.Response(status=404)
        height = round(width * 1.5) if "/thumb/" in path else round(width * 0.5625)
        query = urlencode({"width": width, "height": height, "minSize": 1, "upscale": 1, "url": path, "X-Plex-Token": cfg.plex_token})
        return await self._fetch(f"{cfg.plex_url.rstrip('/')}/photo/:/transcode?{query}", f"plex/{path}/{width}")

    async def avatar(self, request: web.Request) -> web.Response:
        uid, avatar = request.match_info["uid"], request.match_info["hash"]
        if not uid.isdigit() or not AVATAR_HASH.match(avatar):
            return web.Response(status=404)
        return await self._fetch(f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size=96", f"avatar/{uid}/{avatar}")

    async def book(self, request: web.Request) -> web.Response:
        book = (await self.data._shelf()).get(request.match_info["id"])
        body = await run_blocking(shelf.cover_bytes, book.get("_cover") if book else None)
        if not body:
            return web.Response(status=404)
        return web.Response(body=body, content_type="image/jpeg", headers=CACHE_HEADERS)
