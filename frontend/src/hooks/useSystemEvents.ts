import { useEffect, useRef } from 'react'
import { fetchSystemEvents } from '../api/systemEventsApi'
import { useNotificationStore } from '../store/notificationStore'
import type { SystemEventItem, ToastNotification } from '../types/systemEvent'

const STORAGE_KEY = 'zahnrad_last_seen_event_id'
const POLL_INTERVAL_MS = 5000
const RECENT_THRESHOLD_MS = 120_000 // 2 minutes

function formatTime(isoStr: string | null): string {
  if (!isoStr) return ''
  try {
    const d = new Date(isoStr)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return ''
  }
}

function eventToToast(evt: SystemEventItem): ToastNotification {
  const detail = evt.detail || {}
  let icon = 'ℹ️'
  let title = 'System Event'
  let message = String(detail.message || '')

  if (evt.kind === 'SERVICE_STARTED') {
    icon = '🟢'
    title = 'Service Started'
    if (!message) message = `${detail.service || 'Service'} started`
  } else if (evt.kind === 'SERVICE_STOPPED') {
    icon = '🔴'
    title = 'Service Stopped'
    if (!message) message = `${detail.service || 'Service'} stopped`
  } else if (evt.kind === 'MARKET_CLOSED') {
    icon = '📅'
    title = 'Market Closed'
    if (!message) message = `Market closed — ${detail.reason || 'Holiday'}`
  }

  return {
    id: `evt-${evt.id}`,
    eventId: evt.id,
    kind: evt.kind,
    icon: String(detail.icon || icon),
    title,
    message,
    timeStr: formatTime(evt.ts),
  }
}

export function useSystemEvents(): void {
  const addToast = useNotificationStore((s) => s.addToast)
  const lastSeenIdRef = useRef<number>((() => {
    const saved = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(STORAGE_KEY) : null
    return saved ? parseInt(saved, 10) || 0 : 0
  })())
  const initializedRef = useRef(false)

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null
    let active = true

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
  }, [addToast])
}
