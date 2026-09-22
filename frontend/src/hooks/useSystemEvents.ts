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
// Retry cadence when the initial feed load fails; polling stays disarmed until it succeeds.
const INIT_RETRY_MS = 3000

const CANONICAL_MESSAGES: Record<string, Record<string, string>> = {
  ibgateway: {
    SERVICE_STARTED: 'IB Gateway Started Successfully',
    SERVICE_STOPPED: 'IB Gateway Stopped',
  },
  'trading-backend': {
    SERVICE_STARTED: 'OEMS Engine Started Successfully',
    SERVICE_STOPPED: 'OEMS Engine Stopped',
  },
  'webhook-ingest': {
    SERVICE_STARTED: 'Signal Receiver Started Successfully',
    SERVICE_STOPPED: 'Signal Receiver Stopped',
  },
  'demo-streaming': {
    SERVICE_STARTED: 'Dashboard Engine Started Successfully',
    SERVICE_STOPPED: 'Dashboard Engine Stopped',
  },
  'server-machine': {
    SERVICE_STARTED: 'Server Machine Started Successfully',
    SERVICE_STOPPED: 'Server Machine Stopped',
  },
  'ec2-instance': {
    SERVICE_STARTED: 'Server Machine Started Successfully',
    SERVICE_STOPPED: 'Server Machine Stopped',
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

  if (svc === 'server-machine' || svc === 'ec2-instance') {
    const action = evt.kind === 'SERVICE_STARTED' ? 'Started Successfully' : 'Stopped'
    const icon = evt.kind === 'SERVICE_STARTED' ? '🟢' : '🔴'
    const msg = `Server Machine ${action}`
    return { icon, title: msg, message: msg }
  }

  const fallbackMsg = String(detail.message || `${svc || 'Service'} ${evt.kind}`)
  return {
    icon: String(detail.icon || (evt.kind === 'SERVICE_STOPPED' ? '🔴' : '🟢')),
    title: fallbackMsg,
    message: fallbackMsg,
  }
}

function eventToToast(evt: SystemEventItem): ToastNotification {
  const { icon, title, message } = resolveCanonicalTitle(evt)
  const clean = message
    .replace(/^<pre>\s*/i, '')
    .replace(/\s*<\/pre>$/i, '')
    .trim()
  // Clean up message for single-line toast if it contains multi-line status table
  const singleLineMessage = clean.includes('\n')
    ? clean.split('\n')[0]
    : clean

  return {
    id: `evt-${evt.id}`,
    eventId: evt.id,
    kind: evt.kind,
    icon,
    title,
    message: singleLineMessage,
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

export function useSystemEvents(enabled = true): void {
  const addToast = useNotificationStore((s) => s.addToast)
  const addNewNotification = useNotificationStore((s) => s.addNewNotification)
  const setFeed = useNotificationStore((s) => s.setFeed)

  const lastSeenIdRef = useRef<number>((() => {
    const saved = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(STORAGE_KEY) : null
    return saved ? parseInt(saved, 10) || 0 : 0
  })())
  const initializedRef = useRef(false)

  useEffect(() => {
    // The notification feed is authenticated. Fetching it before login always
    // 401s, and that failure is what previously armed the poller with an
    // un-bootstrapped cursor.
    if (!enabled) return
    let timer: ReturnType<typeof setInterval> | null = null
    let retryTimer: ReturnType<typeof setTimeout> | null = null
    let active = true

    // 1. Initial load of notification feed & SILENT bootstrap of event cursor
    // Historical events go into the Notification Center only; NEVER replay toasts on mount.
    async function initFeedAndCursor() {
      try {
        const feed = await fetchNotificationFeed(30, 0)
        if (!active) return
        setFeed(feed)

        // Find the absolute highest/latest event ID in the system
        const latestHeadId = feed.items && feed.items.length > 0 ? feed.items[0].id : 0

        // Fast-forward cursor to head so no historical events are ever toasted
        lastSeenIdRef.current = Math.max(lastSeenIdRef.current, latestHeadId)
        if (typeof sessionStorage !== 'undefined') {
          sessionStorage.setItem(STORAGE_KEY, String(lastSeenIdRef.current))
        }
        // Only now is the cursor known to be at the head; polling may start.
        initializedRef.current = true
      } catch {
        // Do NOT start polling. This used to fall through to `finally`, which
        // armed the poller with the cursor still at 0 whenever the first fetch
        // failed (a 401 during auth bootstrap, or any network hiccup) — the next
        // poll then replayed up to 20 historical events as toasts. Retry instead;
        // an un-bootstrapped cursor must never be used.
        if (active) {
          retryTimer = setTimeout(() => {
            void initFeedAndCursor()
          }, INIT_RETRY_MS)
        }
      }
    }

    void initFeedAndCursor()

    // 2. Periodic poll for LIVE events arriving strictly after the dashboard was opened
    async function poll() {
      if (!active || !initializedRef.current) return

      try {
        const newEvents = await fetchSystemEvents(lastSeenIdRef.current, 20)
        if (!active || newEvents.length === 0) return

        const strictlyNew = newEvents
          .filter((e) => e.id > lastSeenIdRef.current)
          .sort((a, b) => a.id - b.id)

        if (strictlyNew.length === 0) return

        // Update cursor to the latest received event
        const maxId = strictlyNew[strictlyNew.length - 1].id
        lastSeenIdRef.current = maxId
        if (typeof sessionStorage !== 'undefined') {
          sessionStorage.setItem(STORAGE_KEY, String(maxId))
        }

        // Always register every event in the persistent Notification Center
        for (const evt of strictlyNew) {
          addNewNotification(eventToNotificationItem(evt))
        }

        // Manage transient live toasts: coalesce rapid bursts to avoid screen spam
        if (strictlyNew.length > 2) {
          // If multiple events arrived simultaneously (e.g. session start/stop sweep),
          // toast the most important event (e.g. stop/error) plus a coalesced badge
          const criticalEvt =
            strictlyNew.find(
              (e) =>
                e.kind === 'SERVICE_STOPPED' ||
                e.kind === 'ROGUE_TRADE_DETECTED' ||
                e.kind === 'LOSS_THRESHOLD_BREACHED',
            ) || strictlyNew[strictlyNew.length - 1]

          addToast(eventToToast(criticalEvt))
          addToast({
            id: `batch-${maxId}`,
            eventId: -maxId,
            kind: 'INFO',
            icon: '⚡',
            title: 'System Activity',
            message: `${strictlyNew.length} events logged. Check Notification Center for details.`,
            timeStr: formatTime(new Date().toISOString()),
          })
        } else {
          // 1 or 2 live events: show individual toasts
          for (const evt of strictlyNew) {
            addToast(eventToToast(evt))
          }
        }
      } catch {
        // Ignore transient poll errors
      }
    }

    // Setup periodic 5-second polling
    timer = setInterval(() => {
      void poll()
    }, POLL_INTERVAL_MS)

    return () => {
      active = false
      if (timer) clearInterval(timer)
      if (retryTimer) clearTimeout(retryTimer)
    }
  }, [addToast, addNewNotification, setFeed, enabled])
}
