import { defineConfig, devices } from '@playwright/test'

const PORT = 5173
// Point the suite at a deployed dashboard with E2E_BASE_URL; without it the
// tests run against a local dev server as usual.
const remoteBaseURL = process.env.E2E_BASE_URL
const baseURL = remoteBaseURL || `http://localhost:${PORT}`

export default defineConfig({
  testDir: './e2e',
  // e2e/live/* drives a deployed dashboard with real credentials. It only runs
  // when E2E_BASE_URL points at one, so the default local run stays hermetic.
  testIgnore: remoteBaseURL ? [] : ['**/live/**'],
  fullyParallel: true,
  // A stray `test.only` should fail the build, not silently skip the suite.
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: 'html',
  use: {
    baseURL,
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  // Never start a dev server when the target is a deployed host.
  ...(remoteBaseURL
    ? {}
    : {
        webServer: {
          command: 'npm run dev',
          url: baseURL,
          reuseExistingServer: !process.env.CI,
          timeout: 120_000,
        },
      }),
})
