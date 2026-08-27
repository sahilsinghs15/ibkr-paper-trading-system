/** Persist and normalize the IBKR account used by account-scoped dashboard routes. */

export const LAST_ACCOUNT_KEY = 'modelBlue.lastIbkrAccount'

export function normalizeIbkrAccount(value: string | null | undefined): string {
  const clean = String(value || '').trim().toUpperCase()
  if (!clean || clean === 'UNKNOWN') return ''
  return clean
}

export function ibkrAccountFromPath(pathname: string): string {
  const match = pathname.match(/^\/account\/([^/]+)/i)
  return normalizeIbkrAccount(match?.[1])
}

export function readLastIbkrAccount(): string {
  try {
    return normalizeIbkrAccount(localStorage.getItem(LAST_ACCOUNT_KEY))
  } catch {
    return ''
  }
}

export function writeLastIbkrAccount(account: string): void {
  const clean = normalizeIbkrAccount(account)
  if (!clean) return
  try {
    localStorage.setItem(LAST_ACCOUNT_KEY, clean)
  } catch {
    /* ignore quota / private mode */
  }
}

export function pickDefaultIbkrAccount(
  accounts: Array<{ ibkr_account?: string | null; enabled?: boolean }>,
  last: string,
): string {
  const ids = accounts
    .map((row) => normalizeIbkrAccount(row.ibkr_account))
    .filter(Boolean)
  const idSet = new Set(ids)
  if (last && idSet.has(last)) return last

  const enabled = accounts
    .filter((row) => row.enabled)
    .map((row) => normalizeIbkrAccount(row.ibkr_account))
    .filter(Boolean)
  if (enabled.length === 1) return enabled[0]
  if (ids.length === 1) return ids[0]
  return ''
}
