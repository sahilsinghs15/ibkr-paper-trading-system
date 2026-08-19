import { NavLink } from 'react-router-dom'

export function AppNav() {
  return (
    <nav className="app-nav" aria-label="Main">
      <NavLink to="/" end className={({ isActive }) => (isActive ? 'on' : undefined)}>
        Positions
      </NavLink>
      <NavLink to="/settings" className={({ isActive }) => (isActive ? 'on' : undefined)}>
        Settings
      </NavLink>
    </nav>
  )
}
