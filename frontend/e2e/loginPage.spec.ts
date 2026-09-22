import { expect, test } from '@playwright/test'

/**
 * The login page must not call authenticated endpoints.
 *
 * `usePnlStream` and `useSystemEvents` were mounted unconditionally in App, so
 * visiting /login fired /demo/positions, /demo/closed-positions,
 * /demo/notifications and /demo/stream with no token — four guaranteed 401s per
 * session. That failing notification fetch is also what previously armed the
 * event poller with an un-bootstrapped cursor, replaying historical events as
 * toasts once the user did log in.
 */
test('no authenticated requests are made before login', async ({ page }) => {
  const authed: string[] = []
  page.on('request', (req) => {
    const path = new URL(req.url()).pathname
    if (path.startsWith('/demo/') || path.startsWith('/api/v1/')) {
      // The login POST itself is expected once credentials are submitted.
      if (path === '/api/v1/auth/login') return
      authed.push(`${req.method()} ${path}`)
    }
  })

  await page.goto('/login')
  await expect(page.locator('input[type="email"]')).toBeVisible()
  // Well past the poll interval and the SSE connect attempt.
  await page.waitForTimeout(6000)

  expect(
    [...new Set(authed)],
    'login page called authenticated endpoints before any token existed',
  ).toEqual([])
})
