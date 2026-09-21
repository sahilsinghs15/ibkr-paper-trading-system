import { expect, test, type Page } from '@playwright/test'

const EMAIL = process.env.E2E_EMAIL || 'audit-test@zanrad.com'
const PASSWORD = process.env.E2E_PASSWORD || 'AuditTest!2026'
const ACCOUNT = process.env.E2E_ACCOUNT || 'DU20202'

async function login(page: Page): Promise<void> {
  await page.goto('/login')
  await page.locator('input[type="email"]').fill(EMAIL)
  await page.locator('input[type="password"]').fill(PASSWORD)
  await page.locator('button[type="submit"]').click()
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 })
}

/**
 * Regression: an emergency flatten returns 202 Accepted while the broker work
 * is still in flight, and the SSE publisher only emits POSITION_CLOSED on its
 * next poll. The dashboard previously had no resync trigger at all, so flattened
 * positions lingered as "ghosts" until the operator reloaded the page.
 */
test('flatten re-pulls the positions snapshot without a page reload', async ({ page }) => {
  let positionsRequests = 0
  let flattened = false

  // Serve one open position until the flatten is accepted, then none - the same
  // transition the backend makes once the broker sweep lands.
  await page.route('**/demo/positions', async (route) => {
    positionsRequests += 1
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        positions: flattened
          ? []
          : [
              {
                account_id: 202,
                trade_id: 'T-GHOST-1',
                symbol: 'AAPL',
                status: 'OPEN',
                quantity: 100,
                opened_at: new Date().toISOString(),
              },
            ],
      }),
    })
  })
  await page.route('**/demo/closed-positions', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '{"closed_positions":[]}' }),
  )
  // Leave the SSE request hanging. An immediately-closed stream would make
  // EventSource fire onerror, and the hook's reconnect path reloads the
  // snapshot on its own - which would pass this test even without the fix.
  await page.route('**/demo/stream*', () => new Promise(() => {}))
  await page.route('**/config/accounts/*/square-off*', async (route) => {
    flattened = true
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        account_id: 202,
        ibkr_account: ACCOUNT,
        squared_off_count: 1,
        trade_ids: ['T-GHOST-1'],
        operation_id: '00000000-0000-0000-0000-000000000001',
        status: 'ACTIVATING',
        scope: 'ENGINE_POSITION_FLATTEN',
        error: null,
      }),
    })
  })

  await login(page)
  await page.goto(`/account/${ACCOUNT}/settings`)

  await page.getByRole('button', { name: /SQUARE OFF ALL POSITIONS/i }).click()
  await page.getByRole('button', { name: 'Flatten Signal Positions' }).click()

  const before = positionsRequests
  await page.getByRole('button', { name: 'CONFIRM FLATTEN SIGNAL POSITIONS' }).click()

  // The fix schedules a short burst of snapshot re-pulls after the 202.
  await expect
    .poll(() => positionsRequests, {
      message: 'flatten did not trigger a positions resync',
      timeout: 15_000,
    })
    .toBeGreaterThan(before)

  // And it keeps re-pulling, so a slow broker sweep still converges.
  await expect
    .poll(() => positionsRequests, { timeout: 15_000 })
    .toBeGreaterThan(before + 1)
})
