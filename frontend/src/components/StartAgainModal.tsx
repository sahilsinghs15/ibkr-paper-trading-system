import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { clearKillSwitch } from '../api/configApi'

interface StartAgainModalProps {
  isOpen: boolean
  accountId: number
  ibkrAccount: string
  onClose: () => void
  onSuccess: () => void
}

function extractError(err: unknown): string {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const res = (err as { response?: { data?: { detail?: string } } }).response
    if (res?.data?.detail) return String(res.data.detail)
  }
  if (err instanceof Error) return err.message
  return 'Failed to start account'
}

export function StartAgainModal({
  isOpen,
  accountId,
  ibkrAccount,
  onClose,
  onSuccess,
}: StartAgainModalProps) {
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => clearKillSwitch(accountId),
    onSuccess: () => {
      setError(null)
      onSuccess()
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
        <div className="modal-header">
          <h3>▶ START AGAIN CONFIRMATION</h3>
          <button type="button" className="modal-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">
          <div className="killswitch-account-badge">
            <span className="dim">ACCOUNT:</span>
            <span className="mono bold">{ibkrAccount}</span>
          </div>

          <p className="killswitch-warning-text" style={{ marginTop: 12 }}>
            Are you sure you want to start this account again? It will be allowed to receive trading signals.
          </p>
          <p className="field-hint dim">
            This will disarm the emergency Kill Switch for paper account{' '}
            <strong>{ibkrAccount}</strong> and restore its ACTIVE status. No IBKR orders will be automatically submitted.
          </p>

          {error ? <p className="settings-msg err">{error}</p> : null}
        </div>

        <div className="modal-footer">
          <button type="button" className="btn" onClick={onClose} disabled={mutation.isPending}>
            CANCEL
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? 'STARTING…' : 'YES, START AGAIN'}
          </button>
        </div>
      </div>
    </div>
  )
}
