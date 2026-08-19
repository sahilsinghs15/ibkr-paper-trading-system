import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import {
  checkAccountDeletable,
  createAccount,
  createAllocation,
  deleteAccount,
  deleteSymbolLimit,
  fetchAccountsConfig,
  fetchExecutionSettings,
  patchAccount,
  patchAllocation,
  patchExecutionSettings,
  putSymbolLimit,
} from '../api/configApi'
import type { AccountConfig, AccountDeleteCheck, ExecutionSettings } from '../types/config'
import {
  cleanNumberInput,
  displayStrategy,
  fmtPct,
  fmtUsd,
} from '../utils/format'

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

function AccountCard({ account }: { account: AccountConfig }) {
  const queryClient = useQueryClient()
  const [margin, setMargin] = useState(() => cleanNumberInput(account.total_margin))
  const [enabled, setEnabled] = useState(account.enabled)
  const [drafts, setDrafts] = useState<Record<number, AllocationDraft>>(() =>
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
  const [newSymbol, setNewSymbol] = useState('')
  const [newLimit, setNewLimit] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const enabledSum = useMemo(
    () =>
      Object.values(drafts).reduce(
        (sum, d) => sum + (d.enabled ? d.allocPct : 0),
        0,
      ),
    [drafts],
  )

  const accountMutation = useMutation({
    mutationFn: () =>
      patchAccount(account.id, {
        total_margin: parseFloat(margin) || undefined,
        enabled,
      }),
    onSuccess: () => {
      setMessage('Account saved.')
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
    },
    onError: (err: unknown) => {
      setError(extractError(err))
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
      setMessage('Allocation saved.')
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
    },
    onError: (err: unknown) => {
      setError(extractError(err))
      setMessage(null)
    },
  })

  const limitMutation = useMutation({
    mutationFn: ({ symbol, limit }: { symbol: string; limit: string }) =>
      putSymbolLimit(account.id, symbol, limit),
    onSuccess: () => {
      setNewSymbol('')
      setNewLimit('')
      setMessage('Symbol limit saved.')
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
    },
    onError: (err: unknown) => {
      setError(extractError(err))
      setMessage(null)
    },
  })

  const deleteLimitMutation = useMutation({
    mutationFn: (symbol: string) => deleteSymbolLimit(account.id, symbol),
    onSuccess: () => {
      setMessage('Symbol limit removed.')
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
    },
    onError: (err: unknown) => {
      setError(extractError(err))
      setMessage(null)
    },
  })

  function updateDraft(id: number, patch: Partial<AllocationDraft>) {
    setDrafts((prev) => ({
      ...prev,
      [id]: { ...prev[id], ...patch },
    }))
  }

  const sumOver = enabledSum > 100.0001
  const paperLabel =
    account.name.toLowerCase().includes('paper') ||
    account.ibkr_account.toUpperCase().startsWith('DU')
      ? 'Paper account'
      : 'Account'

  return (
    <section className="settings-card">
      <div className="settings-block">
        <div className="settings-block-h">
          <h2>Account</h2>
        </div>
        <div className="settings-card-head">
          <div>
            <div className="settings-kicker">{paperLabel}</div>
            <h3 className="settings-account-id">{account.ibkr_account}</h3>
            {account.name && account.name !== account.ibkr_account ? (
              <div className="dim">{account.name}</div>
            ) : null}
          </div>
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
            <span>Total margin</span>
            <div className="money-field">
              <span className="money-prefix">$</span>
              <input
                type="number"
                min="0"
                step="0.01"
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
            Save account
          </button>
        </div>
      </div>

      <div className="settings-block">
        <div className="settings-block-h">
          <h2>Strategy allocation</h2>
          <span className={`alloc-sum ${sumOver ? 'over' : ''}`}>
            Enabled total {fmtPct(enabledSum)}
          </span>
        </div>

        {account.allocations.length === 0 ? (
          <p className="empty">No strategy allocations.</p>
        ) : (
          account.allocations.map((alloc) => {
            const draft = drafts[alloc.id]
            if (!draft) return null
            const rowInvalid = enabledSum > 100.0001
            return (
              <div className="alloc-card" key={alloc.id}>
                <div className="alloc-card-h">
                  <h3>{displayStrategy(alloc.strategy_id)}</h3>
                  <label className="toggle-row">
                    <input
                      type="checkbox"
                      checked={draft.enabled}
                      onChange={(e) =>
                        updateDraft(alloc.id, { enabled: e.target.checked })
                      }
                    />
                    <span>{draft.enabled ? 'Enabled' : 'Disabled'}</span>
                  </label>
                </div>
                <div className="settings-grid">
                  <label className="field">
                    <span>Allocation</span>
                    <div className="money-field">
                      <input
                        className="inline-input"
                        type="number"
                        min="0"
                        max="100"
                        step="0.01"
                        value={draft.allocPct}
                        onChange={(e) =>
                          updateDraft(alloc.id, {
                            allocPct: parseFloat(e.target.value) || 0,
                          })
                        }
                      />
                      <span className="money-suffix">%</span>
                    </div>
                  </label>
                  <label className="field">
                    <span>Max open positions</span>
                    <input
                      className="inline-input narrow"
                      type="number"
                      min="0"
                      step="1"
                      value={draft.maxOpenPositions}
                      onChange={(e) =>
                        updateDraft(alloc.id, {
                          maxOpenPositions: parseInt(e.target.value, 10) || 0,
                        })
                      }
                    />
                  </label>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={allocationMutation.isPending || rowInvalid}
                    onClick={() =>
                      allocationMutation.mutate({ id: alloc.id, draft })
                    }
                  >
                    Save
                  </button>
                </div>
              </div>
            )
          })
        )}
      </div>

      <div className="settings-block">
        <div className="settings-block-h">
          <h2>Risk &amp; money limits</h2>
          <span
            className="field-hint"
            title="Caps how much capital can sit in one symbol."
          >
            Per symbol
          </span>
        </div>

        <div className="board">
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="right">Money limit</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {account.symbol_limits.map((lim) => (
                <tr key={lim.symbol}>
                  <td className="sym">{lim.symbol}</td>
                  <td className="right">{fmtUsd(lim.money_limit)}</td>
                  <td className="right">
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
              <tr>
                <td>
                  <input
                    className="inline-input"
                    placeholder="Symbol"
                    value={newSymbol}
                    onChange={(e) => setNewSymbol(e.target.value.toUpperCase())}
                  />
                </td>
                <td className="right">
                  <div className="money-field tight">
                    <span className="money-prefix">$</span>
                    <input
                      className="inline-input"
                      type="number"
                      min="0"
                      step="0.01"
                      placeholder="Limit"
                      value={newLimit}
                      onChange={(e) => setNewLimit(e.target.value)}
                    />
                  </div>
                </td>
                <td className="right">
                  <button
                    type="button"
                    className="btn primary"
                    disabled={
                      limitMutation.isPending || !newSymbol.trim() || !newLimit
                    }
                    onClick={() =>
                      limitMutation.mutate({
                        symbol: newSymbol.trim(),
                        limit: newLimit,
                      })
                    }
                  >
                    Add limit
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      {message ? <p className="settings-msg ok">{message}</p> : null}
      {error ? <p className="settings-msg err">{error}</p> : null}
    </section>
  )
}

function extractError(err: unknown): string {
  if (axiosIsError(err) && err.response?.data?.detail) {
    return String(err.response.data.detail)
  }
  if (err instanceof Error) return err.message
  return 'Request failed'
}

function axiosIsError(
  err: unknown,
): err is { response?: { data?: { detail?: string } } } {
  return typeof err === 'object' && err !== null && 'response' in err
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
          If all legs of a trade are not filled within the configured time, the
          system can retry missing quantity or square off filled exposure.
        </p>
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
                      update(
                        'square_off_after_sec',
                        parseInt(e.target.value, 10) || 0,
                      )
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
                  onChange={(e) =>
                    update('max_retries', parseInt(e.target.value, 10) || 0)
                  }
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
                      update(
                        'retry_interval_sec',
                        parseInt(e.target.value, 10) || 0,
                      )
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
                      update(
                        'retry_window_sec',
                        parseInt(e.target.value, 10) || 0,
                      )
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

function AddAccountModal({
  isOpen,
  onClose,
  onSuccess,
}: {
  isOpen: boolean
  onClose: () => void
  onSuccess: () => void
}) {
  const [name, setName] = useState('')
  const [ibkrAccount, setIbkrAccount] = useState('')
  const [margin, setMargin] = useState('100000')
  const [enabled, setEnabled] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => {
      const cleanName = name.trim()
      const cleanIbkr = ibkrAccount.trim().toUpperCase()
      const parsedMargin = parseFloat(margin)

      if (!cleanName) throw new Error('Account Name is required.')
      if (!cleanIbkr) throw new Error('IBKR Account identifier is required.')
      if (isNaN(parsedMargin) || parsedMargin <= 0) {
        throw new Error('Total Margin must be greater than 0.')
      }

      return createAccount({
        name: cleanName,
        ibkr_account: cleanIbkr,
        total_margin: parsedMargin,
        enabled,
      })
    },
    onSuccess: () => {
      setError(null)
      setName('')
      setIbkrAccount('')
      setMargin('100000')
      setEnabled(true)
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
          <h3>Add Paper Trading Account</h3>
          <button type="button" className="modal-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">
          <label className="field">
            <span>Account Name</span>
            <input
              type="text"
              placeholder="e.g. Paper Account 2"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="field">
            <span>IBKR Account Identifier</span>
            <input
              type="text"
              placeholder="e.g. DUR888999"
              value={ibkrAccount}
              onChange={(e) => setIbkrAccount(e.target.value)}
            />
            <span className="field-hint">Paper accounts typically start with DU</span>
          </label>
          <label className="field">
            <span>Total Margin (USD)</span>
            <div className="money-field">
              <span className="money-prefix">$</span>
              <input
                type="number"
                min="1"
                step="1000"
                value={margin}
                onChange={(e) => setMargin(e.target.value)}
              />
            </div>
            <span className="field-hint">{fmtUsd(margin)}</span>
          </label>
          <label className="toggle-row">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <span>{enabled ? 'Enabled' : 'Disabled'}</span>
          </label>
          {error ? <p className="settings-msg err">{error}</p> : null}
        </div>
        <div className="modal-footer">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            Create Account
          </button>
        </div>
      </div>
    </div>
  )
}

function EditAccountModal({
  account,
  onClose,
  onSuccess,
}: {
  account: AccountConfig | null
  onClose: () => void
  onSuccess: () => void
}) {
  const [name, setName] = useState('')
  const [ibkrAccount, setIbkrAccount] = useState('')
  const [margin, setMargin] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [hasHistory, setHasHistory] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (account) {
      setName(account.name)
      setIbkrAccount(account.ibkr_account)
      setMargin(cleanNumberInput(account.total_margin))
      setEnabled(account.enabled)
      setError(null)
      checkAccountDeletable(account.id)
        .then((res) => setHasHistory(res.has_history))
        .catch(() => setHasHistory(false))
    }
  }, [account])

  const mutation = useMutation({
    mutationFn: () => {
      if (!account) throw new Error('No account selected.')
      const cleanName = name.trim()
      const cleanIbkr = ibkrAccount.trim().toUpperCase()
      const parsedMargin = parseFloat(margin)

      if (!cleanName) throw new Error('Account Name is required.')
      if (!cleanIbkr) throw new Error('IBKR Account identifier is required.')
      if (isNaN(parsedMargin) || parsedMargin <= 0) {
        throw new Error('Total Margin must be greater than 0.')
      }

      return patchAccount(account.id, {
        name: cleanName,
        ibkr_account: hasHistory ? undefined : cleanIbkr,
        total_margin: parsedMargin,
        enabled,
      })
    },
    onSuccess: () => {
      setError(null)
      onSuccess()
      onClose()
    },
    onError: (err: unknown) => {
      setError(extractError(err))
    },
  })

  if (!account) return null

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Edit Account: {account.ibkr_account}</h3>
          <button type="button" className="modal-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">
          <label className="field">
            <span>Account Name</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="field">
            <span>IBKR Account Identifier</span>
            <input
              type="text"
              value={ibkrAccount}
              disabled={hasHistory}
              onChange={(e) => setIbkrAccount(e.target.value)}
            />
            {hasHistory ? (
              <span className="field-hint err" style={{ color: 'var(--amber)' }}>
                IBKR account identifier cannot be changed because this account has trading history.
              </span>
            ) : null}
          </label>
          <label className="field">
            <span>Total Margin (USD)</span>
            <div className="money-field">
              <span className="money-prefix">$</span>
              <input
                type="number"
                min="1"
                step="1000"
                value={margin}
                onChange={(e) => setMargin(e.target.value)}
              />
            </div>
            <span className="field-hint">{fmtUsd(margin)}</span>
          </label>
          <label className="toggle-row">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <span>{enabled ? 'Enabled' : 'Disabled'}</span>
          </label>
          {error ? <p className="settings-msg err">{error}</p> : null}
        </div>
        <div className="modal-footer">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            Save Changes
          </button>
        </div>
      </div>
    </div>
  )
}

function AddAllocationModal({
  account,
  onClose,
  onSuccess,
}: {
  account: AccountConfig | null
  onClose: () => void
  onSuccess: () => void
}) {
  const [strategyId, setStrategyId] = useState('model_blue')
  const [allocPct, setAllocPct] = useState('25')
  const [enabled, setEnabled] = useState(true)
  const [maxOpenPositions, setMaxOpenPositions] = useState('2')
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (account) {
      setStrategyId('model_blue')
      setAllocPct('25')
      setEnabled(true)
      setMaxOpenPositions('2')
      setError(null)
    }
  }, [account])

  const mutation = useMutation({
    mutationFn: () => {
      if (!account) throw new Error('No account selected.')
      const pct = parseFloat(allocPct)
      const cap = parseInt(maxOpenPositions, 10)

      if (isNaN(pct) || pct <= 0 || pct > 100) {
        throw new Error('Allocation percentage must be between 0.01% and 100%.')
      }
      if (isNaN(cap) || cap <= 0) {
        throw new Error('Max open positions cap must be at least 1.')
      }

      return createAllocation(account.id, {
        strategy_id: strategyId,
        alloc_pct: pct / 100,
        enabled,
        max_open_positions: cap,
      })
    },
    onSuccess: () => {
      setError(null)
      onSuccess()
      onClose()
    },
    onError: (err: unknown) => {
      setError(extractError(err))
    },
  })

  if (!account) return null

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Add Strategy Allocation</h3>
          <button type="button" className="modal-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">
          <label className="field">
            <span>Strategy</span>
            <select
              className="inline-input"
              value={strategyId}
              onChange={(e) => setStrategyId(e.target.value)}
            >
              <option value="model_blue">Model Blue</option>
            </select>
          </label>
          <label className="field">
            <span>Allocation Percentage</span>
            <div className="money-field">
              <input
                type="number"
                min="1"
                max="100"
                step="1"
                value={allocPct}
                onChange={(e) => setAllocPct(e.target.value)}
              />
              <span className="money-suffix">%</span>
            </div>
            <span className="field-hint">
              Committed Notional: {fmtUsd(String((parseFloat(account.total_margin) * (parseFloat(allocPct) || 0)) / 100))}
            </span>
          </label>
          <label className="field">
            <span>Max Open Positions</span>
            <input
              className="inline-input"
              type="number"
              min="1"
              step="1"
              value={maxOpenPositions}
              onChange={(e) => setMaxOpenPositions(e.target.value)}
            />
          </label>
          <label className="toggle-row">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <span>{enabled ? 'Enabled' : 'Disabled'}</span>
          </label>
          {error ? <p className="settings-msg err">{error}</p> : null}
        </div>
        <div className="modal-footer">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="btn primary"
            disabled={mutation.isPending}
            onClick={() => mutation.mutate()}
          >
            Save Allocation
          </button>
        </div>
      </div>
    </div>
  )
}

function DeleteAccountModal({
  account,
  onClose,
  onSuccess,
}: {
  account: AccountConfig | null
  onClose: () => void
  onSuccess: () => void
}) {
  const [check, setCheck] = useState<AccountDeleteCheck | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (account) {
      setLoading(true)
      setError(null)
      checkAccountDeletable(account.id)
        .then((res) => setCheck(res))
        .catch((err) => setError(extractError(err)))
        .finally(() => setLoading(false))
    }
  }, [account])

  const deleteMutation = useMutation({
    mutationFn: () => {
      if (!account) throw new Error('No account selected')
      return deleteAccount(account.id)
    },
    onSuccess: () => {
      onSuccess()
      onClose()
    },
    onError: (err: unknown) => {
      setError(extractError(err))
    },
  })

  const disableMutation = useMutation({
    mutationFn: () => {
      if (!account) throw new Error('No account selected')
      return patchAccount(account.id, { enabled: false })
    },
    onSuccess: () => {
      onSuccess()
      onClose()
    },
    onError: (err: unknown) => {
      setError(extractError(err))
    },
  })

  if (!account) return null

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>Delete Account: {account.ibkr_account}</h3>
          <button type="button" className="modal-close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="modal-body">
          {loading ? <p className="empty">Checking account dependencies…</p> : null}
          {!loading && check ? (
            check.can_delete ? (
              <p>
                This account <strong>({account.name} · {account.ibkr_account})</strong> has no
                trading history and can be permanently removed.
              </p>
            ) : (
              <p style={{ color: 'var(--amber)' }}>
                {check.reason ||
                  'Account deletion is unavailable because this account has trading history. Disable the account instead.'}
              </p>
            )
          ) : null}
          {error ? <p className="settings-msg err">{error}</p> : null}
        </div>
        <div className="modal-footer">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          {!loading && check?.can_delete ? (
            <button
              type="button"
              className="btn danger"
              disabled={deleteMutation.isPending}
              onClick={() => deleteMutation.mutate()}
            >
              Delete Account
            </button>
          ) : null}
          {!loading && check && !check.can_delete ? (
            <button
              type="button"
              className="btn danger"
              disabled={disableMutation.isPending}
              onClick={() => disableMutation.mutate()}
            >
              Disable Account
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}

function AccountManagementSection({
  accounts,
  onRefresh,
}: {
  accounts: AccountConfig[]
  onRefresh: () => void
}) {
  const [isAddOpen, setIsAddOpen] = useState(false)
  const [editingAccount, setEditingAccount] = useState<AccountConfig | null>(null)
  const [allocatingAccount, setAllocatingAccount] = useState<AccountConfig | null>(null)
  const [deletingAccount, setDeletingAccount] = useState<AccountConfig | null>(null)

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      patchAccount(id, { enabled }),
    onSuccess: () => {
      onRefresh()
    },
  })

  return (
    <section className="settings-card">
      <div className="settings-block">
        <div className="settings-block-h" style={{ alignItems: 'center' }}>
          <div>
            <h2>ACCOUNT MANAGEMENT</h2>
            <div className="settings-kicker" style={{ marginTop: 2 }}>
              Manage paper trading accounts and their trading configuration.
            </div>
          </div>
          <button
            type="button"
            className="btn primary"
            onClick={() => setIsAddOpen(true)}
          >
            + Add Account
          </button>
        </div>

        <div style={{ marginTop: 12, overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>ACCOUNT</th>
                <th>IBKR ACCOUNT</th>
                <th>STATUS</th>
                <th>MARGIN</th>
                <th>ALLOCATIONS</th>
                <th style={{ textAlign: 'right' }}>ACTIONS</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((acc) => {
                const allocSummary =
                  acc.allocations.length > 0
                    ? acc.allocations
                        .map(
                          (a) =>
                            `${displayStrategy(a.strategy_id)} · ${fmtPct(pctFromDecimal(a.alloc_pct))}${
                              a.enabled ? '' : ' (OFF)'
                            }`,
                        )
                        .join(', ')
                    : 'No Allocation'

                return (
                  <tr key={acc.id}>
                    <td style={{ fontWeight: 600 }}>{acc.name}</td>
                    <td className="mono">{acc.ibkr_account}</td>
                    <td>
                      <span
                        className={`account-status-pill ${
                          acc.enabled ? 'enabled' : 'disabled'
                        }`}
                      >
                        ● {acc.enabled ? 'ENABLED' : 'DISABLED'}
                      </span>
                    </td>
                    <td className="mono">{fmtUsd(acc.total_margin)}</td>
                    <td className="dim">{allocSummary}</td>
                    <td style={{ textAlign: 'right' }}>
                      <div
                        style={{
                          display: 'inline-flex',
                          gap: 6,
                          justifyContent: 'flex-end',
                        }}
                      >
                        <button
                          type="button"
                          className="btn"
                          onClick={() => setEditingAccount(acc)}
                        >
                          Edit
                        </button>
                        <button
                          type="button"
                          className="btn"
                          onClick={() =>
                            toggleMutation.mutate({ id: acc.id, enabled: !acc.enabled })
                          }
                        >
                          {acc.enabled ? 'Disable' : 'Enable'}
                        </button>
                        <button
                          type="button"
                          className="btn"
                          onClick={() => setAllocatingAccount(acc)}
                        >
                          + Allocation
                        </button>
                        <button
                          type="button"
                          className="btn danger"
                          onClick={() => setDeletingAccount(acc)}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
              {accounts.length === 0 ? (
                <tr>
                  <td colSpan={6} className="empty">
                    No accounts configured.
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>

      <AddAccountModal
        isOpen={isAddOpen}
        onClose={() => setIsAddOpen(false)}
        onSuccess={onRefresh}
      />
      <EditAccountModal
        account={editingAccount}
        onClose={() => setEditingAccount(null)}
        onSuccess={onRefresh}
      />
      <AddAllocationModal
        account={allocatingAccount}
        onClose={() => setAllocatingAccount(null)}
        onSuccess={onRefresh}
      />
      <DeleteAccountModal
        account={deletingAccount}
        onClose={() => setDeletingAccount(null)}
        onSuccess={onRefresh}
      />
    </section>
  )
}

export function SettingsPage() {
  const queryClient = useQueryClient()
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['config', 'accounts'],
    queryFn: fetchAccountsConfig,
  })

  function handleRefresh() {
    void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
  }

  return (
    <main className="page settings-page">
      <AccountManagementSection
        accounts={data?.accounts ?? []}
        onRefresh={handleRefresh}
      />
      <ExecutionSettingsCard />
      {isLoading ? <p className="empty">Loading configuration…</p> : null}
      {isError ? (
        <p className="settings-msg err">
          {extractError(error)}{' '}
          <button type="button" className="btn" onClick={() => void refetch()}>
            Retry
          </button>
        </p>
      ) : null}
      {data?.accounts.map((account) => (
        <AccountCard key={account.id} account={account} />
      ))}
    </main>
  )
}

