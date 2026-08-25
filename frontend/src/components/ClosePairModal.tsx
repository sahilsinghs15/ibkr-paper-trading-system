import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { closeSinglePair } from '../api/configApi'
import type { ClosePairResponse } from '../types/config'

interface ClosePairModalProps {
  isOpen: boolean
  accountId: number
  ibkrAccount: string
  tradeId: string
  legASymbol: string
  legBSymbol?: string | null
  onClose: () => void
  onSuccess: (res: ClosePairResponse) => void
}

function extractError(err: unknown): string {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const res = (err as { response?: { data?: { detail?: string } } }).response
    if (res?.data?.detail) return String(res.data.detail)
  }
  if (err instanceof Error) return err.message
  return 'Failed to close pair'
}

export function ClosePairModal({
  isOpen,
  accountId,
  ibkrAccount,
  tradeId,
  legASymbol,
  legBSymbol,
  onClose,
  onSuccess,
}: ClosePairModalProps) {
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => closeSinglePair(accountId, tradeId),
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

  const pairText = legBSymbol ? `${legASymbol} / ${legBSymbol}` : legASymbol

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header danger-header">
          <h3>⚠️ CLOSE PAIR CONFIRMATION</h3>
          <button type="button" className="modal-close" onClick={onClose} disabled={mutation.isPending}>
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
              <span className="dim">TRADE ID:</span>
              <span className="mono bold">{tradeId}</span>
            </div>
          </div>

          <p className="killswitch-warning-text" style={{ marginTop: 8 }}>
            Close <strong>{pairText}</strong>?
          </p>
          <p className="field-hint dim">
            Both legs of this pair will be closed for account <strong>{ibkrAccount}</strong>. Other open pairs for this account will remain open and unaffected.
          </p>

          {error ? <p className="settings-msg err" style={{ marginTop: 8 }}>{error}</p> : null}
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
            {mutation.isPending ? 'CLOSING PAIR…' : '⚠️ CLOSE PAIR'}
          </button>
        </div>
      </div>
    </div>
  )
}
