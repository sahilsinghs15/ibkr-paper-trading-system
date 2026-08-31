import type { ReactNode } from 'react'
import { Navigate, useParams } from 'react-router-dom'
import { useAuthStore } from '../store/authStore'

interface ProtectedRouteProps {
  children: ReactNode
  requireAdmin?: boolean
}

export function ProtectedRoute({ children, requireAdmin = false }: ProtectedRouteProps) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const user = useAuthStore((s) => s.user)
  const params = useParams<{ ibkrAccount?: string }>()

  if (!isAuthenticated || !user) {
    return <Navigate to="/login" replace />
  }

  if (requireAdmin && user.role !== 'admin') {
    const fallback = user.ibkr_account ? `/account/${user.ibkr_account}` : '/accounts'
    return <Navigate to={fallback} replace />
  }

  // Cross-account protection for normal user role
  if (user.role === 'user' && params.ibkrAccount && user.ibkr_account) {
    if (params.ibkrAccount.toUpperCase() !== user.ibkr_account.toUpperCase()) {
      return <Navigate to={`/account/${user.ibkr_account}`} replace />
    }
  }

  return <>{children}</>
}
