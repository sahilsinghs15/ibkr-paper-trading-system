import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import {
  checkAccountDeletable,
  createAccount,
  createAllocation,
  deleteAccount,
  fetchAccountsConfig,
  patchAccount,
} from '../api/configApi'
import type { AccountConfig, AccountDeleteCheck } from '../types/config'
import { writeLastIbkrAccount } from '../utils/activeAccount'
import { displayStrategy, fmtPct, fmtUsd } from '../utils/format'

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
      setMargin(account.total_margin)
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
  const [maxOpenPositions, setMaxOpenPositions] = useState('200')
  const [error, setError] = useState<string | null>(null)

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

export function AccountsPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['config', 'accounts'],
    queryFn: fetchAccountsConfig,
  })

  const [isAddOpen, setIsAddOpen] = useState(false)
  const [editingAccount, setEditingAccount] = useState<AccountConfig | null>(null)
  const [allocatingAccount, setAllocatingAccount] = useState<AccountConfig | null>(null)
  const [deletingAccount, setDeletingAccount] = useState<AccountConfig | null>(null)

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      patchAccount(id, { enabled }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
    },
  })

  function handleRefresh() {
    void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
  }

  return (
    <main className="page settings-page">
      <section className="settings-card">
        <div className="settings-block">
          <div className="settings-block-h" style={{ alignItems: 'center' }}>
            <div>
              <h2>GLOBAL ACCOUNT MANAGEMENT</h2>
              <div className="settings-kicker" style={{ marginTop: 2 }}>
                Manage paper trading accounts and their strategy allocations.
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

          {isLoading ? <p className="empty">Loading accounts…</p> : null}
          {isError ? (
            <p className="settings-msg err">
              {extractError(error)}{' '}
              <button type="button" className="btn" onClick={() => void refetch()}>
                Retry
              </button>
            </p>
          ) : null}

          {data ? (
            <div style={{ marginTop: 12, overflowX: 'auto' }}>
              <table className="accounts-table">
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
                  {data.accounts.map((acc) => {
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
                          <div className="accounts-actions">
                            <button
                              type="button"
                              className="btn primary"
                              onClick={() => {
                                writeLastIbkrAccount(acc.ibkr_account)
                                navigate(`/account/${acc.ibkr_account}`)
                              }}
                            >
                              View
                            </button>
                            <Link
                              to={`/account/${acc.ibkr_account}/settings`}
                              className="btn"
                              onClick={() => writeLastIbkrAccount(acc.ibkr_account)}
                            >
                              Settings
                            </Link>
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
                  {data.accounts.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="empty">
                        No paper accounts configured. Click "+ Add Account" to create one.
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      </section>

      <AddAccountModal
        isOpen={isAddOpen}
        onClose={() => setIsAddOpen(false)}
        onSuccess={handleRefresh}
      />
      <EditAccountModal
        account={editingAccount}
        onClose={() => setEditingAccount(null)}
        onSuccess={handleRefresh}
      />
      <AddAllocationModal
        account={allocatingAccount}
        onClose={() => setAllocatingAccount(null)}
        onSuccess={handleRefresh}
      />
      <DeleteAccountModal
        account={deletingAccount}
        onClose={() => setDeletingAccount(null)}
        onSuccess={handleRefresh}
      />
    </main>
  )
}
