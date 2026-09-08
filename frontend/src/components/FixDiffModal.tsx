import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { alignBrokerPositionLine } from '../api/reconcileApi'
import type {
  FlattenBrokerPositionResponse,
  LedgerPositionRow,
  ReconcileDiffRow,
} from '../types/reconcile'
import { fmtQty } from '../utils/format'

interface FixDiffModalProps {
  isOpen: boolean
  diff: ReconcileDiffRow
  ibkrAccount: string
  ledgerPositions: LedgerPositionRow[]
  onClose: () => void
  onSuccess: (res: FlattenBrokerPositionResponse) => void
}

const QTY_EPSILON = 1e-6

function extractError(err: unknown): string {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const res = (err as { response?: { data?: { detail?: string } } }).response
    if (res?.data?.detail) return String(res.data.detail)
  }
  if (err instanceof Error) return err.message
  return 'Broker align request failed'
}

function normSymbol(value: string): string {
  return value.trim().toUpperCase()
}

export function computeAlignPlan(diff: ReconcileDiffRow): {
  target: number
  current: number
  delta: number
  side: string | null
  quantity: number
} {
  const target = diff.ledger_qty ?? 0
  const current = diff.broker_qty ?? 0
  const delta = target - current
  if (Math.abs(delta) <= QTY_EPSILON) {
    return { target, current, delta, side: null, quantity: 0 }
  }
  const side = delta > 0 ? 'BUY' : 'SELL'
  return { target, current, delta, side, quantity: Math.abs(delta) }
}

function contributingLegQty(row: LedgerPositionRow, symbol: string): number | null {
  const norm = normSymbol(symbol)
  if (normSymbol(row.leg_a_symbol) === norm) return row.leg_a_signed_qty
  if (row.leg_b_symbol && normSymbol(row.leg_b_symbol) === norm) {
    return row.leg_b_signed_qty ?? null
  }
  return null
}

export function filterAffectedLedgerPairs(
  ledgerPositions: LedgerPositionRow[],
  diff: ReconcileDiffRow,
): Array<LedgerPositionRow & { contributing_qty: number | null }> {
  const norm = normSymbol(diff.symbol)
  return ledgerPositions
    .filter((row) => {
      if (diff.account_id != null && row.account_id !== diff.account_id) return false
      if (
        diff.ibkr_account &&
        row.ibkr_account &&
        normSymbol(row.ibkr_account) !== normSymbol(diff.ibkr_account)
      ) {
        return false
      }
      return (
        normSymbol(row.leg_a_symbol) === norm ||
        (row.leg_b_symbol != null && normSymbol(row.leg_b_symbol) === norm)
      )
    })
    .map((row) => ({
      ...row,
      contributing_qty: contributingLegQty(row, diff.symbol),
    }))
}

function resolveIbkrAccount(
  diff: ReconcileDiffRow,
  pageAccount?: string,
): string | null {
  if (diff.ibkr_account?.trim()) return diff.ibkr_account.trim()
  if (pageAccount?.trim()) return pageAccount.trim()
  return null
}

function fixDisabledReason(diff: ReconcileDiffRow, pageAccount?: string): string | null {
  if (diff.kind === 'MATCH') return 'Already matched'
  if (diff.kind === 'UNMAPPED_ACCOUNT') return 'IBKR account not mapped in config'
  if (diff.in_flight) return 'Account has in-flight execution'
  if (diff.con_id == null) return 'Missing conId on diff row'
  if (!resolveIbkrAccount(diff, pageAccount)) return 'Missing IBKR account on diff row'
  const plan = computeAlignPlan(diff)
  if (plan.side == null) return 'Broker already matches ledger'
  return null
}

export function canFixDiff(diff: ReconcileDiffRow, pageAccount?: string): boolean {
  if (!['QTY_DRIFT', 'BROKER_ORPHAN', 'LEDGER_GHOST'].includes(diff.kind)) {
    return false
  }
  return fixDisabledReason(diff, pageAccount) === null
}

export function fixTooltip(diff: ReconcileDiffRow, pageAccount?: string): string {
  return fixDisabledReason(diff, pageAccount) ?? 'Align IBKR broker qty to signal ledger'
}

export function FixDiffModal({
  isOpen,
  diff,
  ibkrAccount,
  ledgerPositions,
  onClose,
  onSuccess,
}: FixDiffModalProps) {
  const [error, setError] = useState<string | null>(null)
  const plan = computeAlignPlan(diff)
  const affected = filterAffectedLedgerPairs(ledgerPositions, diff)
  const canSubmit = plan.side != null && diff.con_id != null

  const mutation = useMutation({
    mutationFn: () => {
      if (diff.con_id == null) {
        throw new Error('Missing conId on diff row.')
      }
      return alignBrokerPositionLine({
        ibkr_account: ibkrAccount,
        symbol: diff.symbol,
        sec_type: diff.sec_type,
        con_id: diff.con_id,
      })
    },
    onSuccess: (res) => {
      setError(null)
      onSuccess(res)
      onClose()
    },
    onError: (err: unknown) => {
      setError(extractError(err))
    },
  })

  if (!isOpen) return null

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header danger-header">
          <h3>FIX BROKER → LEDGER</h3>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            disabled={mutation.isPending}
          >
            ✕
          </button>
        </div>
        <div className="modal-body">
          <div style={{ display: 'flex', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
            <div className="killswitch-account-badge">
              <span className="dim">ACCOUNT:</span>
              <span className="mono bold">{ibkrAccount}</span>
            </div>
            <div className="killswitch-account-badge">
              <span className="dim">SYMBOL:</span>
              <span className="mono bold">
                {diff.symbol} {diff.sec_type}
              </span>
            </div>
            <div className="killswitch-account-badge">
              <span className="dim">CONID:</span>
              <span className="mono bold">{diff.con_id ?? '—'}</span>
            </div>
            <div className="killswitch-account-badge">
              <span className="dim">KIND:</span>
              <span className="mono bold">{diff.kind}</span>
            </div>
          </div>

          <div className="reconcile-fix-summary" style={{ marginBottom: 12 }}>
            <p className="mono">
              Broker qty: {fmtQty(diff.broker_qty)} · Ledger qty: {fmtQty(diff.ledger_qty)}
            </p>
            {plan.side ? (
              <p className="killswitch-warning-text">
                Gap → submit MARKET <strong>{plan.side}</strong> for{' '}
                <strong>{fmtQty(plan.quantity)}</strong> shares
              </p>
            ) : (
              <p className="field-hint dim">No broker trade required — quantities already match.</p>
            )}
          </div>

          <div className="reconcile-fix-affected">
            <h4 style={{ margin: '0 0 8px' }}>Affected OPEN ledger pairs</h4>
            {affected.length === 0 ? (
              <p className="field-hint dim">
                No OPEN ledger pairs for this symbol — fix closes orphan broker exposure.
              </p>
            ) : (
              <div className="reconcile-table-wrap">
                <table className="reconcile-table">
                  <thead>
                    <tr>
                      <th>Trade ID</th>
                      <th>Strategy</th>
                      <th>Leg qty</th>
                    </tr>
                  </thead>
                  <tbody>
                    {affected.map((row) => (
                      <tr key={row.trade_id}>
                        <td className="mono">{row.trade_id}</td>
                        <td>{row.strategy_id}</td>
                        <td className="mono">
                          {row.contributing_qty != null ? fmtQty(row.contributing_qty) : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <p className="field-hint dim" style={{ marginTop: 12 }}>
            This adjusts the IBKR broker line only. OPEN Model Blue ledger pairs are{' '}
            <strong>not</strong> updated. This does <strong>not</strong> arm the account kill
            switch.
          </p>

          {error ? (
            <p className="settings-msg err" style={{ marginTop: 8 }}>
              {error}
            </p>
          ) : null}
        </div>

        <div className="modal-footer">
          <button type="button" className="btn" onClick={onClose} disabled={mutation.isPending}>
            CANCEL
          </button>
          <button
            type="button"
            className="btn danger"
            disabled={mutation.isPending || !canSubmit}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? 'SUBMITTING…' : 'SUBMIT FIX'}
          </button>
        </div>
      </div>
    </div>
  )
}
