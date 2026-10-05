# path: database/request_store.py
"""Media requests, stored one row per request.

Previously a single JSON file that every writer rewrote in full to append one
record - 661 KB by the time this was written, read and rewritten synchronously on
the event loop for roughly 15 ms per submission, growing linearly with every
distinct title ever requested.

The records themselves are not disposable, which is why this migrates rather than
prunes. media_cleanup reads them daily and, for each medium's *newest* request,
either ensures Sonarr/Radarr is monitoring it (within REQUEST_EXPIRY_DAYS) or
un-monitors it (beyond). A medium with no record is skipped entirely, so deleting
one would leave an approved title un-monitored with nothing left to correct it.

One row per request means appending costs one small write, restoring a view costs
one keyed read, and the daily reconciliation costs one query.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.logging import get_logger
from database.kv_store import kv_get, kv_get_all, kv_set, kv_update

logger = get_logger(__name__)

#: Key-value namespace holding one entry per approval message.
#: A request that came from Seerr, as saved: "seerr", or "overseerr" from before the rename.
SEERR_SOURCES = ("seerr", "overseerr")
REQUESTS_NAMESPACE = "media_requests"

#: Statuses a request can carry. "pending" was the only one ever written before -
#: nothing updated it - so every historical record claims to be outstanding.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DECLINED = "declined"


def _now() -> str:
    """Timezone-aware ISO timestamp.

    Aware on purpose: the file this replaces held 154 naive and 61 aware values in
    the same field, and a consumer that forgets to coerce raises TypeError on the
    naive ones.
    """
    return datetime.now(timezone.utc).isoformat()


async def save_request(
    message_id: int,
    *,
    user_id: int,
    media: Dict[str, Any],
    seasons: Any = None,
    monitor: bool = False,
    media_type: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a newly submitted request, keyed by its approval message.

    `extra` carries website-only facts (requester_name, plex_account_id) for a
    request made by someone signed in with Plex and without a Discord account.
    """
    record = {
        "user_id": user_id,
        "media": media,
        "seasons": seasons,
        "monitor": monitor,
        "status": STATUS_PENDING,
        "timestamp": _now(),
    }
    if media_type:
        record["media_type"] = media_type
    if extra:
        record.update({k: v for k, v in extra.items() if k not in record})
    await kv_set(REQUESTS_NAMESPACE, str(message_id), record)


async def get_request(message_id: int) -> Optional[Dict[str, Any]]:
    """One request by approval message id, or None."""
    return await kv_get(REQUESTS_NAMESPACE, str(message_id))


async def all_requests() -> Dict[str, Dict[str, Any]]:
    """Every request, keyed by message id as a string.

    Shaped exactly like the JSON file it replaces so that
    media_cleanup._latest_requests_by_media needs no change.
    """
    return await kv_get_all(REQUESTS_NAMESPACE)


async def mark_resolved(message_id: int, status: str, resolved_by: Optional[str] = None) -> bool:
    """Record that a request was approved or declined. True if a record existed.

    Additive: the record stays, because the monitoring reconciliation still needs
    it. This only makes "what is actually outstanding" answerable, which it was
    not before - every record claimed to be pending, including ones actioned a
    year ago.
    """
    fields = {"status": status, "resolved_at": _now()}
    if resolved_by:
        fields["resolved_by"] = resolved_by
    if not await set_fields(message_id, **fields):
        logger.warning(f"Cannot mark request {message_id} as {status}: no record found")
        return False
    return True


async def pending_requests() -> Dict[str, Dict[str, Any]]:
    """Only the requests still awaiting a decision."""
    return {
        key: record
        for key, record in (await all_requests()).items()
        if record.get("status", STATUS_PENDING) == STATUS_PENDING
    }


async def set_fields(message_id: int, **fields: Any) -> bool:
    """Add or change fields on one request. True if it existed."""
    return await kv_update(REQUESTS_NAMESPACE, str(message_id), **fields) is not None


async def find_by_seerr_id(request_id: int) -> Optional[tuple]:
    """(key, record) of the request Seerr knows as `request_id`, or None."""
    for key, record in (await all_requests()).items():
        if isinstance(record, dict) and str(record.get("overseerr_request_id") or "") == str(request_id):
            return key, record
    return None
