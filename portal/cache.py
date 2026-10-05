# path: portal/cache.py
"""A small time-based cache shared by every viewer of the site.

Many people looking at the same page must not mean many calls to Plex, Sonarr
or Radarr. Each key is loaded at most once per TTL; concurrent misses for the
same key wait on one load. When a reload fails, the last good value is served
rather than an error, because a stale library count beats a broken page, but
never for access decisions ("auth:" keys), where stale means letting in someone
who has since lost access.

Members choose some keys (every search is one), so the cache holds at most
MAX_KEYS, dropping the least recently used.
"""
import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, Tuple

from core.logging import get_logger

logger = get_logger(__name__)

MAX_KEYS = 2000
#: Keys whose failed reload must raise rather than serve the last value.
NO_STALE = ("auth:",)


class TTLCache:
    def __init__(self, max_keys: int = MAX_KEYS) -> None:
        self.max_keys = max_keys
        self._values: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self._locks: Dict[str, asyncio.Lock] = {}

    def _fresh(self, key: str, ttl: float):
        hit = self._values.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            self._values.move_to_end(key)
            return True, hit[1]
        return False, None

    def _store(self, key: str, value: Any) -> None:
        self._values[key] = (time.monotonic(), value)
        self._values.move_to_end(key)
        while len(self._values) > self.max_keys:
            old, _ = self._values.popitem(last=False)
            lock = self._locks.get(old)
            if lock is not None and not lock.locked():
                del self._locks[old]

    async def get(self, key: str, ttl: float, load: Callable[[], Awaitable[Any]]) -> Any:
        fresh, value = self._fresh(key, ttl)
        if fresh:
            return value
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            fresh, value = self._fresh(key, ttl)
            if fresh:
                return value
            try:
                value = await load()
            except Exception as e:
                stale = self._values.get(key)
                if stale is not None and not key.startswith(NO_STALE):
                    logger.warning(f"portal cache: reload of {key} failed ({e}); serving the last good value")
                    return stale[1]
                if stale is None:
                    self._locks.pop(key, None)        # nothing cached: don't keep a lock for it
                raise
            self._store(key, value)
            return value

    def drop(self, key: str) -> None:
        self._values.pop(key, None)
