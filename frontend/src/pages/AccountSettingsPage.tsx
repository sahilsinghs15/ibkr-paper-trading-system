import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import {
  deleteSymbolLimit,
  fetchAccountByIdentifier,
  fetchExecutionSettings,
  patchAccount,
  patchAllocation,
  patchExecutionSettings,
  putSymbolLimit,
} from '../api/configApi'
import { KillSwitchModal } from '../components/KillSwitchModal'
import { usePnlStore } from '../store/pnlStore'
import type { ExecutionSettings } from '../types/config'
import {
  cleanNumberInput,
  displayStrategy,
  fmtPct,
  fmtUsd,
} from '../utils/format'

function extractError(err: unknown): string {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const res = (err as { response?: { data?: { detail?: string } } }).response
    if (res?.data?.detail) return String(res.data.detail)
  }
  if (err instanceof Error) return err.message
  return 'Request failed'
}

function pctFromDecimal(value: string): number {
  return Math.round(parseFloat(value) * 10000) / 100
}

function decimalFromPct(pct: number): string {
  return (pct / 100).toFixed(4)
}

interface AllocationDraft {
  allocPct: number
  enabled: boolean
  maxOpenPositions: number
}

function executionSummary(s: {
  square_off_after_sec: number
  max_retries: number
  retry_interval_sec: number
  retry_window_sec: number
}): string {
  return `After ${s.square_off_after_sec}s → retry up to ${s.max_retries} times → every ${s.retry_interval_sec}s → stop after ${s.retry_window_sec}s.`
}

function ExecutionSettingsCard() {
  const queryClient = useQueryClient()
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['config', 'execution'],
    queryFn: fetchExecutionSettings,
  })
  const [draft, setDraft] = useState<ExecutionSettings | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)

  useEffect(() => {
    if (data) setDraft(data)
  }, [data])

  const mutation = useMutation({
    mutationFn: () => {
      if (!draft) throw new Error('No draft')
      if (draft.square_off_after_sec <= 0) {
        throw new Error('Square-off timeout must be greater than 0.')
      }
      if (draft.max_retries < 0) {
        throw new Error('Maximum retries must be 0 or more.')
      }
      if (draft.retry_interval_sec <= 0) {
        throw new Error('Retry interval must be greater than 0.')
      }
      if (draft.retry_window_sec < draft.retry_interval_sec) {
        throw new Error('Retry window must be at least the retry interval.')
      }
      return patchExecutionSettings({
        enabled: draft.enabled,
        square_off_after_sec: draft.square_off_after_sec,
        max_retries: draft.max_retries,
        retry_interval_sec: draft.retry_interval_sec,
        retry_window_sec: draft.retry_window_sec,
      })
    },
    onSuccess: (saved) => {
      setDraft(saved)
      setMessage('Auto square-off settings saved.')
      setLocalError(null)
      void queryClient.invalidateQueries({ queryKey: ['config', 'execution'] })
    },
    onError: (err: unknown) => {
      setLocalError(extractError(err))
      setMessage(null)
    },
  })

  function update<K extends keyof ExecutionSettings>(key: K, value: ExecutionSettings[K]) {
    setDraft((prev) => (prev ? { ...prev, [key]: value } : prev))
  }

  return (
    <section className="settings-card">
      <div className="settings-block">
        <div className="settings-block-h">
          <h2>AUTO SQUARE-OFF &amp; RETRY</h2>
          <label className="toggle-row">
            <input
              type="checkbox"
              checked={draft?.enabled ?? true}
              onChange={(e) => update('enabled', e.target.checked)}
              disabled={!draft}
            />
            <span>{draft?.enabled ? 'Enabled' : 'Disabled'}</span>
          </label>
        </div>
        <p className="field-hint">
          If all legs of a trade are not filled within the configured time, the system will retry
          unfilled leg quantities before squaring off exposure.
        </p>

        {draft ? (
          <div className="execution-pipeline-badge">
            ⚡ <strong>Execution Pipeline:</strong> {executionSummary(draft)}
          </div>
        ) : null}

        {data && !data.paper_retries_active ? (
          <p className="settings-msg err">
            Retries apply on paper TWS/Gateway ports only (7497 / 4002).
          </p>
        ) : null}
        {isLoading ? <p className="empty">Loading…</p> : null}
        {isError ? (
          <p className="settings-msg err">
            {extractError(error)}{' '}
            <button type="button" className="btn" onClick={() => void refetch()}>
              Retry
            </button>
          </p>
        ) : null}
        {draft ? (
          <>
            <div className="settings-grid">
              <label className="field">
                <span>Square off after</span>
                <div className="money-field">
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={draft.square_off_after_sec}
                    onChange={(e) =>
                      update('square_off_after_sec', parseInt(e.target.value, 10) || 0)
                    }
                  />
                  <span className="money-suffix">sec</span>
                </div>
              </label>
              <label className="field">
                <span>Maximum retries</span>
                <input
                  className="inline-input narrow"
                  type="number"
                  min="0"
                  step="1"
                  value={draft.max_retries}
                  onChange={(e) => update('max_retries', parseInt(e.target.value, 10) || 0)}
                />
              </label>
              <label className="field">
                <span>Retry every</span>
                <div className="money-field">
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={draft.retry_interval_sec}
                    onChange={(e) =>
                      update('retry_interval_sec', parseInt(e.target.value, 10) || 0)
                    }
                  />
                  <span className="money-suffix">sec</span>
                </div>
              </label>
              <label className="field">
                <span>Retry window</span>
                <div className="money-field">
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={draft.retry_window_sec}
                    onChange={(e) =>
                      update('retry_window_sec', parseInt(e.target.value, 10) || 0)
                    }
                  />
                  <span className="money-suffix">sec</span>
                </div>
              </label>
              <button
                type="button"
                className="btn primary"
                disabled={mutation.isPending}
                onClick={() => mutation.mutate()}
              >
                Save
              </button>
            </div>
            <p className="field-hint" style={{ marginTop: 8 }}>
              {executionSummary(draft)}
            </p>
          </>
        ) : null}
        {message ? <p className="settings-msg ok">{message}</p> : null}
        {localError ? <p className="settings-msg err">{localError}</p> : null}
      </div>
    </section>
  )
}

export function AccountSettingsPage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const cleanAccount = (ibkrAccount || 'DUR919062').trim().toUpperCase()

  const queryClient = useQueryClient()
  const { data: account, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['config', 'account', cleanAccount],
    queryFn: () => fetchAccountByIdentifier(cleanAccount),
  })

  const [margin, setMargin] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [drafts, setDrafts] = useState<Record<number, AllocationDraft>>({})
  const [newSymbol, setNewSymbol] = useState('')
  const [newLimit, setNewLimit] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)
  const [isKillSwitchOpen, setIsKillSwitchOpen] = useState(false)

  // Query active store positions count for Kill Switch modal
  const activeMap = usePnlStore((s) => s.active)
  const accountOpenPositionsCount = useMemo(() => {
    if (!account) return 0
    let count = 0
    for (const leg of Object.values(activeMap)) {
      if (
        String(leg.account_id) === String(account.id) ||
        String(leg.ibkr_account || '').toUpperCase() === cleanAccount
      ) {
        count += 1
      }
    }
    return count
  }, [activeMap, account, cleanAccount])

  useEffect(() => {
    if (account) {
      setMargin(cleanNumberInput(account.total_margin))
      setEnabled(account.enabled)
      setDrafts(
        Object.fromEntries(
          account.allocations.map((a) => [
            a.id,
            {
              allocPct: pctFromDecimal(a.alloc_pct),
              enabled: a.enabled,
              maxOpenPositions: a.max_open_positions,
            },
          ]),
        ),
      )
    }
  }, [account])

  const accountMutation = useMutation({
    mutationFn: () => {
      if (!account) throw new Error('Account not loaded')
      return patchAccount(account.id, {
        total_margin: parseFloat(margin) || undefined,
        enabled,
      })
    },
    onSuccess: () => {
      setMessage('Account configuration saved.')
      setLocalError(null)
      void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
      void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
    },
    onError: (err: unknown) => {
      setLocalError(extractError(err))
      setMessage(null)
    },
  })

  const allocationMutation = useMutation({
    mutationFn: ({ id, draft }: { id: number; draft: AllocationDraft }) =>
      patchAllocation(id, {
        alloc_pct: decimalFromPct(draft.allocPct),
        enabled: draft.enabled,
        max_open_positions: draft.maxOpenPositions,
      }),
    onSuccess: () => {
      setMessage('Strategy allocation saved.')
      setLocalError(null)
      void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
      void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
    },
    onError: (err: unknown) => {
      setLocalError(extractError(err))
      setMessage(null)
    },
  })

  const limitMutation = useMutation({
    mutationFn: ({ symbol, limit }: { symbol: string; limit: string }) => {
      if (!account) throw new Error('Account not loaded')
      return putSymbolLimit(account.id, symbol, limit)
    },
    onSuccess: () => {
      setNewSymbol('')
      setNewLimit('')
      setMessage('Symbol limit saved.')
      setLocalError(null)
      void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
    },
    onError: (err: unknown) => {
      setLocalError(extractError(err))
      setMessage(null)
    },
  })

  const deleteLimitMutation = useMutation({
    mutationFn: (symbol: string) => {
      if (!account) throw new Error('Account not loaded')
      return deleteSymbolLimit(account.id, symbol)
    },
    onSuccess: () => {
      setMessage('Symbol limit removed.')
      setLocalError(null)
      void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
    },
    onError: (err: unknown) => {
      setLocalError(extractError(err))
      setMessage(null)
    },
  })

  function updateDraft(id: number, patch: Partial<AllocationDraft>) {
    setDrafts((prev) => ({
      ...prev,
      [id]: { ...prev[id], ...patch },
    }))
  }

  const enabledSum = useMemo(
    () =>
      Object.values(drafts).reduce(
        (sum, d) => sum + (d.enabled ? d.allocPct : 0),
        0,
      ),
    [drafts],
  )
  const sumOver = enabledSum > 100.0001

  return (
    <main className="page settings-page">
      <header className="settings-header-banner">
        <div className="settings-header-title">
          <h1>MODEL BLUE ACCOUNT SETTINGS</h1>
          <div className="settings-header-meta">
            <span>Account: <strong className="mono">{cleanAccount}</strong></span>
            <span>·</span>
            <span className="paper-pill">PAPER</span>
            {account ? (
              <span className={`account-status-pill ${enabled ? 'enabled' : 'disabled'}`}>
                ● {enabled ? 'ENABLED' : 'DISABLED'}
              </span>
            ) : null}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <Link to={`/account/${cleanAccount}`} className="btn primary">
            View Dashboard →
          </Link>
        </div>
      </header>

      {isLoading ? <p className="empty">Loading configuration for {cleanAccount}…</p> : null}
      {isError ? (
        <p className="settings-msg err">
          {extractError(error)}{' '}
          <button type="button" className="btn" onClick={() => void refetch()}>
            Retry
          </button>
        </p>
      ) : null}

      {account ? (
        <div className="settings-dashboard-grid">
          <div className="settings-column">
            {/* Account Margin & Enable Configuration */}
            <section className="settings-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>ACCOUNT CONFIGURATION</h2>
                  <label className="toggle-row">
                    <input
                      type="checkbox"
                      checked={enabled}
                      onChange={(e) => setEnabled(e.target.checked)}
                    />
                    <span>{enabled ? 'Enabled' : 'Disabled'}</span>
                  </label>
                </div>

                <div className="settings-grid">
                  <label className="field">
                    <span>Account Name</span>
                    <input className="inline-input" type="text" value={account.name} disabled />
                  </label>

                  <label className="field">
                    <span>Total Margin</span>
                    <div className="money-field">
                      <span className="money-prefix">$</span>
                      <input
                        type="number"
                        min="0"
                        step="1000"
                        value={margin}
                        onChange={(e) => setMargin(e.target.value)}
                      />
                    </div>
                    <span className="field-hint">{fmtUsd(margin)}</span>
                  </label>

                  <button
                    type="button"
                    className="btn primary"
                    disabled={accountMutation.isPending}
                    onClick={() => accountMutation.mutate()}
                  >
                    Save Changes
                  </button>
                </div>
              </div>
            </section>

            {/* Strategy Allocations */}
            {account.allocations.map((a) => {
              const draft = drafts[a.id] || {
                allocPct: pctFromDecimal(a.alloc_pct),
                enabled: a.enabled,
                maxOpenPositions: a.max_open_positions,
              }
              const committed =
                (parseFloat(account.total_margin) * (draft.enabled ? draft.allocPct : 0)) / 100

              return (
                <section key={a.id} className="settings-card">
                  <div className="settings-block">
                    <div className="settings-block-h">
                      <h2>MODEL BLUE ALLOCATION</h2>
                      <span className={`alloc-sum ${sumOver ? 'over' : ''}`}>
                        Enabled total {fmtPct(enabledSum)}
                      </span>
                    </div>

                    <div className="alloc-card">
                      <div className="alloc-card-h">
                        <h3>{displayStrategy(a.strategy_id)}</h3>
                        <label className="toggle-row">
                          <input
                            type="checkbox"
                            checked={draft.enabled}
                            onChange={(e) => updateDraft(a.id, { enabled: e.target.checked })}
                          />
                          <span>{draft.enabled ? 'Enabled' : 'Disabled'}</span>
                        </label>
                      </div>

                      <div className="settings-grid">
                        <label className="field">
                          <span>Allocation</span>
                          <div className="money-field">
                            <input
                              type="number"
                              min="0"
                              max="100"
                              step="1"
                              value={draft.allocPct}
                              onChange={(e) =>
                                updateDraft(a.id, { allocPct: parseFloat(e.target.value) || 0 })
                              }
                            />
                            <span className="money-suffix">%</span>
                          </div>
                          <span className="field-hint">
                            Committed: {fmtUsd(String(committed))}
                          </span>
                        </label>

                        <label className="field">
                          <span>Max Open Positions</span>
                          <input
                            className="inline-input narrow"
                            type="number"
                            min="1"
                            step="1"
                            value={draft.maxOpenPositions}
                            onChange={(e) =>
                              updateDraft(a.id, {
                                maxOpenPositions: parseInt(e.target.value, 10) || 1,
                              })
                            }
                          />
                        </label>

                        <button
                          type="button"
                          className="btn primary"
                          disabled={allocationMutation.isPending}
                          onClick={() => allocationMutation.mutate({ id: a.id, draft })}
                        >
                          Save Allocation
                        </button>
                      </div>
                    </div>
                  </div>
                </section>
              )
            })}

            {/* Auto Square-Off & Retry */}
            <ExecutionSettingsCard />
          </div>

          <div className="settings-column">
            {/* Per-Symbol Money Limits */}
            <section className="settings-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>PER-SYMBOL MONEY LIMITS</h2>
                </div>
                <p className="field-hint">
                  Limits max notional across all positions for a specific symbol on account{' '}
                  <span className="mono">{cleanAccount}</span>.
                </p>

                <div style={{ marginTop: 12, overflowX: 'auto' }}>
                  <table>
                    <thead>
                      <tr>
                        <th>SYMBOL</th>
                        <th>MONEY LIMIT</th>
                        <th style={{ textAlign: 'right' }}>ACTION</th>
                      </tr>
                    </thead>
                    <tbody>
                      {account.symbol_limits.map((lim) => (
                        <tr key={lim.symbol}>
                          <td className="mono bold">{lim.symbol}</td>
                          <td className="mono">{fmtUsd(lim.money_limit)}</td>
                          <td style={{ textAlign: 'right' }}>
                            <button
                              type="button"
                              className="btn danger"
                              disabled={deleteLimitMutation.isPending}
                              onClick={() => deleteLimitMutation.mutate(lim.symbol)}
                            >
                              Remove
                            </button>
                          </td>
                        </tr>
                      ))}
                      {account.symbol_limits.length === 0 ? (
                        <tr>
                          <td colSpan={3} className="empty">
                            No symbol limits configured.
                          </td>
                        </tr>
                      ) : null}
                    </tbody>
                  </table>
                </div>

                <div className="settings-grid" style={{ marginTop: 12 }}>
                  <label className="field">
                    <span>Symbol</span>
                    <input
                      className="inline-input"
                      type="text"
                      placeholder="e.g. SIL"
                      value={newSymbol}
                      onChange={(e) => setNewSymbol(e.target.value.toUpperCase())}
                    />
                  </label>
                  <label className="field">
                    <span>Money Limit ($)</span>
                    <div className="money-field">
                      <span className="money-prefix">$</span>
                      <input
                        type="number"
                        min="1"
                        step="1000"
                        placeholder="25000"
                        value={newLimit}
                        onChange={(e) => setNewLimit(e.target.value)}
                      />
                    </div>
                  </label>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={!newSymbol.trim() || !newLimit.trim() || limitMutation.isPending}
                    onClick={() =>
                      limitMutation.mutate({
                        symbol: newSymbol.trim(),
                        limit: newLimit.trim(),
                      })
                    }
                  >
                    + Add Limit
                  </button>
                </div>
              </div>
            </section>

            {/* Emergency / Kill Switch */}
            <section className="settings-card danger-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>EMERGENCY / KILL SWITCH</h2>
                </div>
                <p className="field-hint" style={{ color: 'var(--ink)' }}>
                  Close every currently open position for account{' '}
                  <span className="mono bold">{cleanAccount}</span>.
                </p>

                <div style={{ marginTop: 12 }}>
                  <button
                    type="button"
                    className="btn danger"
                    style={{ padding: '10px 16px', fontSize: '11px' }}
                    onClick={() => setIsKillSwitchOpen(true)}
                  >
                    ⚠️ SQUARE OFF ALL POSITIONS
                  </button>
                </div>
              </div>
            </section>
          </div>
        </div>
      ) : null}

      {message ? <p className="settings-msg ok">{message}</p> : null}
      {localError ? <p className="settings-msg err">{localError}</p> : null}

      {account ? (
        <KillSwitchModal
          isOpen={isKillSwitchOpen}
          accountId={account.id}
          ibkrAccount={account.ibkr_account}
          openCount={accountOpenPositionsCount}
          onClose={() => setIsKillSwitchOpen(false)}
          onSuccess={(closedCount) => {
            setMessage(`Kill Switch executed: squared off ${closedCount} position(s).`)
            setLocalError(null)
            void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
          }}
        />
      ) : null}
    </main>
  )
}
