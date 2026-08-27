import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { flattenBrokerPositionLine } from '../api/reconcileApi'
import type { FlattenBrokerPositionResponse, ReconcileDiffRow } from '../types/reconcile'

interface FlattenDiffModalProps {
  isOpen: boolean
  diff: ReconcileDiffRow
  ibkrAccount: string
  onClose: () => void
  onSuccess: (res: FlattenBrokerPositionResponse) => void
}

function extractError(err: unknown): string {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const res = (err as { response?: { data?: { detail?: string } } }).response
    if (res?.data?.detail) return String(res.data.detail)
  }
  if (err instanceof Error) return err.message
  return 'Broker flatten request failed'
}

function flattenSide(brokerQty: number): string {
  return brokerQty > 0 ? 'SELL' : 'BUY'
}

export function FlattenDiffModal({
  isOpen,
  diff,
  ibkrAccount,
  onClose,
  onSuccess,
}: FlattenDiffModalProps) {
  const [error, setError] = useState<string | null>(null)
  const brokerQty = diff.broker_qty ?? 0
  const closeQty = Math.abs(brokerQty)
  const closeSide = flattenSide(brokerQty)

  const mutation = useMutation({
    mutationFn: () =>
      flattenBrokerPositionLine({
        ibkr_account: ibkrAccount,
        symbol: diff.symbol,
        sec_type: diff.sec_type,
        con_id: diff.con_id as number,
      }),
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
          <h3>SQUARE OFF IBKR LINE</h3>
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
              <span className="mono bold">{diff.con_id}</span>
            </div>
          </div>

          <p className="killswitch-warning-text" style={{ marginTop: 8 }}>
            Submit MARKET <strong>{closeSide}</strong> for <strong>{closeQty}</strong> shares?
          </p>
          <p className="field-hint dim">
            This closes the IBKR broker line only. OPEN Model Blue ledger pairs are{' '}
            <strong>not</strong> updated — the next reconcile poll may show a ledger ghost until
            pairs are closed separately. This does <strong>not</strong> arm the account kill
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
            disabled={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? 'SUBMITTING…' : 'SQUARE OFF'}
          </button>
        </div>
      </div>
    </div>
  )
}

function squareOffDisabledReason(diff: ReconcileDiffRow): string | null {
  if (diff.kind === 'LEDGER_GHOST') {
    return 'No broker line — ledger-only ghost'
  }
  if (diff.in_flight) {
    return 'Account has in-flight execution'
  }
  if (diff.con_id == null) {
    return 'Missing conId on diff row'
  }
  if (!diff.ibkr_account) {
    return 'Missing IBKR account on diff row'
  }
  const qty = diff.broker_qty
  if (qty == null || Math.abs(qty) < 1e-6) {
    return 'No broker quantity to flatten'
  }
  return null
}

export function canSquareOffDiff(diff: ReconcileDiffRow): boolean {
  return squareOffDisabledReason(diff) === null
}

export function squareOffTooltip(diff: ReconcileDiffRow): string {
  return squareOffDisabledReason(diff) ?? 'Flatten this IBKR net line at broker'
}
