import react from '@vitejs/plugin-react'
import { cpSync, rmSync } from 'node:fs'
import { resolve } from 'node:path'
import { defineConfig } from 'vite'

// plexbie.com, the project's own site: a static build of src/project, served by
// Cloudflare (cloudflare/). The household site is the default build (vite.config.ts).
// It shares the household site's public/ (logo, icons), minus what only a household's
// Plexbie serves, plus its own robots.txt, sitemap and llms.txt (project/public).
const out = resolve(__dirname, 'dist-project')
export default defineConfig({
  root: resolve(__dirname, 'project'),
  publicDir: resolve(__dirname, 'public'),
  plugins: [
    react(),
    {
      name: 'plexbie-project-files',
      closeBundle() {
        for (const only of ['sw.js', 'manifest.webmanifest']) rmSync(resolve(out, only), { force: true })
        cpSync(resolve(__dirname, 'project/public'), out, { recursive: true })
      },
    },
  ],
  build: { outDir: out, emptyOutDir: true },
})
