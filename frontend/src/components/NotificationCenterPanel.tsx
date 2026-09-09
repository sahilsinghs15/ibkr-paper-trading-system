import React, { useEffect, useRef, useState } from 'react'
import {
  fetchNotificationFeed,
  markAllNotificationsAsRead,
  markNotificationAsRead,
} from '../api/systemEventsApi'
import { useNotificationStore } from '../store/notificationStore'
import type { NotificationItem } from '../types/systemEvent'

function formatNotificationTime(isoStr: string | null): string {
  if (!isoStr) return ''
  try {
    const d = new Date(isoStr)
    const now = new Date()
    const isToday =
      d.getDate() === now.getDate() &&
      d.getMonth() === now.getMonth() &&
      d.getFullYear() === now.getFullYear()

    if (isToday) {
      return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    }

    const yesterday = new Date(now)
    yesterday.setDate(yesterday.getDate() - 1)
    const isYesterday =
      d.getDate() === yesterday.getDate() &&
      d.getMonth() === yesterday.getMonth() &&
      d.getFullYear() === yesterday.getFullYear()

    if (isYesterday) {
      return 'Yesterday'
    }

    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  } catch {
    return ''
  }
}

export const NotificationCenterPanel: React.FC = () => {
  const isPanelOpen = useNotificationStore((s) => s.isPanelOpen)
  const closePanel = useNotificationStore((s) => s.closePanel)
  const notifications = useNotificationStore((s) => s.notifications)
  const unreadCount = useNotificationStore((s) => s.unreadCount)
  const totalCount = useNotificationStore((s) => s.totalCount)
  const setFeed = useNotificationStore((s) => s.setFeed)
  const appendFeed = useNotificationStore((s) => s.appendFeed)
  const markReadOptimistic = useNotificationStore((s) => s.markReadOptimistic)
  const markAllReadOptimistic = useNotificationStore((s) => s.markAllReadOptimistic)

  const [expandedId, setExpandedId] = useState<number | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const panelRef = useRef<HTMLDivElement>(null)

  // Load notifications feed on open
  useEffect(() => {
    if (!isPanelOpen) return

    let active = true
    async function load() {
      setLoading(true)
      const feed = await fetchNotificationFeed(30, 0)
      if (active) {
        setFeed(feed)
        setLoading(false)
      }
    }

    void load()

    return () => {
      active = false
    }
  }, [isPanelOpen, setFeed])

  // Close on outside click
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (
        panelRef.current &&
        !panelRef.current.contains(e.target as Node) &&
        !(e.target as HTMLElement).closest('.notification-bell-btn')
      ) {
        closePanel()
      }
    }

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        closePanel()
      }
    }

    if (isPanelOpen) {
      document.addEventListener('mousedown', handleClickOutside)
      document.addEventListener('keydown', handleKeyDown)
    }

    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [isPanelOpen, closePanel])

  if (!isPanelOpen) return null

  async function handleMarkRead(item: NotificationItem, e: React.MouseEvent) {
    e.stopPropagation()
    if (item.is_read) return
    markReadOptimistic(item.id)
    await markNotificationAsRead(item.id)
  }

  async function handleMarkAllRead() {
    if (unreadCount === 0) return
    markAllReadOptimistic()
    await markAllNotificationsAsRead()
  }

  async function handleLoadMore() {
    if (loadingMore || notifications.length >= totalCount) return
    setLoadingMore(true)
    const feed = await fetchNotificationFeed(30, notifications.length)
    appendFeed(feed)
    setLoadingMore(false)
  }

  function toggleExpand(id: number) {
    setExpandedId((prev) => (prev === id ? null : id))
  }

  return (
    <div className="notification-panel" ref={panelRef} role="dialog" aria-label="Notification Center">
      {/* Header */}
      <div className="notification-panel-header">
        <div className="notification-panel-title-row">
          <span className="notification-panel-title">Notifications</span>
          {unreadCount > 0 && (
            <span className="notification-unread-pill">{unreadCount} unread</span>
          )}
        </div>
        <button
          type="button"
          className="notification-mark-all-btn"
          onClick={handleMarkAllRead}
          disabled={unreadCount === 0}
        >
          Mark all read
        </button>
      </div>

      {/* Body List */}
      <div className="notification-panel-body">
        {loading && notifications.length === 0 ? (
          <div className="notification-loading">Loading notifications...</div>
        ) : notifications.length === 0 ? (
          <div className="notification-empty">
            <span className="notification-empty-icon">🔔</span>
            <p>No notifications yet</p>
          </div>
        ) : (
          <div className="notification-items-list">
            {notifications.map((item) => {
              const isExpanded = expandedId === item.id
              return (
                <div
                  key={item.id}
                  className={`notification-item-card ${item.is_read ? 'read' : 'unread'}`}
                  onClick={() => toggleExpand(item.id)}
                >
                  <div className="notification-item-main">
                    <span className="notification-item-icon" aria-hidden="true">
                      {item.icon}
                    </span>
                    <div className="notification-item-content">
                      <div className="notification-item-header-line">
                        <span className="notification-item-title">{item.title}</span>
                        {!item.is_read && <span className="notification-new-badge">NEW</span>}
                      </div>
                      <div className="notification-item-meta">
                        <span className="notification-item-time">
                          {formatNotificationTime(item.ts)}
                        </span>
                        {!item.is_read && (
                          <button
                            type="button"
                            className="notification-mark-single-btn"
                            onClick={(e) => handleMarkRead(item, e)}
                            title="Mark as read"
                          >
                            Mark read
                          </button>
                        )}
                      </div>
                    </div>
                  </div>

                  {/* Technical Detail Accordion */}
                  {isExpanded && (
                    <div
                      className="notification-technical-details"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <div className="technical-detail-row">
                        <span className="technical-label">Event ID:</span>
                        <span className="technical-value mono">{item.id}</span>
                      </div>
                      {item.unit && (
                        <div className="technical-detail-row">
                          <span className="technical-label">Unit:</span>
                          <span className="technical-value mono">{item.unit}</span>
                        </div>
                      )}
                      {item.service && (
                        <div className="technical-detail-row">
                          <span className="technical-label">Service:</span>
                          <span className="technical-value mono">{item.service}</span>
                        </div>
                      )}
                      {item.kind && (
                        <div className="technical-detail-row">
                          <span className="technical-label">Kind:</span>
                          <span className="technical-value mono">{item.kind}</span>
                        </div>
                      )}
                      {item.ts && (
                        <div className="technical-detail-row">
                          <span className="technical-label">Timestamp:</span>
                          <span className="technical-value mono">{item.ts}</span>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}

            {notifications.length < totalCount && (
              <div className="notification-load-more-wrapper">
                <button
                  type="button"
                  className="notification-load-more-btn"
                  onClick={handleLoadMore}
                  disabled={loadingMore}
                >
                  {loadingMore ? 'Loading...' : 'Load older notifications'}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
