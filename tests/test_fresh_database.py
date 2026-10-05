# path: tests/test_fresh_database.py
"""A brand-new install's database has every table, plugins' included."""
import asyncio
import sqlite3
import tempfile

import conftest  # noqa: F401


def test_a_fresh_database_has_the_plugin_tables_too():
    from database import session

    path = tempfile.mkdtemp() + "/fresh.db"
    asyncio.run(session.init_database(f"sqlite:///{path}"))
    asyncio.run(session.engine.dispose())
    tables = {r[0] for r in sqlite3.connect(path).execute("select name from sqlite_master where type='table'")}
    # user_mgmt reads watch_party_credits whether or not watch_party has loaded yet.
    assert {"watch_party_credits", "watch_party_sessions"} <= tables
