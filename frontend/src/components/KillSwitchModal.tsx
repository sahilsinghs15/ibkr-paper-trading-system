import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import {
  squareOffEnginePositions,
  squareOffEntireAccount,
  squareOffManualPositions,
  type SquareOffResult,
} from '../api/configApi'

interface KillSwitchModalProps {
  isOpen: boolean
  accountId: number
  ibkrAccount: string
  openCount: number
  onClose: () => void
  onSuccess: (count: number, scope: string) => void
}

type FlattenOption = 'account' | 'engine' | 'manual' | null

function extractError(err: unknown): string {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const res = (err as { response?: { data?: { detail?: string } } }).response
    if (res?.data?.detail) return String(res.data.detail)
  }
  if (err instanceof Error) return err.message
  return 'Emergency flatten request failed'
}

export function KillSwitchModal({
  isOpen,
  accountId,
  ibkrAccount,
  openCount,
  onClose,
  onSuccess,
}: KillSwitchModalProps) {
  const [selectedOption, setSelectedOption] = useState<FlattenOption>(null)
  const [error, setError] = useState<string | null>(null)

  const handleReset = () => {
    setSelectedOption(null)
    setError(null)
    onClose()
  }

  const engineMutation = useMutation({
    mutationFn: () => squareOffEnginePositions(accountId),
    onSuccess: (res: SquareOffResult) => {
      setError(null)
      onSuccess(res.squared_off_count, 'Flatten Signal Positions')
      handleReset()
    },
    onError: (err: unknown) => {
      setError(extractError(err))
    },
  })

  const manualMutation = useMutation({
    mutationFn: () => squareOffManualPositions(accountId),
    onSuccess: (res: SquareOffResult) => {
      setError(null)
      onSuccess(res.squared_off_count, 'Flatten Manual Positions')
      handleReset()
    },
    onError: (err: unknown) => {
      setError(extractError(err))
    },
  })

  const accountMutation = useMutation({
    mutationFn: () => squareOffEntireAccount(accountId),
    onSuccess: (res: SquareOffResult) => {
      if (res.error) {
        setError(res.error)
      } else {
        setError(null)
        onSuccess(res.squared_off_count, 'Flatten Account')
        handleReset()
      }
    },
    onError: (err: unknown) => {
      setError(extractError(err))
    },
  })

  if (!isOpen) return null

  const isPending = engineMutation.isPending || manualMutation.isPending || accountMutation.isPending

  return (
    <div className="modal-overlay" onClick={isPending ? undefined : handleReset}>
      <div className="modal-card killswitch-modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header danger-header">
          <h3>⚠️ KILL SWITCH — FLATTEN</h3>
          <button type="button" className="modal-close" onClick={handleReset} disabled={isPending}>
            ✕
          </button>
        </div>

        <div className="modal-body">
          <div className="killswitch-account-badge">
            <span className="dim">ACCOUNT:</span>
            <span className="mono bold">{ibkrAccount}</span>
          </div>

          {selectedOption === null ? (
            /* STEP 1: CHOICE MENU */
            <div className="killswitch-step-choice">
              <p className="killswitch-description" style={{ marginTop: 12 }}>
                Choose how much of this account you want to flatten:
              </p>

              <div className="killswitch-options-grid">
                {/* Option 1: Flatten Account */}
                <button
                  type="button"
                  className="killswitch-option-card danger-option"
                  onClick={() => setSelectedOption('account')}
                >
                  <div className="option-card-header">
                    <span className="option-title danger">Flatten Account</span>
                    <span className="option-badge danger font-bold">⚠️ ACCOUNT SCOPE</span>
                  </div>
                  <p className="option-desc">
                    Flatten all positions in this account.
                  </p>
                </button>

                {/* Option 2: Flatten Signal Positions */}
                <button
                  type="button"
                  className="killswitch-option-card primary-option"
                  onClick={() => setSelectedOption('engine')}
                >
                  <div className="option-card-header">
                    <span className="option-title">Flatten Signal Positions</span>
                    <span className="option-badge warning">SIGNAL SCOPE</span>
                  </div>
                  <p className="option-desc">
                    Flatten signal/engine positions only.
                  </p>
                </button>

                {/* Option 3: Flatten Manual Positions */}
                <button
                  type="button"
                  className="killswitch-option-card manual-option"
                  onClick={() => setSelectedOption('manual')}
                >
                  <div className="option-card-header">
                    <span className="option-title">Flatten Manual Positions</span>
                    <span className="option-badge info">MANUAL SCOPE</span>
                  </div>
                  <p className="option-desc">
                    Flatten manual positions only.
                  </p>
                </button>
              </div>
            </div>
          ) : selectedOption === 'account' ? (
            /* STEP 2A: CONFIRM FLATTEN ACCOUNT */
            <div className="killswitch-step-confirm">
              <div className="killswitch-scope-banner danger">
                <span>⚠️ Flatten Account</span>
              </div>

              <div className="killswitch-danger-alert">
                <p className="danger-alert-text">
                  This will flatten all positions in this account, including signal and manual positions.
                </p>
              </div>
              <p className="field-hint dim">
                This action executes an account-wide broker flatten sweep against IBKR and arms the account kill switch.
              </p>

              {error ? <p className="settings-msg err">{error}</p> : null}
            </div>
          ) : selectedOption === 'engine' ? (
            /* STEP 2B: CONFIRM FLATTEN SIGNAL POSITIONS */
            <div className="killswitch-step-confirm">
              <div className="killswitch-scope-banner warning">
                <span>Flatten Signal Positions</span>
              </div>

              <p className="killswitch-warning-text">
                This will flatten signal/engine positions only for account <strong>{ibkrAccount}</strong>. Manual positions will not be affected.
              </p>
              <p className="field-hint dim">
                This will submit CLOSE operations for engine pairs ({openCount} open pair{openCount === 1 ? '' : 's'}) through OMS &amp; IBKR and block new OPEN signals for this account.
              </p>

              {error ? <p className="settings-msg err">{error}</p> : null}
            </div>
          ) : (
            /* STEP 2C: CONFIRM FLATTEN MANUAL POSITIONS */
            <div className="killswitch-step-confirm">
              <div className="killswitch-scope-banner info">
                <span>Flatten Manual Positions</span>
              </div>

              <p className="killswitch-warning-text">
                This will flatten manual positions only. Signal/engine positions will not be affected.
              </p>
              <p className="field-hint dim">
                This will submit position-specific close orders for open manual positions and block new manual orders while flattening is active.
              </p>

              {error ? <p className="settings-msg err">{error}</p> : null}
            </div>
          )}
        </div>

        <div className="modal-footer">
          {selectedOption === null ? (
            <button type="button" className="btn" onClick={handleReset}>
              CANCEL
            </button>
          ) : (
            <>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setSelectedOption(null)
                  setError(null)
                }}
                disabled={isPending}
              >
                ← BACK
              </button>

              {selectedOption === 'account' ? (
                <button
                  type="button"
                  className="btn danger"
                  disabled={isPending}
                  onClick={() => accountMutation.mutate()}
                >
                  {accountMutation.isPending ? 'FLATTENING ACCOUNT…' : '⚠️ CONFIRM FLATTEN ACCOUNT'}
                </button>
              ) : selectedOption === 'engine' ? (
                <button
                  type="button"
                  className="btn warning"
                  disabled={isPending}
                  onClick={() => engineMutation.mutate()}
                >
                  {engineMutation.isPending ? 'FLATTENING SIGNAL POSITIONS…' : 'CONFIRM FLATTEN SIGNAL POSITIONS'}
                </button>
              ) : (
                <button
                  type="button"
                  className="btn warning"
                  disabled={isPending}
                  onClick={() => manualMutation.mutate()}
                >
                  {manualMutation.isPending ? 'FLATTENING MANUAL POSITIONS…' : 'CONFIRM FLATTEN MANUAL POSITIONS'}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
