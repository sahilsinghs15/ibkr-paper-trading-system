import { useEffect, useRef } from 'react'
import axios from 'axios'
import type { PositionLeg, PositionsSnapshot } from '../types/position'
import { fetchSseToken } from '../store/authStore'
import { usePnlStore } from '../store/pnlStore'
import { useSignalStore } from '../store/signalStore'

async function loadSnapshot(
  apply: (row: PositionLeg) => void,
  clearActive: () => void,
): Promise<void> {
  const [openRes, closedRes] = await Promise.all([
    axios.get<PositionsSnapshot>('/demo/positions', {
      headers: { 'Cache-Control': 'no-store' },
    }),
    axios.get<{ closed_positions: PositionLeg[] }>('/demo/closed-positions', {
      headers: { 'Cache-Control': 'no-store' },
    }).catch(() => ({ data: { closed_positions: [] } })),
  ])
  const signalState = useSignalStore.getState()
  if (signalState.accountFilter) {
    void signalState.fetchSignals({ account: signalState.accountFilter })
    void signalState.fetchTraySignals({ account: signalState.accountFilter })
  }
  clearActive()
  const openRows = openRes.data.positions || []
  const closedRows = closedRes.data.closed_positions || []
  for (const row of openRows) {
    apply(row)
  }
  for (const row of closedRows) {
    apply(row)
  }
}

/**
 * Force a fresh positions snapshot into the store, outside the SSE lifecycle.
 *
 * Emergency flattens return 202 Accepted: the broker work is still in flight
 * when the caller's promise resolves, and the SSE publisher only emits
 * POSITION_CLOSED on its next poll after the rows actually leave the OPEN set.
 * Until then the dashboard keeps rendering positions that are already being
 * flattened, which previously needed a manual page refresh to clear.
 */
export async function resyncPositions(): Promise<void> {
  const { apply, clearActive } = usePnlStore.getState()
  await loadSnapshot(apply, clearActive)
}

/** Backoff schedule (ms) covering a typical flatten + publisher poll. */
const FLATTEN_RESYNC_DELAYS_MS = [0, 1_500, 4_000, 8_000]

/**
 * Re-pull the snapshot a few times after an accepted flatten so the dashboard
 * converges without a page refresh. Returns a cancel function.
 */
export function scheduleFlattenResync(): () => void {
  const timers = FLATTEN_RESYNC_DELAYS_MS.map((delay) =>
    setTimeout(() => {
      void resyncPositions().catch((err) => console.warn('flatten resync failed', err))
    }, delay),
  )
  return () => timers.forEach(clearTimeout)
}

/**
 * Snapshot + SSE live updates for the PnL dashboard.
 * Reconnects after error: wait 1s, reload snapshot, reconnect.
 */
export function usePnlStream(): void {
  const apply = usePnlStore((s) => s.apply)
  const clearActive = usePnlStore((s) => s.clearActive)
  const setStreamState = usePnlStore((s) => s.setStreamState)
  const sourceRef = useRef<EventSource | null>(null)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const stopped = useRef(false)

  useEffect(() => {
    stopped.current = false

    async function connect() {
      if (stopped.current) return
      if (sourceRef.current) {
        sourceRef.current.close()
        sourceRef.current = null
      }
      setStreamState('CONNECTING')
      const sseToken = await fetchSseToken()
      if (stopped.current) return
      const streamUrl = sseToken ? `/demo/stream?token=${encodeURIComponent(sseToken)}` : '/demo/stream'
      const source = new EventSource(streamUrl)
      sourceRef.current = source

      source.onopen = () => {
        if (!stopped.current) setStreamState('LIVE')
      }

      source.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data) as Record<string, unknown>
          if (data && data.event === 'SIGNAL_RECEIVED') {
            useSignalStore.getState().handleSignalEvent(data)
          } else {
            apply(data as unknown as PositionLeg)
          }
        } catch (err) {
          console.warn(err)
        }
      }

      source.onerror = () => {
        if (stopped.current) return
        setStreamState('RECONNECTING')
        source.close()
        sourceRef.current = null
        if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
        reconnectTimer.current = setTimeout(async () => {
          try {
            await loadSnapshot(apply, clearActive)
          } catch (err) {
            console.warn(err)
          }
          connect()
        }, 1000)
      }
    }

    loadSnapshot(apply, clearActive)
      .then(connect)
      .catch(() => connect())

    return () => {
      stopped.current = true
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      if (sourceRef.current) {
        sourceRef.current.close()
        sourceRef.current = null
      }
    }
  }, [apply, clearActive, setStreamState])
}
