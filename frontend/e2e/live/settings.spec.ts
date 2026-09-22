import { expect, test } from '@playwright/test'
import { ACCOUNT, login, settle } from './fixtures'

/**
 * Settings changes end to end through the UI, including the audit row they must
 * produce (requirements 1, 2 and 7).
 *
 * Cancel Exposure is the subject: a boolean that applies to new signals only,
 * and is trivially reversible. The test always restores the original value.
 */
test.describe.configure({ mode: 'serial' })

/** The toggle is a `label.toggle-row` inside the CANCEL EXPOSURE panel. Match on
 *  the panel that contains both the heading and the toggle, innermost first. */
async function readToggle(page: import('@playwright/test').Page) {
  const panel = page
    .locator('div')
    .filter({ hasText: 'CANCEL EXPOSURE' })
    .filter({ has: page.locator('label.toggle-row') })
    .last()
  const box = panel.locator('label.toggle-row input[type="checkbox"]').last()
  await expect(box).toBeVisible({ timeout: 20_000 })
  return { panel, box, value: await box.isChecked() }
}

test('a settings change persists and is audited with before/after', async ({ page }) => {
  await login(page)
  await page.goto(`/account/${ACCOUNT}/settings`)
  await settle(page)

  const before = await readToggle(page)
  const original = before.value
  console.log(`Cancel Exposure starts: ${original ? 'ON' : 'OFF'}`)

  // --- flip it ---
  await before.box.click()
  await expect(before.box).toBeChecked({ checked: !original })

  const [saveRes] = await Promise.all([
    page.waitForResponse(
      (r) => /\/api\/v1\/config\/accounts\/\d+$/.test(r.url()) && r.request().method() === 'PATCH',
      { timeout: 20_000 },
    ),
    page.getByRole('button', { name: /Save Risk Controls/i }).click(),
  ])
  expect(saveRes.status(), 'save should succeed').toBe(200)
  const saved = await saveRes.json()
  expect(saved.cancel_exposure, 'server must reflect the new value').toBe(!original)

  // --- it must survive a reload, not just optimistic UI ---
  await page.reload()
  await settle(page)
  const afterReload = await readToggle(page)
  expect(afterReload.value, 'value did not persist across reload').toBe(!original)

  // --- and it must have produced an audit row with both states ---
  const auditRes = await page.request.get(
    '/api/v1/audit/events?category=SETTINGS&action=ACCOUNT_SETTINGS_UPDATED&limit=5',
    { headers: { Authorization: `Bearer ${await page.evaluate(() => localStorage.getItem('ibkr_trading_jwt_token'))}` } },
  )
  expect(auditRes.status()).toBe(200)
  const audit = await auditRes.json()
  expect(audit.total, 'settings change produced no audit row').toBeGreaterThan(0)

  const detailRes = await page.request.get(`/api/v1/audit/events/${audit.items[0].event_id}`, {
    headers: { Authorization: `Bearer ${await page.evaluate(() => localStorage.getItem('ibkr_trading_jwt_token'))}` },
  })
  const detail = await detailRes.json()
  // Requirement 1: authenticated identity on the action.
  expect(detail.actor_email, 'audit row missing actor').toBeTruthy()
  expect(detail.actor_role).toBeTruthy()
  expect(detail.client_ip, 'audit row missing client IP').toBeTruthy()
  // Requirement 2: previous and updated value.
  expect(detail.before_state?.cancel_exposure).toBe(original)
  expect(detail.after_state?.cancel_exposure).toBe(!original)

  // --- restore ---
  const restore = await readToggle(page)
  await restore.box.click()
  const [restoreRes] = await Promise.all([
    page.waitForResponse(
      (r) => /\/api\/v1\/config\/accounts\/\d+$/.test(r.url()) && r.request().method() === 'PATCH',
      { timeout: 20_000 },
    ),
    page.getByRole('button', { name: /Save Risk Controls/i }).click(),
  ])
  expect(restoreRes.status()).toBe(200)
  expect((await restoreRes.json()).cancel_exposure, 'failed to restore original value').toBe(original)
})
