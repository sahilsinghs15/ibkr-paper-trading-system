import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { squareOffAccountPositions } from '../api/configApi'

interface KillSwitchModalProps {
  isOpen: boolean
  accountId: number
  ibkrAccount: string
  openCount: number
  onClose: () => void
  onSuccess: (count: number) => void
}

function extractError(err: unknown): string {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const res = (err as { response?: { data?: { detail?: string } } }).response
    if (res?.data?.detail) return String(res.data.detail)
  }
  if (err instanceof Error) return err.message
  return 'Emergency square-off request failed'
}

export function KillSwitchModal({
  isOpen,
  accountId,
  ibkrAccount,
  openCount,
  onClose,
  onSuccess,
}: KillSwitchModalProps) {
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => squareOffAccountPositions(accountId),
    onSuccess: (res) => {
      setError(null)
      onSuccess(res.squared_off_count)
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
          <h3>⚠️ SQUARE OFF ALL POSITIONS</h3>
          <button type="button" className="modal-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">
          <div className="killswitch-account-badge">
            <span className="dim">ACCOUNT:</span>
            <span className="mono bold">{ibkrAccount}</span>
          </div>

          {openCount > 0 ? (
            <>
              <p className="killswitch-warning-text">
                You are about to close all <strong>{openCount}</strong> currently open
                position{openCount === 1 ? '' : 's'} for paper account{' '}
                <strong>{ibkrAccount}</strong>.
              </p>
              <p className="field-hint dim">
                This will submit CLOSE operations through the existing trading execution
                system (OMS &amp; IBKR). This action cannot be easily undone.
              </p>
            </>
          ) : (
            <p className="killswitch-warning-text">
              No open positions currently exist for account <strong>{ibkrAccount}</strong>.
            </p>
          )}

          {error ? <p className="settings-msg err">{error}</p> : null}
        </div>

        <div className="modal-footer">
          {openCount > 0 ? (
            <>
              <button type="button" className="btn" onClick={onClose}>
                CANCEL
              </button>
              <button
                type="button"
                className="btn danger"
                disabled={mutation.isPending}
                onClick={() => mutation.mutate()}
              >
                {mutation.isPending ? 'SQUARING OFF…' : '⚠️ SQUARE OFF ALL'}
              </button>
            </>
          ) : (
            <button type="button" className="btn" onClick={onClose}>
              CLOSE
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
