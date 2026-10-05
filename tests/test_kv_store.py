# path: tests/test_kv_store.py
"""Key-value store durability: atomic upsert and the uniqueness migration.

Regression coverage for: key_value_store had no uniqueness on (namespace, key),
so kv_set's select-then-insert could write two rows for one logical key. After
that, kv_get's scalar_one_or_none() raised MultipleResultsFound on every read -
and media_cleanup swallowed it, falling back to DEFAULT_CONFIG and silently
losing its exemption list while still deleting media.
"""
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

import conftest  # noqa: F401

UNIQUE_INDEX = "uq_key_value_store_namespace_key"

LEGACY_SCHEMA = """
CREATE TABLE key_value_store (
    id INTEGER NOT NULL PRIMARY KEY,
    namespace VARCHAR(64) NOT NULL,
    key VARCHAR(255) NOT NULL,
    value TEXT NOT NULL
)
"""


def _tmp_db(name):
    return str(Path(tempfile.mkdtemp()) / name)


def _index_count(path):
    with sqlite3.connect(path) as con:
        return con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name=?",
            (UNIQUE_INDEX,),
        ).fetchone()[0]


def _row_count(path, namespace, key):
    with sqlite3.connect(path) as con:
        return con.execute(
            "SELECT COUNT(*) FROM key_value_store WHERE namespace=? AND key=?",
            (namespace, key),
        ).fetchone()[0]


async def _init(path):
    """Point the module-level engine at `path`, disposing any previous one.

    init_database() replaces the global engine without disposing it, which is
    harmless in production (called once at startup) but leaks connection pools
    across tests - and a pool still holding the old file raises from its worker
    thread during teardown.
    """
    import database.session as session_module

    if session_module.engine is not None:
        await session_module.engine.dispose()
    await session_module.init_database(f"sqlite:///{path}")


async def _dispose():
    import database.session as session_module

    if session_module.engine is not None:
        await session_module.engine.dispose()


# --- fresh database ---

def test_fresh_database_gets_the_unique_index():
    path = _tmp_db("fresh.db")

    async def scenario():
        await _init(path)
        await _dispose()

    asyncio.run(scenario())
    assert _index_count(path) == 1


def test_set_then_get_roundtrip():
    from database.kv_store import kv_get, kv_set

    path = _tmp_db("roundtrip.db")

    async def scenario():
        await _init(path)
        await kv_set("ns", "k", {"a": 1})
        return await kv_get("ns", "k")

    assert asyncio.run(scenario()) == {"a": 1}


def test_repeated_set_updates_in_place_without_duplicating():
    from database.kv_store import kv_get, kv_set

    path = _tmp_db("upsert.db")

    async def scenario():
        await _init(path)
        for value in range(5):
            await kv_set("ns", "k", {"v": value})
        return await kv_get("ns", "k")

    assert asyncio.run(scenario()) == {"v": 4}
    assert _row_count(path, "ns", "k") == 1


def test_concurrent_sets_do_not_create_duplicate_rows():
    """The original race: two coroutines both saw "no row" and both inserted."""
    from database.kv_store import kv_get, kv_set

    path = _tmp_db("concurrent.db")

    async def scenario():
        await _init(path)
        await asyncio.gather(*(kv_set("ns", "same", {"writer": i}) for i in range(8)))
        return await kv_get("ns", "same")

    result = asyncio.run(scenario())
    assert _row_count(path, "ns", "same") == 1
    assert "writer" in result


def test_missing_key_returns_default():
    from database.kv_store import kv_get

    path = _tmp_db("default.db")

    async def scenario():
        await _init(path)
        return await kv_get("ns", "absent", "fallback")

    assert asyncio.run(scenario()) == "fallback"


def test_get_all_and_delete():
    from database.kv_store import kv_delete, kv_get, kv_get_all, kv_set

    path = _tmp_db("getall.db")

    async def scenario():
        await _init(path)
        await kv_set("ns", "a", 1)
        await kv_set("ns", "b", 2)
        await kv_set("other", "c", 3)
        everything = await kv_get_all("ns")
        deleted = await kv_delete("ns", "a")
        return everything, deleted, await kv_get("ns", "a")

    everything, deleted, after = asyncio.run(scenario())
    assert set(everything) == {"a", "b"}
    assert deleted is True
    assert after is None


# --- legacy database that predates the constraint ---

def _legacy_db_with_duplicates():
    path = _tmp_db("legacy.db")
    with sqlite3.connect(path) as con:
        con.execute(LEGACY_SCHEMA)
        # Two rows for one logical key - what broke every subsequent read.
        con.execute(
            "INSERT INTO key_value_store (namespace, key, value) VALUES (?,?,?)",
            ("media_cleanup", "config", json.dumps({"generation": "old"})))
        con.execute(
            "INSERT INTO key_value_store (namespace, key, value) VALUES (?,?,?)",
            ("media_cleanup", "config", json.dumps({"generation": "new"})))
        con.execute(
            "INSERT INTO key_value_store (namespace, key, value) VALUES (?,?,?)",
            ("media_cleanup", "tracking", json.dumps({})))
        con.commit()
    assert _row_count(path, "media_cleanup", "config") == 2
    assert _index_count(path) == 0
    return path


def test_migration_collapses_duplicates_and_keeps_newest():
    from database.kv_store import kv_get

    path = _legacy_db_with_duplicates()

    async def scenario():
        await _init(path)
        return await kv_get("media_cleanup", "config")

    value = asyncio.run(scenario())
    assert _row_count(path, "media_cleanup", "config") == 1
    assert value == {"generation": "new"}, "migration kept the wrong row"
    assert _index_count(path) == 1


def test_migration_preserves_unrelated_rows():
    path = _legacy_db_with_duplicates()

    async def scenario():
        await _init(path)
        await _dispose()

    asyncio.run(scenario())
    assert _row_count(path, "media_cleanup", "tracking") == 1


def test_migration_is_idempotent():
    """It runs on every startup, so a second pass must be a no-op."""
    from database.kv_store import kv_get

    path = _legacy_db_with_duplicates()

    async def scenario():
        await _init(path)
        await _init(path)
        await _init(path)
        return await kv_get("media_cleanup", "config")

    assert asyncio.run(scenario()) == {"generation": "new"}
    assert _index_count(path) == 1


def test_reads_survive_duplicates_even_without_the_index():
    """Defence in depth: kv_get must not raise MultipleResultsFound if the
    migration could not apply for any reason.
    """
    from database.kv_store import kv_get

    path = _legacy_db_with_duplicates()

    async def scenario():
        await _init(path)
        # Re-introduce a duplicate behind the constraint's back, via a separate
        # connection. Deliberately do NOT re-init afterwards: the migration would
        # collapse the duplicate and re-add the index, which is the exact state
        # this test needs to avoid.
        with sqlite3.connect(path) as con:
            con.execute("DROP INDEX IF EXISTS " + UNIQUE_INDEX)
            con.execute(
                "INSERT INTO key_value_store (namespace, key, value) VALUES (?,?,?)",
                ("media_cleanup", "config", json.dumps({"generation": "newest"})))
            con.commit()

        result = await kv_get("media_cleanup", "config")
        # Dispose inside the running loop; leaving it to interpreter teardown
        # makes aiosqlite's worker thread raise against the mutated file.
        await _dispose()
        return result

    assert asyncio.run(scenario()) == {"generation": "newest"}
    assert _index_count(path) == 0, "test must exercise the no-index path"


# --- batched writes: one transaction, not one per key ---

def test_set_many_writes_every_key():
    from database.kv_store import kv_get_all, kv_set_many

    path = _tmp_db("setmany.db")

    async def scenario():
        await _init(path)
        await kv_set_many("ns", {"a": 1, "b": {"nested": True}, "c": [1, 2]})
        result = await kv_get_all("ns")
        await _dispose()
        return result

    assert asyncio.run(scenario()) == {"a": 1, "b": {"nested": True}, "c": [1, 2]}


def test_set_many_updates_each_row_with_its_own_value():
    """The upsert must use excluded.value, not one literal for every row."""
    from database.kv_store import kv_get_all, kv_set_many

    path = _tmp_db("setmany_update.db")

    async def scenario():
        await _init(path)
        await kv_set_many("ns", {"a": "first", "b": "second"})
        await kv_set_many("ns", {"a": "updated-a", "b": "updated-b"})
        result = await kv_get_all("ns")
        await _dispose()
        return result

    assert asyncio.run(scenario()) == {"a": "updated-a", "b": "updated-b"}


def test_set_many_does_not_duplicate_rows():
    from database.kv_store import kv_set_many

    path = _tmp_db("setmany_dupes.db")

    async def scenario():
        await _init(path)
        for _ in range(3):
            await kv_set_many("ns", {"a": 1, "b": 2})
        await _dispose()

    asyncio.run(scenario())
    assert _row_count(path, "ns", "a") == 1
    assert _row_count(path, "ns", "b") == 1


def test_set_many_is_one_transaction():
    """A commit on SQLite is an fsync, so N keys must not cost N commits."""
    from database.kv_store import kv_set_many

    path = _tmp_db("setmany_txn.db")
    commits = {"count": 0}

    async def scenario():
        await _init(path)

        import database.kv_store as module
        from sqlalchemy.ext.asyncio import AsyncSession

        real_commit = AsyncSession.commit

        async def counting_commit(self):
            commits["count"] += 1
            return await real_commit(self)

        AsyncSession.commit = counting_commit
        try:
            await kv_set_many("ns", {f"k{i}": i for i in range(10)})
        finally:
            AsyncSession.commit = real_commit
        await _dispose()

    asyncio.run(scenario())
    # get_session commits on exit as well as the explicit commit inside, so the
    # ceiling is what matters: it must not scale with the number of keys.
    assert commits["count"] <= 2, (
        f"10 keys took {commits['count']} commits; the write is still per-key"
    )


def test_set_many_of_nothing_is_a_no_op():
    from database.kv_store import kv_set_many

    path = _tmp_db("setmany_empty.db")

    async def scenario():
        await _init(path)
        await kv_set_many("ns", {})
        await _dispose()

    asyncio.run(scenario())
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM key_value_store").fetchone()[0] == 0
def test_set_many_works_without_the_unique_index():
    """The fallback path must still write every key, and still in one transaction.

    Getting a database into that state takes care. create_all builds the table
    with its UniqueConstraint, whose auto-index satisfies ON CONFLICT on its own -
    so dropping the migration's named index is not enough, and an earlier version
    of this test passed via the upsert while claiming to cover the fallback. The
    table therefore has to pre-exist in the legacy shape, as it does on a database
    written before the constraint was added.
    """
    from database.kv_store import kv_get_all, kv_set_many

    path = _tmp_db("setmany_legacy.db")
    with sqlite3.connect(path) as con:
        con.execute(LEGACY_SCHEMA)
        con.execute(
            "INSERT INTO key_value_store (namespace, key, value) "
            "VALUES ('ns','a','\"old\"')"
        )

    warnings = []
    commits = {"count": 0}

    async def scenario():
        await _init(path)

        # The migration re-adds the named index; drop it so nothing satisfies
        # ON CONFLICT and the fallback is genuinely taken.
        with sqlite3.connect(path) as con:
            con.execute("DROP INDEX IF EXISTS " + UNIQUE_INDEX)
            con.commit()

        import database.kv_store as module
        from sqlalchemy.ext.asyncio import AsyncSession

        real_warning = module.logger.warning
        real_commit = AsyncSession.commit

        def capture(message, *args, **kwargs):
            warnings.append(str(message))
            return real_warning(message, *args, **kwargs)

        async def counting_commit(self):
            commits["count"] += 1
            return await real_commit(self)

        module.logger.warning = capture
        AsyncSession.commit = counting_commit
        try:
            await kv_set_many("ns", {"a": "new", "b": "added"})
        finally:
            module.logger.warning = real_warning
            AsyncSession.commit = real_commit

        result = await kv_get_all("ns")
        await _dispose()
        return result

    result = asyncio.run(scenario())

    assert _index_count(path) == 0, "test must exercise the no-index path"
    assert any("falling back" in w for w in warnings), (
        f"the fallback never ran, so this test proves nothing; warnings: {warnings}"
    )
    assert result == {"a": "new", "b": "added"}, (
        "the fallback must update the existing key and insert the new one"
    )
    assert commits["count"] <= 3, (
        f"the fallback took {commits['count']} commits for 2 keys; it must batch too"
    )


def test_numbers_read_back_from_an_older_json_column():
    """Databases made by older versions declared the value column JSON (numeric
    affinity), so a stored 1 came back as an int and json.loads() raised -
    which stopped the website's click counter after its first click."""
    import asyncio
    import pathlib
    import sqlite3
    import tempfile
    import database.session as session_module
    from database.kv_store import kv_get, kv_get_all, kv_set_many
    db = pathlib.Path(tempfile.mkdtemp()) / "old.db"
    c = sqlite3.connect(db)
    c.execute('CREATE TABLE key_value_store (id INTEGER NOT NULL, namespace VARCHAR(64) NOT NULL, "key" VARCHAR(255) NOT NULL, '
              'value JSON NOT NULL, updated_at DATETIME, PRIMARY KEY (id))')
    c.commit()
    c.close()

    async def go():
        session_module._LEGACY_REQUESTS_FILE = pathlib.Path(tempfile.mkdtemp()) / "none.json"
        if session_module.engine is not None:
            await session_module.engine.dispose()
        await session_module.init_database(f"sqlite:///{db}")
        await kv_set_many("ns", {"count": 1, "word": "hi", "obj": {"a": 2}})
        return await kv_get_all("ns"), await kv_get("ns", "count")
    everything, one = asyncio.run(go())
    assert everything == {"count": 1, "word": "hi", "obj": {"a": 2}} and one == 1
