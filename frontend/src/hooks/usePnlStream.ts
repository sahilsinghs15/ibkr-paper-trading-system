import { useEffect, useRef } from 'react'
import axios from 'axios'
import type { PositionLeg, PositionsSnapshot } from '../types/position'
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

    function connect() {
      if (stopped.current) return
      if (sourceRef.current) {
        sourceRef.current.close()
        sourceRef.current = null
      }
      setStreamState('CONNECTING')
      const source = new EventSource('/demo/stream')
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
