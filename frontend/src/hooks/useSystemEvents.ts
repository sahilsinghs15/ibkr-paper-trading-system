import { useEffect, useRef } from 'react'
import { fetchNotificationFeed, fetchSystemEvents } from '../api/systemEventsApi'
import { useNotificationStore } from '../store/notificationStore'
import type {
  NotificationItem,
  SystemEventItem,
  ToastNotification,
} from '../types/systemEvent'

const STORAGE_KEY = 'zahnrad_last_seen_event_id'
const POLL_INTERVAL_MS = 5000
const RECENT_THRESHOLD_MS = 120_000 // 2 minutes

const CANONICAL_MESSAGES: Record<string, Record<string, string>> = {
  ibgateway: {
    SERVICE_STARTED: 'Broker connection started',
    SERVICE_STOPPED: 'Broker connection stopped',
  },
  'trading-backend': {
    SERVICE_STARTED: 'Trading Backend started',
    SERVICE_STOPPED: 'Trading Backend stopped',
  },
  'webhook-ingest': {
    SERVICE_STARTED: 'Market signal intake started',
    SERVICE_STOPPED: 'Market signal intake stopped',
  },
  'demo-streaming': {
    SERVICE_STARTED: 'Market data display started',
    SERVICE_STOPPED: 'Market data display stopped',
  },
}

function formatTime(isoStr: string | null): string {
  if (!isoStr) return ''
  try {
    const d = new Date(isoStr)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return ''
  }
}

function resolveCanonicalTitle(evt: SystemEventItem): {
  icon: string
  title: string
  message: string
} {
  if (evt.title && evt.message) {
    return {
      icon: evt.icon || (evt.kind === 'SERVICE_STOPPED' ? '🔴' : '🟢'),
      title: evt.title,
      message: evt.message,
    }
  }

  const detail = evt.detail || {}
  const svc = String(evt.service || detail.service || '')

  if (evt.kind === 'MARKET_CLOSED') {
    const msg = `Market closed — ${detail.reason || 'Weekend'}`
    return { icon: '📅', title: msg, message: msg }
  }

  if (CANONICAL_MESSAGES[svc]?.[evt.kind]) {
    const msg = CANONICAL_MESSAGES[svc][evt.kind]
    const icon = evt.kind === 'SERVICE_STOPPED' ? '🔴' : '🟢'
    return { icon, title: msg, message: msg }
  }

  const fallbackMsg = String(detail.message || `${svc || 'Service'} ${evt.kind}`)
  return {
    icon: String(detail.icon || 'ℹ️'),
    title: fallbackMsg,
    message: fallbackMsg,
  }
}

function eventToToast(evt: SystemEventItem): ToastNotification {
  const { icon, title, message } = resolveCanonicalTitle(evt)
  return {
    id: `evt-${evt.id}`,
    eventId: evt.id,
    kind: evt.kind,
    icon,
    title,
    message,
    timeStr: formatTime(evt.ts),
  }
}

function eventToNotificationItem(evt: SystemEventItem): NotificationItem {
  const { icon, title, message } = resolveCanonicalTitle(evt)
  return {
    id: evt.id,
    ts: evt.ts,
    kind: evt.kind,
    service: evt.service || evt.detail?.service,
    unit: evt.unit || evt.detail?.unit,
    friendly_name: evt.friendly_name || evt.detail?.friendly_name,
    title,
    message,
    icon,
    is_read: false,
    detail: evt.detail,
  }
}

export function useSystemEvents(): void {
  const addToast = useNotificationStore((s) => s.addToast)
  const addNewNotification = useNotificationStore((s) => s.addNewNotification)
  const setFeed = useNotificationStore((s) => s.setFeed)

  const lastSeenIdRef = useRef<number>((() => {
    const saved = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(STORAGE_KEY) : null
    return saved ? parseInt(saved, 10) || 0 : 0
  })())
  const initializedRef = useRef(false)

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null
    let active = true

    // Initial load of unread count / notification feed
    void fetchNotificationFeed(30, 0).then((feed) => {
      if (active) {
        setFeed(feed)
      }
    })

    async function poll() {
      if (!active) return
      const currentCursor = lastSeenIdRef.current

      // First run: bootstrap cursor without replaying unbounded historical events
      if (!initializedRef.current && currentCursor === 0) {
        initializedRef.current = true
        const initialEvents = await fetchSystemEvents(0, 50)
        if (!active) return
        if (initialEvents.length > 0) {
          const maxId = Math.max(...initialEvents.map((e) => e.id))
          lastSeenIdRef.current = maxId
          sessionStorage.setItem(STORAGE_KEY, String(maxId))

          // Only display genuinely recent events that occurred in the last 2 minutes
          const now = Date.now()
          const recent = initialEvents.filter((e) => {
            if (!e.ts) return false
            const t = Date.parse(e.ts)
            return !Number.isNaN(t) && now - t < RECENT_THRESHOLD_MS
          })
          for (const evt of recent.slice(-3)) {
            addToast(eventToToast(evt))
          }
        }
        return
      }

      initializedRef.current = true
      const newEvents = await fetchSystemEvents(lastSeenIdRef.current, 20)
      if (!active || newEvents.length === 0) return

      let maxId = lastSeenIdRef.current
      for (const evt of newEvents) {
        if (evt.id > lastSeenIdRef.current) {
          addToast(eventToToast(evt))
          addNewNotification(eventToNotificationItem(evt))
          if (evt.id > maxId) maxId = evt.id
        }
      }

      if (maxId > lastSeenIdRef.current) {
        lastSeenIdRef.current = maxId
        sessionStorage.setItem(STORAGE_KEY, String(maxId))
      }
    }

    // Run initial poll
    void poll()

    // Setup periodic 5-second polling
    timer = setInterval(() => {
      void poll()
    }, POLL_INTERVAL_MS)

    return () => {
      active = false
      if (timer) clearInterval(timer)
    }
  }, [addToast, addNewNotification, setFeed])
}
