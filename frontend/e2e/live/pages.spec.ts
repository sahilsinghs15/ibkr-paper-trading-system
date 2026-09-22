import { expect, test } from '@playwright/test'
import { ACCOUNT, login, settle, summarize, watchForProblems } from './fixtures'

/**
 * Every route an operator can reach, loaded against the deployed dashboard.
 * Each page must render, reach its APIs, and raise no uncaught error.
 */
const ROUTES: { name: string; path: string; expect: RegExp }[] = [
  { name: 'Accounts', path: '/accounts', expect: /account/i },
  { name: 'Dashboard', path: `/account/${ACCOUNT}`, expect: /position|pnl|signal/i },
  { name: 'Settings', path: `/account/${ACCOUNT}/settings`, expect: /setting|margin|allocation|risk/i },
  { name: 'System Monitor', path: `/account/${ACCOUNT}/system-monitor`, expect: /monitor|service|system/i },
  { name: 'Audit Logs', path: `/account/${ACCOUNT}/audit-logs`, expect: /audit/i },
  { name: 'Ingest Feed', path: `/account/${ACCOUNT}/ingest`, expect: /ingest|signal|job/i },
  { name: 'Reconciliation', path: `/account/${ACCOUNT}/reconcile`, expect: /reconcil|inventory|broker/i },
  { name: 'Trade Book', path: `/account/${ACCOUNT}/trade-book`, expect: /trade/i },
  { name: 'Order Book', path: `/account/${ACCOUNT}/order-book`, expect: /order/i },
  { name: 'Manual Trading', path: `/account/${ACCOUNT}/manual-trade`, expect: /manual/i },
]

test.describe('every dashboard page loads cleanly', () => {
  for (const route of ROUTES) {
    test(route.name, async ({ page }) => {
      // Watch only after login: the login page itself fires four authenticated
      // /demo/* requests that 401 before any token exists, which is its own
      // finding and would otherwise drown every page in the same four errors.
      await login(page)
      const problems = watchForProblems(page)
      await page.goto(route.path)
      await settle(page)

      // The page rendered its own content, not a blank shell or an error boundary.
      const body = (await page.locator('#root').innerText()).slice(0, 4000)
      expect(body.length, `${route.name} rendered an empty page`).toBeGreaterThan(40)
      expect(body, `${route.name} shows an error boundary`).not.toMatch(
        /something went wrong|unexpected error|failed to load/i,
      )
      expect(body, `${route.name} content not recognised`).toMatch(route.expect)

      if (problems.tolerated.length > 0) {
        console.log(`  ${route.name}: tolerated — ${[...new Set(problems.tolerated)].join('; ')}`)
      }
      const found = summarize(problems)
      expect(found, `${route.name} reported problems:\n${found}`).toBe('')
    })
  }
})
