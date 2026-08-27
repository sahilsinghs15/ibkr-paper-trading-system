import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useLocation } from 'react-router-dom'
import { fetchAccountsConfig } from '../api/configApi'
import {
  ibkrAccountFromPath,
  pickDefaultIbkrAccount,
  readLastIbkrAccount,
  writeLastIbkrAccount,
} from '../utils/activeAccount'

/**
 * Account for Positions / Settings / Reconcile / System Monitor links.
 * URL param wins, then last visited account, then the only enabled account.
 * Never returns the placeholder "Unknown".
 */
export function useActiveIbkrAccount(): string {
  const location = useLocation()
  const fromPath = ibkrAccountFromPath(location.pathname)
  const { data } = useQuery({
    queryKey: ['config', 'accounts'],
    queryFn: fetchAccountsConfig,
  })

  const resolved = useMemo(() => {
    if (fromPath) return fromPath
    const last = readLastIbkrAccount()
    const accounts = data?.accounts || []
    if (accounts.length > 0) {
      return pickDefaultIbkrAccount(accounts, last)
    }
    return last
  }, [fromPath, data])

  useEffect(() => {
    if (resolved) writeLastIbkrAccount(resolved)
  }, [resolved])

  return resolved
}
