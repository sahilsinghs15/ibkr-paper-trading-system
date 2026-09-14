import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { fetchManualPositions, type ManualPositionApiRow } from '../api/manualTradingApi'
import { useActiveIbkrAccount } from '../hooks/useActiveIbkrAccount'
import { normalizeIbkrAccount } from '../utils/activeAccount'

export function ManualPositionsPage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const activeAccount = useActiveIbkrAccount()
  const cleanAccount = normalizeIbkrAccount(ibkrAccount || activeAccount)

  const [positions, setPositions] = useState<ManualPositionApiRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

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
            MANUAL POSITIONS (ISOLATED LEDGER)
          </h1>
          <span style={{ fontSize: '13px', color: '#94a3b8' }}>
            ACCOUNT: <strong style={{ color: '#38bdf8' }}>{cleanAccount || 'NONE'}</strong> · AUTHORITATIVE SOURCE = &quot;manual&quot;
          </span>
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

        <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '13px' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #334155', color: '#64748b' }}>
              <th style={{ padding: '10px' }}>TRADE ID</th>
              <th style={{ padding: '10px' }}>SYMBOL</th>
              <th style={{ padding: '10px' }}>SEC TYPE</th>
              <th style={{ padding: '10px' }}>QTY</th>
              <th style={{ padding: '10px' }}>AVG COST</th>
              <th style={{ padding: '10px' }}>REALIZED P&L</th>
              <th style={{ padding: '10px' }}>STATUS</th>
              <th style={{ padding: '10px' }}>SOURCE</th>
              <th style={{ padding: '10px' }}>OPENED AT</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? (
              <tr>
                <td colSpan={9} style={{ padding: '32px', textAlign: 'center', color: '#64748b' }}>
                  {loading ? 'Loading manual positions...' : `No manual positions open for account ${cleanAccount}.`}
                </td>
              </tr>
            ) : (
              positions.map((p) => (
                <tr key={p.id} style={{ borderBottom: '1px solid #1e293b' }}>
                  <td style={{ padding: '10px', fontFamily: 'monospace' }}>{p.trade_id}</td>
                  <td style={{ padding: '10px', fontWeight: 600 }}>{p.symbol}</td>
                  <td style={{ padding: '10px' }}>{p.sec_type}</td>
                  <td style={{ padding: '10px', fontFamily: 'monospace' }}>{String(p.signed_qty)}</td>
                  <td style={{ padding: '10px', fontFamily: 'monospace' }}>{String(p.avg_cost)}</td>
                  <td style={{ padding: '10px', fontFamily: 'monospace' }}>{String(p.realized_pnl)}</td>
                  <td style={{ padding: '10px' }}>{p.status}</td>
                  <td style={{ padding: '10px' }}>
                    <span
                      style={{
                        fontSize: '10px',
                        padding: '2px 6px',
                        borderRadius: '3px',
                        background: '#3b2d54',
                        color: '#d8b4fe',
                        border: '1px solid #7c3aed',
                        fontWeight: 600,
                      }}
                    >
                      {p.source}
                    </span>
                  </td>
                  <td style={{ padding: '10px', color: '#64748b' }}>{p.opened_at}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </main>
  )
}
