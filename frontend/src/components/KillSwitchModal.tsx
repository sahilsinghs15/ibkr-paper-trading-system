import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import {
  squareOffEnginePositions,
  squareOffEntireAccount,
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

type FlattenOption = 'engine' | 'account' | null

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
      onSuccess(res.squared_off_count, 'ENGINE_POSITION_FLATTEN')
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
        onSuccess(res.squared_off_count, 'ACCOUNT_POSITION_FLATTEN')
        handleReset()
      }
    },
    onError: (err: unknown) => {
      setError(extractError(err))
    },
  })

  if (!isOpen) return null

  const isPending = engineMutation.isPending || accountMutation.isPending

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
                {/* Option 1: Engine Positions */}
                <button
                  type="button"
                  className="killswitch-option-card primary-option"
                  onClick={() => setSelectedOption('engine')}
                >
                  <div className="option-card-header">
                    <span className="option-title">Close All Engine Positions</span>
                    <span className="option-badge warning">ENGINE SCOPE</span>
                  </div>
                  <p className="option-desc">
                    Close only positions managed by the trading engine ({openCount} open pair{openCount === 1 ? '' : 's'}).
                  </p>
                </button>

                {/* Option 2: Entire Account Positions */}
                <button
                  type="button"
                  className="killswitch-option-card danger-option"
                  onClick={() => setSelectedOption('account')}
                >
                  <div className="option-card-header">
                    <span className="option-title danger">Close All Positions of Account</span>
                    <span className="option-badge danger font-bold">⚠️ FULL IBKR ACCOUNT</span>
                  </div>
                  <p className="option-desc">
                    Flatten the entire IBKR account, including positions not created by this trading system.
                  </p>
                </button>
              </div>
            </div>
          ) : selectedOption === 'engine' ? (
            /* STEP 2A: CONFIRM ENGINE FLATTEN */
            <div className="killswitch-step-confirm">
              <div className="killswitch-scope-banner warning">
                <span>SCOPE: ENGINE POSITIONS ONLY</span>
              </div>

              <p className="killswitch-warning-text">
                You are about to close all <strong>{openCount}</strong> open position{openCount === 1 ? '' : 's'} managed by the trading engine for account <strong>{ibkrAccount}</strong>.
              </p>
              <p className="field-hint dim">
                This will submit CLOSE operations through OMS &amp; IBKR and block new OPEN signals for this account.
              </p>

              {error ? <p className="settings-msg err">{error}</p> : null}
            </div>
          ) : (
            /* STEP 2B: CONFIRM ENTIRE ACCOUNT FLATTEN (DANGER STEP) */
            <div className="killswitch-step-confirm">
              <div className="killswitch-scope-banner danger">
                <span>⚠️ SCOPE: ENTIRE IBKR ACCOUNT</span>
              </div>

              <div className="killswitch-danger-alert">
                <p className="danger-alert-text">
                  <strong>WARNING:</strong> This will attempt to close <strong>ALL</strong> positions in the selected IBKR account (<strong>{ibkrAccount}</strong>), including positions that were not created by the trading engine.
                </p>
              </div>
              <p className="field-hint dim">
                This action executes a direct broker flatten sweep against IBKR and arms the account kill switch.
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

              {selectedOption === 'engine' ? (
                <button
                  type="button"
                  className="btn warning"
                  disabled={isPending}
                  onClick={() => engineMutation.mutate()}
                >
                  {engineMutation.isPending ? 'FLATTENING ENGINE POSITIONS…' : 'CONFIRM & CLOSE ENGINE POSITIONS'}
                </button>
              ) : (
                <button
                  type="button"
                  className="btn danger"
                  disabled={isPending}
                  onClick={() => accountMutation.mutate()}
                >
                  {accountMutation.isPending ? 'FLATTENING ACCOUNT POSITIONS…' : '⚠️ CONFIRM & FLATTEN ALL ACCOUNT POSITIONS'}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
