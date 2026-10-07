-- plexbie.com's own visit counts (worker.js stores them, stats/worker.js reads them).
-- One row per event; the visitor is that day's anonymous number, never an address.
-- Apply with `cloudflare/node_modules/.bin/wrangler d1 migrations apply plexbie-stats --remote
-- --config cloudflare/wrangler.jsonc` from web/. On a database that already has the table
-- this changes nothing.
CREATE TABLE IF NOT EXISTS events (
  ts INTEGER NOT NULL,
  day TEXT NOT NULL,
  event TEXT NOT NULL,
  path TEXT,
  label TEXT,
  value REAL,
  referrer TEXT,
  country TEXT,
  region TEXT,
  browser TEXT,
  os TEXT,
  device TEXT,
  visitor TEXT,
  site TEXT
);
-- The dashboard asks by day; the nightly clean-up deletes by time.
CREATE INDEX IF NOT EXISTS events_day ON events (day);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
