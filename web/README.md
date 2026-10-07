# Plexbie web

Two sites from one React + TypeScript + Vite codebase:

- **A household's Plexbie** (`src/`, `index.html`): sign-in, Home, requests, Manage,
  at the root of each install's own address. Built into the bot's image by the
  Dockerfile's `web` stage and served by the bot itself (see `portal/` and the
  "Web portal" section of the main README). Old `/app/...` links forward to the root.
- **plexbie.com, the project's own site** (`src/project/`, `project/`): what Plexbie is
  and how to run it. A static build, served by Cloudflare (`cloudflare/`), so it stays up
  whatever any Plexbie server is doing. Self-hosters don't need it.

```bash
npm ci
npm run dev            # http://localhost:5173 with invented sample data (?as=guest|visitor|member)
npm run build          # production build, no sample data
npm test               # unit tests (vitest), against a stubbed fetch
npm run dev:project    # the project site (plexbie.com)
npm run build:project  # its static build, in dist-project/ (VITE_REPO_PUBLIC=false hides GitHub links)
npm ci --prefix cloudflare  # the pinned wrangler that deploys it (cloudflare/package.json)
cloudflare/node_modules/.bin/wrangler deploy --config cloudflare/wrangler.jsonc   # publish it (the maintainer's Cloudflare)
node scripts/a11y.mjs  # axe accessibility audit against the dev server
node scripts/demo-gifs.mjs out/  # click through the README demos and save their frames
```

Every other wrangler command that uses the Cloudflare login (`secret put`,
`r2 object put`, `d1 execute`) goes through the same pinned copy, from `web/`.
A new stats database gets its `events` table from `cloudflare/migrations/`:
`cloudflare/node_modules/.bin/wrangler d1 migrations apply plexbie-stats --remote --config cloudflare/wrangler.jsonc`
(on a database that already has it, nothing changes).

plexbie.com's Worker sends a Content-Security-Policy with the site's pages (`PAGE_HEADERS`
in `cloudflare/worker.js`): only the site itself, plus the films and posters on
media.plexbie.com. Anything new the site loads from elsewhere goes in that list too.
It has none of a household's addresses: `/api`, `/img`, `/download`, `/app-source`,
`/app`, `/invite`, `/auth` and `/setup` are answered 404 there, never redirected or
passed on to any Plexbie.

Add `?demo` to any dev URL for demo mode: every title, author and blurb is made up and
covers are drawn in code, so recordings show no real artwork. `demo-gifs.mjs` prints the
ffmpeg command that turns each scene's frames into a GIF for `docs/demo/`.

Sample data lives in `src/api/sample.ts` and is only used by the dev server;
the production build talks to the bot's `/api`.
