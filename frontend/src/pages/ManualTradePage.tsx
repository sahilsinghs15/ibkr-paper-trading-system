import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import {
  fetchGatewayStatus,
  type GatewayStatusResponse,
  type ManualPositionApiRow,
} from '../api/manualTradingApi'
import { ManualOrdersBlotter } from '../components/manual/ManualOrdersBlotter'
import { ManualOrderTicket, type CloseIntent } from '../components/manual/ManualOrderTicket'
import { ManualPositionsPanel } from '../components/manual/ManualPositionsPanel'
import {
  apiErrorMessage,
  useLiveManualMarks,
  useManualOrders,
  useManualPositions,
} from '../components/manual/useManualWorkspace'
import { useActiveIbkrAccount } from '../hooks/useActiveIbkrAccount'
import { usePnlStore } from '../store/pnlStore'
import { normalizeIbkrAccount } from '../utils/activeAccount'

/**
 * Manual Trading workspace: current manual exposure first (with the full
 * ledger inline), then manual order entry and the order blotter.
 *
 * Every trading action goes through its authoritative, audited backend
 * endpoint; this page only orchestrates presentation and refresh.
 */
export function ManualTradePage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const activeAccount = useActiveIbkrAccount()
  const account = normalizeIbkrAccount(ibkrAccount || activeAccount)
  const streamState = usePnlStore((s) => s.streamState)

  // ── Gateway status ────────────────────────────────────────────────
  const [gateway, setGateway] = useState<GatewayStatusResponse | null>(null)
  const [gatewayLoading, setGatewayLoading] = useState(false)
  const [gatewayError, setGatewayError] = useState<string | null>(null)
  const loadGateway = useCallback(async () => {
    try {
      setGatewayLoading(true)
      setGatewayError(null)
      setGateway(await fetchGatewayStatus(account))
    } catch (err: unknown) {
      setGatewayError(apiErrorMessage(err, 'Failed to query gateway'))
    } finally {
      setGatewayLoading(false)
    }
  }, [account])
  useEffect(() => {
    void loadGateway()
  }, [loadGateway])

  // ── Data ──────────────────────────────────────────────────────────
  const positions = useManualPositions(account)
  const orders = useManualOrders(account)
  const live = useLiveManualMarks(account)

  // Coherence: an order status/fill change or a change in the live set of
  // manual trades means the position ledger moved — refetch it. No extra polling.
  const reloadPositions = positions.reload
  const lastFillSig = useRef<string | null>(null)
  useEffect(() => {
    if (!orders.loaded) return
    if (lastFillSig.current !== null && lastFillSig.current !== orders.fillSignature) void reloadPositions()
    lastFillSig.current = orders.fillSignature
  }, [orders.fillSignature, orders.loaded, reloadPositions])
  const lastTradeSig = useRef<string | null>(null)
  useEffect(() => {
    if (lastTradeSig.current !== null && lastTradeSig.current !== live.tradeSignature) void reloadPositions()
    lastTradeSig.current = live.tradeSignature
  }, [live.tradeSignature, reloadPositions])
  useEffect(() => {
    lastFillSig.current = null
    lastTradeSig.current = null
  }, [account])

  const refreshAll = () => {
    void loadGateway()
    void positions.reload()
    void orders.reload()
  }

  // ── Ledger + close mode live in the URL (deep-linkable, back-button friendly) ──
  const ledgerOpen = searchParams.get('ledger') === '1'
  const toggleLedger = () => {
    const p = new URLSearchParams(searchParams)
    if (ledgerOpen) p.delete('ledger')
    else p.set('ledger', '1')
    setSearchParams(p, { replace: true })
  }

  const closeSymbol = searchParams.get('close_symbol')
  const closeSide = searchParams.get('close_side')
  const closeQty = searchParams.get('close_qty')
  const closeTradeIdParam = searchParams.get('close_trade_id')
  const closeIntent = useMemo<CloseIntent | null>(
    () =>
      closeSymbol && (closeSide === 'BUY' || closeSide === 'SELL') && closeQty
        ? { symbol: closeSymbol, side: closeSide, qty: closeQty, tradeId: closeTradeIdParam }
        : null,
    [closeSymbol, closeSide, closeQty, closeTradeIdParam],
  )

  const ticketRef = useRef<HTMLDivElement>(null)
  const startClose = (row: ManualPositionApiRow, side: 'BUY' | 'SELL', absQty: number) => {
    const p = new URLSearchParams(searchParams)
    p.set('close_symbol', row.symbol)
    p.set('close_side', side)
    p.set('close_qty', String(absQty))
    p.set('close_trade_id', row.trade_id)
    setSearchParams(p, { replace: true })
    requestAnimationFrame(() => ticketRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }))
  }
  const exitClose = () => {
    const p = new URLSearchParams(searchParams)
    for (const k of ['close_symbol', 'close_side', 'close_qty', 'close_trade_id']) p.delete(k)
    setSearchParams(p, { replace: true })
  }
  // Arriving with a close intent (e.g. from the main Positions page): bring the ticket into view.
  useEffect(() => {
    if (closeIntent) ticketRef.current?.scrollIntoView({ block: 'start' })
    // only on first render
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const gatewayDot = gateway?.connected ? 'ok' : gatewayError ? 'bad' : 'warn'
  const gatewayText = gateway?.connected ? 'Connected' : gateway ? 'Disconnected' : 'Unknown'
  const envBadge = !gateway ? null : gateway.environment === 'VERIFIED_PAPER' ? (
    <span className="paper-pill">Paper</span>
  ) : gateway.environment === 'VERIFIED_LIVE' ? (
    <span className="live-pill">Live</span>
  ) : (
    <span className="dim">Env unknown</span>
  )
  const anyLoading = gatewayLoading || positions.loading || orders.loading

  return (
    <main className="manual-page mw-page">
      <header className="mw-header">
        <div className="mw-header-main">
          <h1>Manual Trading</h1>
          <div className="mw-context">
            <span className="mw-context-label">Account</span>
            <strong className="mono">{account || '—'}</strong>
            {envBadge}
            <span className="mw-hint">CFD · discretionary · isolated from the engine</span>
          </div>
        </div>
        <button type="button" className="manual-btn" onClick={refreshAll} disabled={anyLoading}>
          {anyLoading ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>

      <div className="manual-status-bar" role="status" aria-label="Connection status">
        <span className={`dot ${gatewayDot}`}>
          <i />
          IBKR {gatewayText}
        </span>
        {gateway && (
          <>
            <span className="sep" />
            <span className="dim">
              {gateway.host}:{gateway.port} · ID {gateway.client_id}
            </span>
          </>
        )}
        {gateway?.managed_accounts?.length ? (
          <>
            <span className="sep" />
            <span className="mw-hint">{gateway.managed_accounts.join(', ')}</span>
          </>
        ) : null}
        <span className="sep" />
        <span className={`dot ${streamState === 'LIVE' ? 'ok' : 'warn'}`}>
          <i />
          Live marks {streamState === 'LIVE' ? 'streaming' : streamState.toLowerCase()}
        </span>
        <span className="sep" />
        <span className="mw-hint">{gatewayError || gateway?.message || '—'}</span>
      </div>

      <ManualPositionsPanel
        account={account}
        rows={positions.rows}
        loading={positions.loading}
        loaded={positions.loaded}
        error={positions.error}
        liveByTrade={live.byTrade}
        ledgerOpen={ledgerOpen}
        onToggleLedger={toggleLedger}
        onRetry={() => void positions.reload()}
        onClose={startClose}
      />

      <section className="mw-section" aria-labelledby="mw-orders-title">
        <header className="mw-section-head">
          <div className="mw-section-title">
            <h2 id="mw-orders-title">Manual Orders</h2>
            <span className="mw-hint">Order entry and order activity for {account || '—'}</span>
          </div>
        </header>
        <ManualOrderTicket
          ref={ticketRef}
          account={account}
          closeIntent={closeIntent}
          positions={positions.rows}
          onExitClose={exitClose}
          onSubmitted={() => {
            if (closeIntent) exitClose()
            orders.setPage(1)
            void orders.reload(1)
            void positions.reload()
          }}
        />
        <ManualOrdersBlotter
          account={account}
          orders={orders.orders}
          total={orders.total}
          page={orders.page}
          setPage={orders.setPage}
          loading={orders.loading}
          loaded={orders.loaded}
          error={orders.error}
          onRetry={() => void orders.reload()}
          onCancelled={() => {
            void orders.reload()
            void positions.reload()
          }}
        />
      </section>
    </main>
  )
}
