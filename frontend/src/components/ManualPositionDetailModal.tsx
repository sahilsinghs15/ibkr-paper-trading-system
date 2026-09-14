import type { PositionLeg } from '../types/position'
import { fmtPnl, fmtQty, fmtUsd, pnlClass } from '../utils/format'

interface Props {
  isOpen: boolean
  accountId: number
  tradeId: string
  legs?: PositionLeg[]
  onClose: () => void
  onCloseViaManual?: (tradeId: string, symbol: string, qty: string, side: string) => void
}

export function ManualPositionDetailModal({ isOpen, accountId, tradeId, legs, onClose, onCloseViaManual }: Props) {
  if (!isOpen) return null

  const head = legs?.[0]
  const qtyNum = head ? Number(head.quantity ?? head.filled_quantity ?? 0) : null
  const side = head?.side || (qtyNum !== null ? (qtyNum >= 0 ? 'BUY' : 'SELL') : '—')
  const absQty = qtyNum !== null ? Math.abs(qtyNum) : null
  const pos = head
    ? {
        symbol: head.symbol || tradeId,
        signed_qty: head.quantity || '0',
        avg_cost: head.entry_price || '0',
        realized_pnl: head.realized_pnl || '0',
        status: head.status || 'OPEN',
        trade_id: tradeId,
        account_id: accountId,
      }
    : null

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card signal-detail-modal pair-detail-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>MANUAL POSITION — {pos?.symbol || tradeId}</h3>
          <button type="button" className="modal-close" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body signal-detail-modal-body">
          <div className="pair-detail-meta">
            <div className="killswitch-account-badge"><span className="dim">ACCOUNT:</span> <span className="mono bold">{pos?.account_id ?? accountId}</span></div>
            <div className="killswitch-account-badge"><span className="dim">TRADE ID:</span> <span className="mono bold">{tradeId}</span></div>
            <div className="killswitch-account-badge"><span className="dim">SOURCE:</span> <span className="mono bold" style={{ color: '#d8b4fe' }}>MANUAL</span></div>
            <div className="killswitch-account-badge"><span className="dim">STATUS:</span> <span className="mono bold">{pos?.status || 'OPEN'}</span></div>
          </div>

          {pos ? (
            <section className="pair-detail-section">
              <h4>POSITION</h4>
              <table className="factory-table drawer-leg-table pair-legs-table">
                <thead><tr><th>SYMBOL</th><th>SIDE</th><th>QTY</th><th>AVG COST</th><th>MARK</th><th>UNREALIZED P&L</th><th>REALIZED P&L</th></tr></thead>
                <tbody>
                  <tr>
                    <td className="mono">{pos.symbol}</td>
                    <td className="mono">{side}</td>
                    <td className="mono">{absQty !== null ? fmtQty(absQty) : '—'}</td>
                    <td className="mono">{pos.avg_cost != null ? fmtUsd(pos.avg_cost) : '—'}</td>
                    <td className="mono">{head?.mark_price != null ? fmtUsd(head.mark_price) : head?.last_price != null ? fmtUsd(head.last_price) : '—'}</td>
                    <td className={`mono ${head?.unrealized_pnl != null ? pnlClass(Number(head.unrealized_pnl)) : ''}`}>{head?.unrealized_pnl != null ? fmtPnl(Number(head.unrealized_pnl)) : head?.market_data_status === 'UNAVAILABLE' ? 'MARK UNAVAILABLE' : '—'}</td>
                    <td className={`mono ${pos.realized_pnl != null ? pnlClass(Number(pos.realized_pnl)) : ''}`}>{pos.realized_pnl != null ? fmtPnl(Number(pos.realized_pnl)) : '—'}</td>
                  </tr>
                </tbody>
              </table>
              <p className="field-hint dim" style={{ marginTop: 8 }}>
                Manual positions are standalone discretionary trades. Use “Close via Manual Trading” to create an opposite manual order (e.g. SELL {absQty} {pos.symbol} to close long). No engine pair is involved.
              </p>
              <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
                <button
                  type="button"
                  className="btn primary"
                  onClick={() => {
                    if (onCloseViaManual && pos) {
                      const closeSide = Number(pos.signed_qty) >= 0 ? 'SELL' : 'BUY'
                      onCloseViaManual(pos.trade_id, pos.symbol, String(Math.abs(Number(pos.signed_qty))), closeSide)
                    }
                    onClose()
                  }}
                >
                  Close via Manual Trading
                </button>
                <button type="button" className="btn" onClick={onClose}>Close</button>
              </div>
            </section>
          ) : (
            <p className="dim">Manual position not found.</p>
          )}
        </div>
      </div>
    </div>
  )
}
