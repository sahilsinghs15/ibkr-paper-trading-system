import { NavLink } from 'react-router-dom'
import { useActiveIbkrAccount } from '../hooks/useActiveIbkrAccount'
import { useAuthStore } from '../store/authStore'

export function AppNav() {
  const activeAccount = useActiveIbkrAccount()
  const user = useAuthStore((s) => s.user)

  const effectiveAccount = user?.role === 'user' && user.ibkr_account ? user.ibkr_account : activeAccount
  const accountHome = effectiveAccount ? `/account/${effectiveAccount}` : '/accounts'

  return (
    <nav className="app-nav" aria-label="Main">
      {user?.role === 'admin' && (
        <NavLink
          to="/accounts"
          className={({ isActive }) => (isActive ? 'on' : undefined)}
        >
          Accounts
        </NavLink>
      )}
      <NavLink
        to={accountHome}
        end
        className={({ isActive }) => (effectiveAccount && isActive ? 'on' : undefined)}
      >
        Positions
      </NavLink>
      <NavLink
        to={effectiveAccount ? `${accountHome}/settings` : '/accounts'}
        className={({ isActive }) => (effectiveAccount && isActive ? 'on' : undefined)}
      >
        Settings
      </NavLink>
      <NavLink
        to={effectiveAccount ? `${accountHome}/reconcile` : '/accounts'}
        className={({ isActive }) => (effectiveAccount && isActive ? 'on' : undefined)}
      >
        Reconcile
      </NavLink>
      {user?.role === 'admin' && (
        <NavLink
          to={effectiveAccount ? `${accountHome}/system-monitor` : '/accounts'}
          className={({ isActive }) => (effectiveAccount && isActive ? 'on' : undefined)}
        >
          System Monitor
        </NavLink>
      )}
    </nav>
  )
}
