import React from 'react'
import { useNotificationStore } from '../store/notificationStore'

export const NotificationBell: React.FC = () => {
  const unreadCount = useNotificationStore((s) => s.unreadCount)
  const isPanelOpen = useNotificationStore((s) => s.isPanelOpen)
  const togglePanel = useNotificationStore((s) => s.togglePanel)

  const badgeText = unreadCount > 99 ? '99+' : unreadCount > 0 ? String(unreadCount) : null

  return (
    <div className="notification-bell-wrapper">
      <button
        type="button"
        className={`notification-bell-btn ${isPanelOpen ? 'active' : ''}`}
        onClick={togglePanel}
        aria-label={`Notifications (${unreadCount} unread)`}
        title={unreadCount > 0 ? `${unreadCount} unread notification(s)` : 'Notifications'}
      >
        <svg
          className="notification-bell-icon"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          width="18"
          height="18"
        >
          <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 0 1-3.46 0" />
        </svg>
        {badgeText && <span className="notification-bell-badge">{badgeText}</span>}
      </button>
    </div>
  )
}
