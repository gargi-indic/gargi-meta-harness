import { defineConfig, devices } from '@playwright/test';

const PORT = 8799;
const TOKEN = 'smoke';

export default defineConfig({
  testDir: './e2e',
  // One demo server (and one scripted run) is shared by every scenario, so run them in order.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `uv run harness serve --demo --no-open --port ${PORT}`,
    cwd: '..',
    // /auth sets the session cookie and redirects; any non-5xx answer means the server is up.
    url: `http://127.0.0.1:${PORT}/auth?token=${TOKEN}`,
    env: { HARNESS_DEMO_TOKEN: TOKEN },
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
