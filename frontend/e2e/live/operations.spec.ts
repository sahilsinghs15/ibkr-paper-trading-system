import { expect, test } from '@playwright/test'
import { ACCOUNT, login, settle } from './fixtures'

const token = (page: import('@playwright/test').Page) =>
  page.evaluate(() => localStorage.getItem('ibkr_trading_jwt_token'))

async function api(page: import('@playwright/test').Page, path: string) {
  const res = await page.request.get(path, {
    headers: { Authorization: `Bearer ${await token(page)}` },
  })
  return { status: res.status(), ok: res.ok(), body: res.ok() ? await res.json() : await res.text() }
}

test.describe('reconciliation', () => {
  test('inventory loads and reports broker vs ledger', async ({ page }) => {
    await login(page)
    await page.goto(`/account/${ACCOUNT}/reconcile`)
    await settle(page)

    const text = await page.locator('#root').innerText()
    expect(text).toMatch(/inventory|reconcil|broker|ledger/i)
    expect(text, 'reconciliation page shows an error').not.toMatch(
      /something went wrong|failed to load/i,
    )
  })

  test('reconcile API returns a well-formed snapshot', async ({ page }) => {
    await login(page)
    const { status, body } = await api(page, `/api/v1/reconcile/positions?ibkr_account=${ACCOUNT}`)
    // With the gateway down a broker snapshot may be unavailable; either a
    // payload or an explicit 503 is acceptable, a 500 is not.
    expect([200, 503], `unexpected status ${status}`).toContain(status)
    if (status === 200) {
      expect(body).toBeTruthy()
      console.log('reconcile keys:', Object.keys(body).join(', '))
    }
  })
})

test.describe('order book and trade book', () => {
  for (const [name, path] of [
    ['Order Book', 'order-book'],
    ['Trade Book', 'trade-book'],
  ] as const) {
    test(`${name} renders`, async ({ page }) => {
      await login(page)
      await page.goto(`/account/${ACCOUNT}/${path}`)
      await settle(page)
      const text = await page.locator('#root').innerText()
      expect(text.length).toBeGreaterThan(40)
      expect(text, `${name} shows an error`).not.toMatch(/something went wrong|failed to load/i)
    })
  }
})

test.describe('manual trading', () => {
  test('page renders and exposes the ticket', async ({ page }) => {
    await login(page)
    await page.goto(`/account/${ACCOUNT}/manual-trade`)
    await settle(page)
    const text = await page.locator('#root').innerText()
    expect(text).toMatch(/manual/i)
    expect(text, 'manual trading shows an error').not.toMatch(
      /something went wrong|failed to load/i,
    )
  })

  test('order submission is refused while the broker is unreachable', async ({ page }) => {
    await login(page)
    // Deliberately malformed: this must be rejected by validation, never reach
    // the broker, and never 500. The gateway is stopped outside trading hours,
    // so this also proves the path fails closed rather than hanging.
    const res = await page.request.post('/api/v1/manual/orders?ibkr_account=' + ACCOUNT, {
      headers: {
        Authorization: `Bearer ${await token(page)}`,
        'Content-Type': 'application/json',
      },
      data: { account_id: 0 },
    })
    expect(
      res.status(),
      `expected a 4xx/503 refusal, got ${res.status()}`,
    ).toBeGreaterThanOrEqual(400)
    expect(res.status(), 'must not be an unhandled server error').not.toBe(500)
    console.log(`manual order refusal: HTTP ${res.status()}`)
  })
})

test.describe('system monitor', () => {
  test('reports service state without crashing', async ({ page }) => {
    await login(page)
    await page.goto(`/account/${ACCOUNT}/system-monitor`)
    await settle(page)
    const text = await page.locator('#root').innerText()
    expect(text).toMatch(/service|system|monitor/i)
    expect(text, 'system monitor shows an error').not.toMatch(/something went wrong/i)
  })
})
