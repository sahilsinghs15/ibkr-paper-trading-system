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

test.describe('operator audit trail', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
    await page.goto(`/account/${ACCOUNT}/audit-logs`)
    await expect(page.getByRole('heading', { name: /Audit & Accountability/i })).toBeVisible()
  })

  test('lists events with actor identity and client IP', async ({ page }) => {
    const res = await page.waitForResponse(
      (r) => r.url().includes('/api/v1/audit/events') && r.status() === 200,
    )
    const body = await res.json()
    expect(body.total).toBeGreaterThan(0)

    // Requirement 1: every row carries authenticated identity + IP.
    for (const item of body.items) {
      expect(item.actor_email, `event ${item.action} lost its actor`).toBeTruthy()
      expect(item.actor_role).toBeTruthy()
      expect(item.client_ip).toBeTruthy()
    }
  })

  // The filter panel is a <form>; nothing is applied until Search is pressed.
  async function search(page: Page, match: string) {
    const [res] = await Promise.all([
      page.waitForResponse(
        (r) => r.url().includes('/api/v1/audit/events?') && r.url().includes(match) && r.status() === 200,
        { timeout: 20_000 },
      ),
      page.getByRole('button', { name: 'Search' }).click(),
    ])
    return res.json()
  }

  test('category filter narrows results to one category', async ({ page }) => {
    await page.locator('.audit-field', { hasText: 'Category' }).locator('select').selectOption('SETTINGS')
    const body = await search(page, 'category=SETTINGS')
    expect(body.total).toBeGreaterThan(0)
    expect(body.items.every((i: { category: string }) => i.category === 'SETTINGS')).toBe(true)
  })

  test('IP / CIDR filter is applied server-side', async ({ page }) => {
    await page.getByRole('button', { name: /Advanced \/ Security filters/i }).click()
    await page.getByPlaceholder('203.0.113.7 or 10.0.0.0/8').fill('203.0.113.0/24')
    const body = await search(page, 'ip=')
    expect(body.total).toBeGreaterThan(0)
    expect(
      body.items.every((i: { client_ip: string }) => i.client_ip.startsWith('203.0.113.')),
    ).toBe(true)
  })

  test('actor e-mail filter is applied server-side', async ({ page }) => {
    await page.getByPlaceholder('E-mail or part of it').fill(EMAIL)
    const body = await search(page, 'actor=')
    expect(body.total).toBeGreaterThan(0)
    expect(body.items.every((i: { actor_email: string }) => i.actor_email === EMAIL)).toBe(true)
  })

  test('all seven operator categories are offered as filters', async ({ page }) => {
    const token = await page.evaluate(() => localStorage.getItem('ibkr_trading_jwt_token'))
    expect(token).toBeTruthy()
    const res = await page.request.get('/api/v1/audit/facets', {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(res.ok()).toBe(true)
    const body = await res.json()
    const categories = body.categories.map((c: { category: string }) => c.category)
    // Requirement 7: the categories the operator brief called for.
    for (const expected of [
      'SETTINGS',
      'INVENTORY',
      'MANUAL_TRADING',
      'POSITIONS',
      'EMERGENCY',
      'SYSTEM_CONTROL',
    ]) {
      expect(categories).toContain(expected)
    }
  })
})
