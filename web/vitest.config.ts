import { defineConfig } from 'vitest/config'

// `npm test`. The tests talk to a stubbed fetch, never to a bot.
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts', 'test/**/*.test.ts'],
    environment: 'node',
    // Without this the client would serve sample data, as it does in `npm run dev`.
    env: { VITE_SAMPLE: '0' },
  },
})
