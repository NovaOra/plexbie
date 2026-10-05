# path: core/blocking.py
"""Run blocking (synchronous) I/O off the asyncio event loop.

plexapi is entirely synchronous: every call performs an HTTP request, with a
30 second default timeout (plexapi.TIMEOUT). Awaiting nothing and calling one
directly from a coroutine stops the *whole* event loop for the duration -
including discord.py's gateway heartbeat, which is what makes the bot appear to
freeze and then drop its connection.

Two rules when using this:

1. Wrap a coarse unit of work, not each individual call. plexapi objects are
   lazy: ``library.all()`` performs a request, and so does ``item.seasons()`` on
   each object it returned. Wrapping only the outer call leaves the per-object
   requests running on the loop. Push the whole traversal into one thread.

2. Prefer returning plain data (dicts, lists, scalars) from the thread. Handing a
   lazy plexapi object back to async code invites an innocuous-looking attribute
   access to perform an HTTP request on the loop again. Where an object must come
   back so a method can be called on it later, that later call belongs in a
   thread too.
"""
import asyncio
from typing import Any, Callable, TypeVar

from core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


async def run_blocking(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Await a synchronous callable, executed in a worker thread.

    Exceptions propagate to the caller exactly as if the call had been made
    inline, so existing try/except blocks around plexapi calls keep working
    unchanged.

    Note that a thread cannot be cancelled: if the surrounding task is cancelled,
    the call keeps running to completion in the background. That is acceptable
    here because plexapi requests are bounded by their own timeout, and it is
    strictly better than blocking the loop.
    """
    return await asyncio.to_thread(func, *args, **kwargs)
