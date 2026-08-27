import { Navigate } from 'react-router-dom'
import { useActiveIbkrAccount } from '../hooks/useActiveIbkrAccount'

/** Legacy /settings and /system-monitor URLs → current account, not a hardcoded id. */
export function RedirectToAccount({ suffix = '' }: { suffix?: string }) {
  const account = useActiveIbkrAccount()
  if (!account) return <Navigate to="/accounts" replace />
  return <Navigate to={`/account/${account}${suffix}`} replace />
}
