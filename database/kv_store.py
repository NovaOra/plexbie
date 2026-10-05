# path: database/kv_store.py
"""Key-value store helpers for plugin data persistence"""
import json
from typing import Any, Dict, Optional

from sqlalchemy import select, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import OperationalError

from core.logging import get_logger
from database.session import get_session
from database.models import KeyValueStore

logger = get_logger(__name__)


def _decode(value: Any) -> Any:
    """A stored value back as Python. Databases made by older versions declared
    the column JSON, which SQLite treats as numeric: "1" written there comes back
    as the number 1, not text to parse."""
    if not isinstance(value, (str, bytes, bytearray)):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


async def kv_get(namespace: str, key: str, default: Any = None) -> Any:
    """Get a value from the key-value store"""
    async with get_session() as session:
        result = await session.execute(
            select(KeyValueStore)
            .where(
                KeyValueStore.namespace == namespace,
                KeyValueStore.key == key
            )
            # Deliberately not scalar_one_or_none(): that raises
            # MultipleResultsFound on a duplicated key, and callers swallow the
            # exception and fall back to defaults - which for media_cleanup meant
            # silently losing the exemption list. Take the newest row instead so a
            # database written before the uniqueness constraint still reads.
            .order_by(KeyValueStore.id.desc())
            .limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return default
        return _decode(row.value)


async def kv_set(namespace: str, key: str, value: Any) -> None:
    """Set a single value in the key-value store. See kv_set_many for the batch form."""
    await kv_set_many(namespace, {key: value})


async def kv_update(namespace: str, key: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Add or change fields on one stored record. The updated record, or None if there was none."""
    record = await kv_get(namespace, key)
    if not isinstance(record, dict):
        return None
    record.update(fields)
    await kv_set(namespace, key, record)
    return record


async def kv_set_many(namespace: str, items: Dict[str, Any]) -> None:
    """Upsert every key in `items` within one transaction.

    Uses a single INSERT ... ON CONFLICT DO UPDATE so that two concurrent writers
    cannot both observe "no existing row" and each insert one. The previous
    select-then-insert yielded duplicate rows that then broke every read.

    Batching matters because a commit on SQLite is an fsync: writing N keys with N
    calls to kv_set costs N transactions and N fsyncs. Both callers that do this
    write a whole namespace at once - new_media_added saves every tracked batch
    whenever one changes, and auto_link_users used to rewrite every invite when a
    single one linked, most of them byte-identical.
    """
    if not items:
        return

    rows = [
        {"namespace": namespace, "key": key, "value": json.dumps(value)}
        for key, value in items.items()
    ]

    statement = sqlite_insert(KeyValueStore)
    statement = statement.on_conflict_do_update(
        index_elements=[KeyValueStore.namespace, KeyValueStore.key],
        # excluded.value rather than a literal: one statement covers every row,
        # and each row must update with its own value.
        set_={"value": statement.excluded.value},
    )

    try:
        async with get_session() as session:
            await session.execute(statement, rows)
            await session.commit()
    except OperationalError:
        # ON CONFLICT needs the unique index to exist. If the startup migration
        # could not apply it, fall back to the older read-then-write rather than
        # failing the write outright - still in one transaction.
        logger.warning(
            f"Upsert unavailable for {len(rows)} key(s) in {namespace} (missing "
            f"unique index); falling back to read-then-write"
        )
        async with get_session() as session:
            for row in rows:
                await _kv_set_row(session, row["namespace"], row["key"], row["value"])
            await session.commit()


async def _kv_set_row(session, namespace: str, key: str, json_value: str) -> None:
    """Non-atomic single-row write, used only when the unique index is absent."""
    result = await session.execute(
        select(KeyValueStore)
        .where(
            KeyValueStore.namespace == namespace,
            KeyValueStore.key == key
        )
        .order_by(KeyValueStore.id.desc())
        .limit(1)
    )
    row = result.scalars().first()
    if row:
        row.value = json_value
    else:
        session.add(KeyValueStore(namespace=namespace, key=key, value=json_value))


async def kv_delete(namespace: str, key: str) -> bool:
    """Delete a key from the key-value store. Returns True if deleted."""
    async with get_session() as session:
        result = await session.execute(
            delete(KeyValueStore).where(
                KeyValueStore.namespace == namespace,
                KeyValueStore.key == key
            )
        )
        await session.commit()
        return result.rowcount > 0


async def kv_delete_many(namespace: str, keys) -> int:
    """Delete several keys in one transaction. Returns the number removed.

    The counterpart to kv_set_many, and the reason it exists: new_media_added
    pruned its batches out of an in-memory dict and then saved the survivors,
    which upserts and therefore left every removed row in place. It logged
    "Cleaned up 47 old batches" twenty times over, on the same 47 batches, because
    each restart loaded them straight back.
    """
    keys = [str(key) for key in keys]
    if not keys:
        return 0

    async with get_session() as session:
        result = await session.execute(
            delete(KeyValueStore).where(
                KeyValueStore.namespace == namespace,
                KeyValueStore.key.in_(keys),
            )
        )
        await session.commit()
        return result.rowcount or 0


async def kv_get_all(namespace: str) -> Dict[str, Any]:
    """Get all key-value pairs in a namespace"""
    async with get_session() as session:
        result = await session.execute(
            select(KeyValueStore).where(KeyValueStore.namespace == namespace)
        )
        rows = result.scalars().all()
        data = {}
        for row in rows:
            data[row.key] = _decode(row.value)
        return data
