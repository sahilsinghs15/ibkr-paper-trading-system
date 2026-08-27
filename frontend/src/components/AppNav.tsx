import { NavLink } from 'react-router-dom'
import { useActiveIbkrAccount } from '../hooks/useActiveIbkrAccount'

export function AppNav() {
  const activeAccount = useActiveIbkrAccount()
  const accountHome = activeAccount ? `/account/${activeAccount}` : '/accounts'

  return (
    <nav className="app-nav" aria-label="Main">
      <NavLink
        to="/accounts"
        className={({ isActive }) => (isActive ? 'on' : undefined)}
      >
        Accounts
      </NavLink>
      <NavLink
        to={accountHome}
        end
        className={({ isActive }) => (activeAccount && isActive ? 'on' : undefined)}
      >
        Positions
      </NavLink>
      <NavLink
        to={activeAccount ? `${accountHome}/settings` : '/accounts'}
        className={({ isActive }) => (activeAccount && isActive ? 'on' : undefined)}
      >
        Settings
      </NavLink>
      <NavLink
        to={activeAccount ? `${accountHome}/reconcile` : '/accounts'}
        className={({ isActive }) => (activeAccount && isActive ? 'on' : undefined)}
      >
        Reconcile
      </NavLink>
      <NavLink
        to={activeAccount ? `${accountHome}/system-monitor` : '/accounts'}
        className={({ isActive }) => (activeAccount && isActive ? 'on' : undefined)}
      >
        System Monitor
      </NavLink>
    </nav>
  )
}
