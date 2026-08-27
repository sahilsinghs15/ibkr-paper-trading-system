import type { ReactNode } from 'react'
import { Navigate, useLocation, useParams } from 'react-router-dom'
import { useActiveIbkrAccount } from '../hooks/useActiveIbkrAccount'
import { normalizeIbkrAccount } from '../utils/activeAccount'

/** Replace /account/Unknown (and other empty ids) with a real account, or /accounts. */
export function AccountScoped({ children }: { children: ReactNode }) {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const location = useLocation()
  const resolved = useActiveIbkrAccount()
  const fromUrl = normalizeIbkrAccount(ibkrAccount)

  if (!fromUrl) {
    if (!resolved) return <Navigate to="/accounts" replace />
    const rest = location.pathname.replace(/^\/account\/[^/]+/i, '')
    return <Navigate to={`/account/${resolved}${rest}${location.search}`} replace />
  }

  return children
}
