# Tests

Regression coverage for the security and durability fixes. Every test here maps
to a defect that was live in production, so a failure means a real behaviour
regressed rather than a style preference being violated.

## Running them

No dev dependencies needed — this works against the production image as-is:

```bash
docker run --rm -v "$PWD":/app -w /app \
  plexbie:latest python tests/run_all.py
```

Filter to particular modules by substring:

```bash
python tests/run_all.py config kv
```

They are ordinary `def test_*` functions using bare `assert`, so pytest works
once installed (`pip install -r requirements-dev.txt -c constraints.txt`):

```bash
pytest tests/ -v
```

Nothing here touches the live database, `config/.env`, Discord, or Plex. The
database tests build throwaway SQLite files in a temp directory; the end-to-end
webhook tests bind `127.0.0.1:18099` only for the duration of a test.

## What each module pins

| Module | Defect it guards against |
| --- | --- |
| `test_permissions.py` | No view defined `interaction_check`, so persistent approval views dispatched to whoever clicked. Anyone who could see the admin channel could grant Plex access or approve requests. |
| `test_webhook_security.py` | `hmac.compare_digest` raises `TypeError` on non-ASCII `str`, turning a bad credential into an unauthenticated 500. Plus the Plex validator returning True when a secret was set but no token sent, and unknown services passing through. |
| `test_webhook_routes.py` | `/webhook/sonarr`, `/webhook/radarr` and `/webhook/plex` registered straight onto the aiohttp app, so the validator never ran — `_validate_arr_signature` was dead code — while the startup banner claimed auth was enabled. |
| `test_webhook_e2e.py` | The wrapper and the handlers must compose over real HTTP. Unit tests cannot see that the wrapper reads the body before a handler calls `request.post()` on multipart. |
| `test_kv_store.py` | No uniqueness on `(namespace, key)`, so `kv_set`'s select-then-insert could write two rows; `kv_get` then raised `MultipleResultsFound` forever and `media_cleanup` silently fell back to defaults, losing its exemption list while still deleting media. |
| `test_config.py` | `BOT_OWNER_ID=novaora` — a username where a snowflake belonged — was discarded silently by `_int_or_none`, across 25 fields. Six more fields used a bare `int()` that crashed the whole bot on one typo. |
| `test_cleanup_settings_load.py` | After a restart the cleanup cog held DEFAULT_CONFIG until its first load. `/cleanup config`, the panel's status and Run Scan buttons, and a failed database read all acted on those defaults: a save emptied the exemption list and the skipped libraries and turned a switched-off cleanup back on, and Run Scan went ahead against a stored "off". Every reader now loads first, and nothing is saved until the stored settings have loaded. A failed save puts memory back to what is stored, and a website change refused partway through changes nothing. |
| `test_cleanup_kept_titles.py` | A title kept forever was matched by its Plex key alone and a skipped library by its name alone. Renaming a skipped library in Plex made every title in it past the threshold removable by the next daily check, and a title added back under a new key lost its "keep forever". Kept titles now carry their ids, skipped libraries their section keys, and a skipped library that matches nothing stops every removal and tells the admins. |
| `test_bookshelf_moves.py` | Multi-disc audiobooks were flattened by file name, so `CD2/01.mp3` silently replaced `CD1/01.mp3` before the source was deleted. A partly failed move still deleted the hint, announced the book and DM'd the requester, and the retry filed the rest as "Title (2)". |

## Conventions

- Async code is exercised via `asyncio.run(...)` inside a sync test, so no
  `pytest-asyncio` plugin is required and both runners behave identically.
- Fakes live in `helpers.py` and are hand-rolled rather than mocked: the
  production code reads only a couple of attributes off each discord.py object,
  and explicit fakes document that access pattern.
- `conftest.py` puts the project on `sys.path` and supplies a dummy
  `DISCORD_BOT_TOKEN`, since `Config` validates that one is present.
- Several tests assert on structure (AST walks, "is this class a subclass of
  `AdminOnlyView`") rather than behaviour. That is deliberate: the bugs were
  *omissions* — a missing gate, a route registered on the wrong object — and an
  omission in future code is caught by asserting the invariant, not by calling
  something that does not exist yet.
