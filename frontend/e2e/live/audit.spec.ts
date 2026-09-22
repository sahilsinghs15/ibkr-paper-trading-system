import { expect, test } from '@playwright/test'
import { ACCOUNT, login, settle } from './fixtures'

const token = (page: import('@playwright/test').Page) =>
  page.evaluate(() => localStorage.getItem('ibkr_trading_jwt_token'))

async function api(page: import('@playwright/test').Page, path: string) {
  const res = await page.request.get(path, {
    headers: { Authorization: `Bearer ${await token(page)}` },
  })
  return { status: res.status(), body: res.ok() ? await res.json() : await res.text() }
}

test.describe('audit trail', () => {
  test.beforeEach(async ({ page }) => {
    await login(page)
    await page.goto(`/account/${ACCOUNT}/audit-logs`)
    await settle(page)
  })

  test('page renders events with identity and source', async ({ page }) => {
    const { status, body } = await api(page, '/api/v1/audit/events?limit=20')
    expect(status).toBe(200)
    expect(body.total, 'audit trail is empty').toBeGreaterThan(0)
    for (const item of body.items) {
      expect(item.client_ip, `${item.action} missing client IP`).toBeTruthy()
      // Only an unauthenticated event (a failed login) may lack an actor.
      if (item.actor_type !== 'ANONYMOUS') {
        expect(item.actor_email, `${item.action} missing actor`).toBeTruthy()
        expect(item.actor_role, `${item.action} missing role`).toBeTruthy()
      }
    }
  })

  test('settings changes are recorded under the Settings category', async ({ page }) => {
    const { body } = await api(page, '/api/v1/audit/events?category=SETTINGS&limit=20')
    expect(body.total, 'no SETTINGS audit rows').toBeGreaterThan(0)
    expect(body.items.every((i: { category: string }) => i.category === 'SETTINGS')).toBe(true)
  })

  test('multi-select action ORs within the field', async ({ page }) => {
    const single = await api(page, '/api/v1/audit/events?action=LOGIN_SUCCEEDED&limit=50')
    const multi = await api(
      page,
      '/api/v1/audit/events?action=LOGIN_SUCCEEDED&action=ACCOUNT_SETTINGS_UPDATED&limit=50',
    )
    expect(multi.body.total, 'adding an action must never reduce the result set').toBeGreaterThanOrEqual(
      single.body.total,
    )
  })

  test('multi-select result ORs within the field', async ({ page }) => {
    const one = await api(page, '/api/v1/audit/events?result=SUCCEEDED&limit=50')
    const two = await api(page, '/api/v1/audit/events?result=SUCCEEDED&result=DENIED&limit=50')
    expect(two.body.total).toBeGreaterThanOrEqual(one.body.total)
  })

  test('empty search explains which filter is responsible', async ({ page }) => {
    const { body } = await api(page, '/api/v1/audit/events?result=PENDING&limit=50')
    expect(body.total).toBe(0)
    const hints = body.empty_filter_hints ?? []
    const culprit = hints.find((h: { field: string }) => h.field === 'results')
    expect(culprit, 'no diagnostic for the blocking filter').toBeTruthy()
    expect(culprit.matches).toBe(0)
  })

  test('invalid filters are rejected rather than silently ignored', async ({ page }) => {
    for (const q of ['category=BOGUS', 'ip=not-an-ip', 'action=NOPE']) {
      const { status } = await api(page, `/api/v1/audit/events?${q}`)
      expect(status, `${q} should be rejected`).toBe(422)
    }
  })

  test('event detail exposes before/after and investigation context', async ({ page }) => {
    const list = await api(page, '/api/v1/audit/events?category=SETTINGS&limit=1')
    test.skip(list.body.total === 0, 'no settings events to inspect')
    const { status, body } = await api(page, `/api/v1/audit/events/${list.body.items[0].event_id}`)
    expect(status).toBe(200)
    expect(body.before_state, 'detail missing before_state').toBeTruthy()
    expect(body.after_state, 'detail missing after_state').toBeTruthy()
    expect(Array.isArray(body.changes), 'detail missing computed changes').toBe(true)
  })
})
