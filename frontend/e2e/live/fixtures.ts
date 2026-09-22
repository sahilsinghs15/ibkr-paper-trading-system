import { expect, type Page, type Request, type Response } from '@playwright/test'

export const EMAIL = process.env.E2E_EMAIL ?? 'admin@zanrad.com'
export const PASSWORD = process.env.E2E_PASSWORD ?? ''
export const ACCOUNT = process.env.E2E_ACCOUNT ?? 'DUR919062'

export interface PageProblems {
  consoleErrors: string[]
  pageErrors: string[]
  failedRequests: string[]
  /** Known-correct failures, reported but not treated as defects. */
  tolerated: string[]
}

/** Attach collectors for anything that would show an operator a broken page. */
export function watchForProblems(page: Page): PageProblems {
  const p: PageProblems = { consoleErrors: [], pageErrors: [], failedRequests: [], tolerated: [] }

  page.on('console', (msg) => {
    if (msg.type() !== 'error') return
    const text = msg.text()
    // The dashboard logs SSE reconnects as errors when a stream drops; that is
    // expected churn, not a defect.
    if (/EventSource|SSE|stream/i.test(text)) return
    // The browser logs a bare "Failed to load resource" for the tolerated
    // margin 503 with no URL, so it cannot be matched precisely -- suppress the
    // generic form and rely on the response handler above, which can.
    if (/Failed to load resource/i.test(text) && p.tolerated.length > 0) return
    p.consoleErrors.push(text)
  })
  page.on('pageerror', (err) => p.pageErrors.push(String(err)))
  page.on('requestfailed', (req: Request) => {
    const f = req.failure()?.errorText ?? ''
    if (/ERR_ABORTED/.test(f)) return // navigation cancelled an in-flight fetch
    p.failedRequests.push(`${req.method()} ${req.url()} — ${f}`)
  })
  page.on('response', (res: Response) => {
    if (res.status() < 400) return
    // `/margin/accounts/{acct}` answers 503 when TWS is unreachable, which is
    // correct: it refuses to serve a margin figure it cannot source rather than
    // returning a stale one. ibgateway.service is stopped outside trading hours
    // by the session timers, so this is the normal state for most of the day.
    // Narrow on purpose -- a 503 from anything else is still a failure, and this
    // endpoint should return 200 during a live session.
    if (res.status() === 503 && /\/api\/v1\/margin\/accounts\//.test(res.url())) {
      p.tolerated.push(`503 ${new URL(res.url()).pathname} (broker unreachable — expected while ibgateway is stopped)`)
      return
    }
    p.failedRequests.push(`${res.request().method()} ${res.url()} — HTTP ${res.status()}`)
  })
  return p
}

export function summarize(p: PageProblems): string {
  return [
    ...p.pageErrors.map((e) => `  [uncaught] ${e}`),
    ...p.consoleErrors.map((e) => `  [console] ${e}`),
    ...p.failedRequests.map((e) => `  [request] ${e}`),
  ].join('\n')
}

export async function login(page: Page): Promise<void> {
  expect(PASSWORD, 'E2E_PASSWORD must be set').not.toBe('')
  await page.goto('/login')
  await page.locator('input[type="email"]').fill(EMAIL)
  await page.locator('input[type="password"]').fill(PASSWORD)
  await page.locator('button[type="submit"]').click()
  await expect(page, 'login should navigate away from /login').not.toHaveURL(/\/login/, {
    timeout: 20_000,
  })
}

/** Wait for the app to settle: React mounted and initial fetches done. */
export async function settle(page: Page): Promise<void> {
  await expect(page.locator('#root')).not.toBeEmpty({ timeout: 20_000 })
  // Best-effort only, on a short budget. Several pages never go idle -- the
  // dashboard holds an SSE stream open and the Ingest Feed re-polls every 5s --
  // so a long timeout here just burns the test budget without adding certainty.
  await page.waitForLoadState('networkidle', { timeout: 3_000 }).catch(() => {})
  // Give any in-flight render a beat to paint after the last response.
  await page.waitForTimeout(500)
}
