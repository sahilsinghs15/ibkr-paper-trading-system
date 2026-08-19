import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import {
  deleteSymbolLimit,
  fetchAccountsConfig,
  patchAccount,
  patchAllocation,
  putSymbolLimit,
} from '../api/configApi'
import type { AccountConfig, AllocationConfig } from '../types/config'
import { AppNav } from '../components/AppNav'

function pctFromDecimal(value: string): number {
  return Math.round(parseFloat(value) * 10000) / 100
}

function decimalFromPct(pct: number): string {
  return (pct / 100).toFixed(4)
}

function enabledAllocSum(allocations: AllocationConfig[]): number {
  return allocations.reduce(
    (sum, a) => sum + (a.enabled ? pctFromDecimal(a.alloc_pct) : 0),
    0,
  )
}

interface AllocationDraft {
  allocPct: number
  enabled: boolean
  maxOpenPositions: number
}

function AccountCard({ account }: { account: AccountConfig }) {
  const queryClient = useQueryClient()
  const [margin, setMargin] = useState(account.total_margin)
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
        total_margin: margin,
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

  return (
    <section className="settings-card">
      <div className="settings-card-head">
        <div>
          <h2>{account.name}</h2>
          <span className="acct">{account.ibkr_account}</span>
        </div>
        <label className="toggle-row">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Account enabled
        </label>
      </div>

      <div className="settings-grid">
        <label className="field">
          <span>Total margin</span>
          <input
            type="number"
            min="0"
            step="0.01"
            value={margin}
            onChange={(e) => setMargin(e.target.value)}
          />
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

      <div className="section-h">
        <h2>Strategy allocations</h2>
        <span className={`alloc-sum ${sumOver ? 'over' : ''}`}>
          Enabled sum: {enabledSum.toFixed(2)}%
        </span>
      </div>

      <div className="board">
        <table>
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Alloc %</th>
              <th>Max open</th>
              <th>Enabled</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {account.allocations.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty">
                  No allocations
                </td>
              </tr>
            ) : (
              account.allocations.map((alloc) => {
                const draft = drafts[alloc.id]
                if (!draft) return null
                const rowSum =
                  enabledSum -
                  (draft.enabled ? draft.allocPct : 0) +
                  (draft.enabled ? draft.allocPct : 0)
                const rowInvalid = rowSum > 100.0001
                return (
                  <tr key={alloc.id}>
                    <td className="sym">{alloc.strategy_id}</td>
                    <td>
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
                    </td>
                    <td>
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
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        checked={draft.enabled}
                        onChange={(e) =>
                          updateDraft(alloc.id, { enabled: e.target.checked })
                        }
                      />
                    </td>
                    <td className="right">
                      <button
                        type="button"
                        className="btn"
                        disabled={allocationMutation.isPending || rowInvalid}
                        onClick={() =>
                          allocationMutation.mutate({ id: alloc.id, draft })
                        }
                      >
                        Save
                      </button>
                    </td>
                  </tr>
                )
              })
            )}
          </tbody>
        </table>
      </div>

      <div className="section-h">
        <h2>Per-symbol money limits (RMS check 8)</h2>
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
                <td className="right">{lim.money_limit}</td>
                <td className="right">
                  <button
                    type="button"
                    className="btn danger"
                    disabled={deleteLimitMutation.isPending}
                    onClick={() => deleteLimitMutation.mutate(lim.symbol)}
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
            <tr>
              <td>
                <input
                  className="inline-input"
                  placeholder="SYMBOL"
                  value={newSymbol}
                  onChange={(e) => setNewSymbol(e.target.value.toUpperCase())}
                />
              </td>
              <td className="right">
                <input
                  className="inline-input"
                  type="number"
                  min="0"
                  step="0.01"
                  placeholder="Limit"
                  value={newLimit}
                  onChange={(e) => setNewLimit(e.target.value)}
                />
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
                  Add
                </button>
              </td>
            </tr>
          </tbody>
        </table>
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

export function SettingsPage() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['config', 'accounts'],
    queryFn: fetchAccountsConfig,
  })

  return (
    <>
      <header className="settings-header">
        <div className="brand">
          <h1>CONFIG</h1>
          <span className="tag">RMS &amp; ALLOCATIONS</span>
        </div>
        <AppNav />
      </header>
      <main className="settings-main">
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
        {data && data.accounts.length === 0 ? (
          <p className="empty">No accounts configured in Postgres.</p>
        ) : null}
      </main>
    </>
  )
}

// Reference baseline sum helper for tests / lint (account-level validation display)
export function accountEnabledPct(account: AccountConfig): number {
  return enabledAllocSum(account.allocations)
}
