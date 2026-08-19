import { NavLink, useLocation, matchPath } from 'react-router-dom'

export function AppNav() {
  const location = useLocation()
  const accountMatch =
    matchPath('/account/:ibkrAccount/*', location.pathname) ||
    matchPath('/account/:ibkrAccount', location.pathname)
  const activeAccount = accountMatch?.params?.ibkrAccount || 'DUR919062'

  return (
    <nav className="app-nav" aria-label="Main">
      <NavLink
        to="/accounts"
        className={({ isActive }) => (isActive ? 'on' : undefined)}
      >
        Accounts
      </NavLink>
      <NavLink
        to={`/account/${activeAccount}`}
        end
        className={({ isActive }) => (isActive ? 'on' : undefined)}
      >
        Positions
      </NavLink>
      <NavLink
        to={`/account/${activeAccount}/settings`}
        className={({ isActive }) => (isActive ? 'on' : undefined)}
      >
        Settings
      </NavLink>
    </nav>
  )
}
