import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { fetchManualPositions, type ManualPositionApiRow } from '../api/manualTradingApi'
import { useActiveIbkrAccount } from '../hooks/useActiveIbkrAccount'
import { normalizeIbkrAccount } from '../utils/activeAccount'
import { usePnlStore } from '../store/pnlStore'
import { fmtPnl, fmtQty, fmtUsd, num, pnlClass } from '../utils/format'

export function ManualPositionsPage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const navigate = useNavigate()
  const activeAccount = useActiveIbkrAccount()
  const cleanAccount = normalizeIbkrAccount(ibkrAccount || activeAccount)

  const [positions, setPositions] = useState<ManualPositionApiRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Live PnL via same demo stream as main Positions (not a different API)
  const activeLegs = usePnlStore((s) => s.active)
  const manualLiveByTrade = useMemo(() => {
    const map = new Map<string, { unrealized: string | number | null, mark: string | number | null, marketStatus: string | null }>()
    const want = (cleanAccount || '').trim().toUpperCase()
    for (const leg of Object.values(activeLegs)) {
      if (String(leg.source || '').toLowerCase() !== 'manual') continue
      if (want && String(leg.ibkr_account || '').trim().toUpperCase() !== want) continue
      const tid = String(leg.trade_id || '')
      if (!tid) continue
      map.set(tid, { unrealized: leg.unrealized_pnl as string | number | null, mark: (leg.mark_price || leg.last_price) as string | number | null, marketStatus: leg.market_data_status as string | null })
    }
    return map
  }, [activeLegs, cleanAccount])

  const summary = useMemo(() => {
    let gross = 0
    let net = 0
    let unreal = 0
    let realized = 0
    let hasUnreal = false
    for (const p of positions) {
      const live = manualLiveByTrade.get(p.trade_id)
      const mark = live?.mark != null ? num(live.mark) : null
      const qty = Number(p.signed_qty)
      const entry = Number(p.avg_cost)
      if (mark != null && !Number.isNaN(mark)) {
        const mv = Math.abs(qty) * mark
        gross += mv
        net += qty * mark
        const pnl = qty * (mark - entry)
        unreal += pnl
        hasUnreal = true
      }
      const rp = Number(p.realized_pnl)
      if (!Number.isNaN(rp)) realized += rp
    }
    return { gross, net, unreal: hasUnreal ? unreal : null, realized }
  }, [positions, manualLiveByTrade])

  const loadData = useCallback(async () => {
    if (!cleanAccount) return
    try {
      setLoading(true)
      setError(null)
      const res = await fetchManualPositions(cleanAccount)
      setPositions(res.positions)
    } catch (err: unknown) {
      const ae = err as { response?: { data?: { detail?: string } } }
      setError(ae?.response?.data?.detail || (err instanceof Error ? err.message : 'Failed to load manual positions'))
    } finally {
      setLoading(false)
    }
  }, [cleanAccount])

  useEffect(() => {
    void loadData()
  }, [loadData])

  return (
    <main className="page manual-positions-page" style={{ padding: '24px 32px', maxWidth: '1400px', margin: '0 auto' }}>
      <div className="section-h" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
        <div>
          <h1 style={{ fontSize: '24px', fontWeight: 700, margin: 0, color: '#f8fafc' }}>
            MANUAL POSITIONS
          </h1>

        </div>
        <div style={{ display: 'flex', gap: '12px' }}>
          <button
            type="button"
            onClick={() => void loadData()}
            style={{
              padding: '6px 14px',
              borderRadius: '4px',
              background: '#1e293b',
              color: '#e2e8f0',
              fontSize: '12px',
              fontWeight: 600,
              cursor: 'pointer',
              border: '1px solid #334155',
            }}
          >
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
          <Link
            to={`/account/${cleanAccount}/manual-trade`}
            style={{
              padding: '6px 14px',
              borderRadius: '4px',
              background: '#1e293b',
              color: '#e2e8f0',
              fontSize: '12px',
              fontWeight: 600,
              textDecoration: 'none',
              border: '1px solid #334155',
            }}
          >
            ← BACK TO MANUAL TICKET
          </Link>
        </div>
      </div>

      {error && (
        <div style={{ padding: '12px 16px', background: '#451a1a', border: '1px solid #7f1d1d', borderRadius: '6px', color: '#fca5a5', marginBottom: '16px', fontSize: '13px' }}>
          {error}
        </div>
      )}

      <div
        style={{
          background: '#0f172a',
          border: '1px solid #1e293b',
          borderRadius: '8px',
          padding: '24px',
        }}
      >
        <div style={{ marginBottom: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span
              style={{
                fontSize: '11px',
                padding: '2px 8px',
                borderRadius: '4px',
                background: '#3b2d54',
                color: '#d8b4fe',
                border: '1px solid #7c3aed',
                fontWeight: 600,
              }}
            >
              Manual Trading Only
            </span>
            <span style={{ fontSize: '13px', color: '#64748b' }}>
              Engine positions are strictly excluded from this view.
            </span>
          </div>
          <span style={{ fontSize: '12px', color: '#64748b' }}>
            {positions.length} Open Manual Positions
          </span>
        </div>

        {/* Summary — operator-grade, same hierarchy as main Positions */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, minmax(0, 1fr))', gap: 8, marginBottom: 16 }}>
          <div style={{ background: '#0b0e14', border: '1px solid #1a2330', borderRadius: 6, padding: '10px 12px' }}>
            <div style={{ fontSize: 10, color: 'var(--dim)', fontWeight: 700, letterSpacing: '0.06em' }}>OPEN POSITIONS</div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 16, fontWeight: 700 }}>{positions.length}</div>
          </div>
          <div style={{ background: '#0b0e14', border: '1px solid #1a2330', borderRadius: 6, padding: '10px 12px' }}>
            <div style={{ fontSize: 10, color: 'var(--dim)', fontWeight: 700, letterSpacing: '0.06em' }}>GROSS MARKET VALUE</div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 14, fontWeight: 600 }}>{summary.gross ? fmtUsd(summary.gross) : '—'}</div>
          </div>
          <div style={{ background: '#0b0e14', border: '1px solid #1a2330', borderRadius: 6, padding: '10px 12px' }}>
            <div style={{ fontSize: 10, color: 'var(--dim)', fontWeight: 700, letterSpacing: '0.06em' }}>UNREALIZED P&L</div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 14, fontWeight: 600 }} className={summary.unreal != null ? pnlClass(summary.unreal) : ''}>{summary.unreal != null ? fmtPnl(summary.unreal) : 'MARK UNAVAILABLE'}</div>
          </div>
          <div style={{ background: '#0b0e14', border: '1px solid #1a2330', borderRadius: 6, padding: '10px 12px' }}>
            <div style={{ fontSize: 10, color: 'var(--dim)', fontWeight: 700, letterSpacing: '0.06em' }}>REALIZED P&L</div>
            <div style={{ fontFamily: 'var(--mono)', fontSize: 14, fontWeight: 600 }} className={pnlClass(summary.realized)}>{fmtPnl(summary.realized)}</div>
          </div>
        </div>

        <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '13px' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #334155', color: '#64748b' }}>
              <th style={{ padding: '10px' }}>SYMBOL</th>
              <th style={{ padding: '10px' }}>SIDE</th>
              <th style={{ padding: '10px' }}>QTY</th>
              <th style={{ padding: '10px' }}>AVG COST</th>
              <th style={{ padding: '10px' }}>MARK</th>
              <th style={{ padding: '10px' }}>MARKET VALUE</th>
              <th style={{ padding: '10px' }}>UNREALIZED P&L</th>
              <th style={{ padding: '10px' }}>REALIZED P&L</th>
              <th style={{ padding: '10px' }}>STATUS</th>
              <th style={{ padding: '10px', textAlign: 'right' }}>ACTION</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={10} style={{ padding: '32px', textAlign: 'center', color: '#64748b' }}>
                  {loading ? 'Loading manual positions...' : `No manual positions open for account ${cleanAccount}.`}
                </td>
              </tr>
            ) : (
              positions.map((p) => {
                const qty = Number(p.signed_qty)
                const side = qty >= 0 ? 'BUY' : 'SELL'
                const absQty = Math.abs(qty)
                const avg = Number(p.avg_cost)
                const live = manualLiveByTrade.get(p.trade_id)
                const mark = live?.mark != null ? Number(live.mark) : null
                const unreal = mark != null && !Number.isNaN(mark) ? (mark - avg) * qty : null
                const hasMark = mark != null && !Number.isNaN(mark)
                const mv = hasMark ? absQty * mark : null
                return (
                  <tr key={p.id} style={{ borderBottom: '1px solid #1e293b' }}>
                    <td style={{ padding: '10px', fontWeight: 600 }}>{p.symbol}</td>
                    <td style={{ padding: '10px' }}><span style={{ color: side === 'BUY' ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>{side}</span></td>
                    <td style={{ padding: '10px', fontFamily: 'monospace' }}>{fmtQty(absQty)}</td>
                    <td style={{ padding: '10px', fontFamily: 'monospace' }}>{fmtUsd(avg)}</td>
                    <td style={{ padding: '10px', fontFamily: 'monospace' }}>{hasMark ? fmtUsd(mark!) : <span style={{ color: 'var(--dim)' }}>MARK UNAVAILABLE</span>}</td>
                    <td style={{ padding: '10px', fontFamily: 'monospace' }}>{hasMark && mv != null ? fmtUsd(mv) : '—'}</td>
                    <td style={{ padding: '10px', fontFamily: 'monospace' }} className={unreal != null ? pnlClass(unreal) : ''}>{unreal != null ? fmtPnl(unreal) : hasMark ? '—' : <span style={{ color: 'var(--dim)' }}>MARK UNAVAILABLE</span>}</td>
                    <td style={{ padding: '10px', fontFamily: 'monospace' }} className={pnlClass(Number(p.realized_pnl))}>{fmtPnl(Number(p.realized_pnl))}</td>
                    <td style={{ padding: '10px' }}>{p.status}</td>
                    <td style={{ padding: '10px', textAlign: 'right' }}>
                      <button
                        type="button"
                        style={{ padding: '4px 8px', fontSize: '10px', fontWeight: 600, background: '#3b2d54', color: '#d8b4fe', border: '1px solid #7c3aed', borderRadius: 4, cursor: 'pointer' }}
                        onClick={() => {
                          const side = qty >= 0 ? 'SELL' : 'BUY'
                          navigate(`/account/${cleanAccount}/manual-trade?close_symbol=${encodeURIComponent(p.symbol)}&close_side=${side}&close_qty=${absQty}&close_trade_id=${encodeURIComponent(String(p.trade_id))}`)
                        }}
                      >
                        Close
                      </button>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>
    </main>
  )
}
