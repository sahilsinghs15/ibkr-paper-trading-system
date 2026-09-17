import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import {
  deleteSymbolLimit,
  fetchAccountByIdentifier,
  fetchExecutionSettings,
  fetchKillSwitchStatus,
  fetchMarginSettings,
  fetchTradingPause,
  patchAccount,
  patchExecutionSettings,
  patchMarginSettings,
  pauseTrading,
  putSymbolLimit,
  resumeTrading,
  updateDefaultSymbolLimit,
} from '../api/configApi'
import { fetchAccountMargin } from '../api/marginApi'
import { KillSwitchModal } from '../components/KillSwitchModal'
import { StartAgainModal } from '../components/StartAgainModal'
import { usePnlStore } from '../store/pnlStore'
import type { ExecutionSettings, MarginSettings } from '../types/config'
import { normalizeIbkrAccount } from '../utils/activeAccount'
import { showFeedbackToast } from '../utils/feedbackToast'
import { cleanNumberInput, fmtUsd } from '../utils/format'

function extractError(err: unknown): string {
  if (typeof err === 'object' && err !== null && 'response' in err) {
    const res = (err as { response?: { data?: { detail?: string } } }).response
    if (res?.data?.detail) return String(res.data.detail)
  }
  if (err instanceof Error) return err.message
  return 'Request failed'
}

function thresholdInputValue(
  raw: string | number | null | undefined,
  unit: string,
): string {
  if (raw === null || raw === undefined || raw === '') return '0'
  const n = parseFloat(String(raw))
  if (Number.isNaN(n)) return '0'
  if (unit === 'PERCENT') return String(Math.round(n * 10000) / 100)
  return String(n)
}

function thresholdPayload(input: string, unit: string): string {
  const n = parseFloat(input)
  if (Number.isNaN(n)) return '0'
  if (unit === 'PERCENT') return (n / 100).toFixed(6)
  return n.toFixed(4)
}

function killSwitchRearmNotice(
  requestedBy: string | null | undefined,
  accountRiskEnabled: boolean,
): {
  title: string
  message: string
} {
  if (requestedBy === 'auto_risk' || (requestedBy == null && accountRiskEnabled)) {
    return {
      title: 'Kill switch re-armed',
      message:
        'Account daily risk is still breached. Turn off account daily risk or change the daily stop/target, then Start Again.',
    }
  }
  if (requestedBy === 'emergency_webhook') {
    return {
      title: 'Kill switch re-armed',
      message: 'The emergency kill-switch webhook armed this account again.',
    }
  }
  return {
    title: 'Kill switch re-armed',
    message: 'This account is blocked from new opening signals again.',
  }
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
      showFeedbackToast('success', 'Settings saved', 'Auto square-off settings saved.')
      void queryClient.invalidateQueries({ queryKey: ['config', 'execution'] })
    },
    onError: (err: unknown) => {
      const text = extractError(err)
      setLocalError(text)
      setMessage(null)
      showFeedbackToast('error', 'Save failed', text)
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
                {mutation.isPending ? 'Saving…' : 'Save'}
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

function MarginSettingsCard() {
  const queryClient = useQueryClient()
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['config', 'margin'],
    queryFn: fetchMarginSettings,
  })
  const [draft, setDraft] = useState<MarginSettings | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)

  useEffect(() => {
    if (data) setDraft(data)
  }, [data])

  const mutation = useMutation({
    mutationFn: () => {
      if (!draft) throw new Error('No draft')
      const comfort = parseFloat(String(draft.comfort_ratio))
      if (!(comfort > 0 && comfort <= 1)) {
        throw new Error('Comfort ratio must be in (0, 1].')
      }
      return patchMarginSettings({
        check_enabled: draft.check_enabled,
        gate_basis: draft.gate_basis,
        min_free_buffer: draft.min_free_buffer,
        min_free_pct_of_netliq: draft.min_free_pct_of_netliq,
        comfort_ratio: draft.comfort_ratio,
        confirm_borderline: draft.confirm_borderline,
        enforce_look_ahead: draft.enforce_look_ahead,
        reject_on_stale_snapshot: draft.reject_on_stale_snapshot,
        default_rate: draft.default_rate,
        rate_safety_multiplier: draft.rate_safety_multiplier,
      })
    },
    onSuccess: (saved) => {
      setDraft(saved)
      setMessage('Margin policy saved.')
      setLocalError(null)
      showFeedbackToast('success', 'Settings saved', 'Margin policy saved.')
      void queryClient.invalidateQueries({ queryKey: ['config', 'margin'] })
    },
    onError: (err) => {
      const text = extractError(err)
      setLocalError(text)
      setMessage(null)
      showFeedbackToast('error', 'Save failed', text)
    },
  })

  return (
    <section className="settings-card">
      <div className="settings-block">
        <div className="settings-block-h">
          <h2>MARGIN GATE</h2>
          {draft ? (
            <label className="toggle-row">
              <input
                type="checkbox"
                checked={draft.check_enabled}
                onChange={(e) => setDraft({ ...draft, check_enabled: e.target.checked })}
              />
              <span>{draft.check_enabled ? 'Margin check enabled' : 'Shadow mode'}</span>
            </label>
          ) : null}
        </div>
        {isLoading ? <p className="field-hint">Loading…</p> : null}
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
                <span>Gate basis</span>
                <select
                  className="inline-input"
                  value={draft.gate_basis}
                  onChange={(e) => setDraft({ ...draft, gate_basis: e.target.value })}
                >
                  <option value="available_funds">Available funds (initial)</option>
                  <option value="excess_liquidity">Excess liquidity (maintenance)</option>
                </select>
              </label>
              <label className="field">
                <span>Comfort ratio</span>
                <input
                  className="inline-input"
                  type="number"
                  min="0.01"
                  max="1"
                  step="0.05"
                  value={draft.comfort_ratio}
                  onChange={(e) => setDraft({ ...draft, comfort_ratio: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Min free buffer</span>
                <input
                  className="inline-input"
                  type="number"
                  min="0"
                  step="100"
                  value={draft.min_free_buffer}
                  onChange={(e) => setDraft({ ...draft, min_free_buffer: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Min free % of net liq</span>
                <input
                  className="inline-input"
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  value={draft.min_free_pct_of_netliq}
                  onChange={(e) =>
                    setDraft({ ...draft, min_free_pct_of_netliq: e.target.value })
                  }
                />
              </label>
              <label className="toggle-row">
                <input
                  type="checkbox"
                  checked={draft.confirm_borderline}
                  onChange={(e) =>
                    setDraft({ ...draft, confirm_borderline: e.target.checked })
                  }
                />
                <span>Confirm borderline with what-if</span>
              </label>
              <label className="toggle-row">
                <input
                  type="checkbox"
                  checked={draft.enforce_look_ahead}
                  onChange={(e) =>
                    setDraft({ ...draft, enforce_look_ahead: e.target.checked })
                  }
                />
                <span>Enforce look-ahead</span>
              </label>
              <label className="toggle-row">
                <input
                  type="checkbox"
                  checked={draft.reject_on_stale_snapshot}
                  onChange={(e) =>
                    setDraft({ ...draft, reject_on_stale_snapshot: e.target.checked })
                  }
                />
                <span>Reject on stale snapshot</span>
              </label>
              <button
                type="button"
                className="btn primary"
                disabled={mutation.isPending}
                onClick={() => mutation.mutate()}
              >
                {mutation.isPending ? 'Saving…' : 'Save'}
              </button>
            </div>
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
  const cleanAccount = normalizeIbkrAccount(ibkrAccount)

  const queryClient = useQueryClient()
  const { data: account, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['config', 'account', cleanAccount],
    queryFn: () => fetchAccountByIdentifier(cleanAccount),
  })

  const { data: brokerMargin } = useQuery({
    queryKey: ['margin', 'account', cleanAccount],
    queryFn: () => fetchAccountMargin(cleanAccount),
    enabled: Boolean(cleanAccount),
    refetchInterval: 15_000,
    retry: false,
  })

  const { data: killSwitchData } = useQuery({
    queryKey: ['config', 'kill-switch', account?.id],
    queryFn: () => (account ? fetchKillSwitchStatus(account.id) : Promise.resolve(null)),
    enabled: !!account,
    refetchInterval: 2_000,
    staleTime: 0,
  })

  const { data: pauseData } = useQuery({
    queryKey: ['config', 'trading-pause', account?.id],
    queryFn: () => (account ? fetchTradingPause(account.id) : Promise.resolve(null)),
    enabled: !!account,
    refetchInterval: 2_000,
    staleTime: 0,
  })

  const isKillSwitchActive = killSwitchData?.kill_switch_active ?? account?.kill_switch_active ?? false
  const isTradingPaused = pauseData?.trading_paused ?? account?.trading_paused ?? false
  const prevKillSwitchActiveRef = useRef<boolean | null>(null)
  const killSwitchWatchAccountRef = useRef<number | undefined>(undefined)
  const skipNextArmToastRef = useRef(false)

  const [margin, setMargin] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [defaultLimitInput, setDefaultLimitInput] = useState('10000000')
  const [dailyTarget, setDailyTarget] = useState('0')
  const [dailyStop, setDailyStop] = useState('0')
  const [dailyTargetUnit, setDailyTargetUnit] = useState('ABSOLUTE')
  const [dailyStopUnit, setDailyStopUnit] = useState('ABSOLUTE')
  const [accountRiskEnabled, setAccountRiskEnabled] = useState(false)
  const [cancelExposure, setCancelExposure] = useState(false)
  const [newSymbol, setNewSymbol] = useState('')
  const [newLimit, setNewLimit] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)
  const [isKillSwitchOpen, setIsKillSwitchOpen] = useState(false)
  const [isStartAgainOpen, setIsStartAgainOpen] = useState(false)

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

  const pauseMutation = useMutation({
    mutationFn: () => (account ? pauseTrading(account.id) : Promise.reject(new Error('No account'))),
    onSuccess: () => {
      setMessage(`Trading paused for account ${cleanAccount}. New open signals are blocked.`)
      setLocalError(null)
      showFeedbackToast('success', 'Trading paused', `Account ${cleanAccount} is paused.`)
      void queryClient.invalidateQueries({ queryKey: ['config', 'trading-pause', account?.id] })
      void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
    },
    onError: (err: unknown) => {
      setLocalError(extractError(err))
    },
  })

  const resumeMutation = useMutation({
    mutationFn: () => (account ? resumeTrading(account.id) : Promise.reject(new Error('No account'))),
    onSuccess: () => {
      setMessage(`Trading resumed for account ${cleanAccount}. New open signals are allowed.`)
      setLocalError(null)
      showFeedbackToast('success', 'Trading resumed', `Account ${cleanAccount} has resumed trading.`)
      void queryClient.invalidateQueries({ queryKey: ['config', 'trading-pause', account?.id] })
      void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
    },
    onError: (err: unknown) => {
      setLocalError(extractError(err))
    },
  })

  useEffect(() => {
    if (!account?.id || killSwitchData == null) return
    if (killSwitchWatchAccountRef.current !== account.id) {
      killSwitchWatchAccountRef.current = account.id
      prevKillSwitchActiveRef.current = killSwitchData.kill_switch_active
      return
    }
    const prev = prevKillSwitchActiveRef.current
    const next = killSwitchData.kill_switch_active
    prevKillSwitchActiveRef.current = next
    if (prev !== false || next !== true) return
    if (skipNextArmToastRef.current || killSwitchData.requested_by === 'operator') {
      skipNextArmToastRef.current = false
      return
    }
    const notice = killSwitchRearmNotice(killSwitchData.requested_by, accountRiskEnabled)
    setMessage(null)
    setLocalError(notice.message)
    showFeedbackToast('error', notice.title, notice.message)
  }, [account?.id, accountRiskEnabled, killSwitchData])

  useEffect(() => {
    if (account) {
      setMargin(cleanNumberInput(account.total_margin))
      setEnabled(account.enabled)
      if (account.default_symbol_limit !== undefined && account.default_symbol_limit !== null) {
        setDefaultLimitInput(cleanNumberInput(String(account.default_symbol_limit)))
      }
      const tgtUnit = account.daily_target_unit || 'ABSOLUTE'
      const stpUnit = account.daily_stop_unit || 'ABSOLUTE'
      setDailyTargetUnit(tgtUnit)
      setDailyStopUnit(stpUnit)
      setDailyTarget(thresholdInputValue(account.daily_target, tgtUnit))
      setDailyStop(thresholdInputValue(account.daily_stop, stpUnit))
      setAccountRiskEnabled(Boolean(account.account_risk_enabled))
      setCancelExposure(Boolean(account.cancel_exposure))
    }
  }, [account])

  const accountMutation = useMutation({
    mutationFn: () => {
      if (!account) throw new Error('Account not loaded')
      return patchAccount(account.id, {
        total_margin: parseFloat(margin) || undefined,
        enabled,
        daily_target: thresholdPayload(dailyTarget, dailyTargetUnit),
        daily_stop: thresholdPayload(dailyStop, dailyStopUnit),
        daily_target_unit: dailyTargetUnit,
        daily_stop_unit: dailyStopUnit,
        account_risk_enabled: accountRiskEnabled,
        cancel_exposure: cancelExposure,
      })
    },
    onSuccess: () => {
      setMessage('Account configuration saved.')
      setLocalError(null)
      showFeedbackToast('success', 'Settings saved', 'Account configuration saved.')
      void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
      void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
    },
    onError: (err: unknown) => {
      const text = extractError(err)
      setLocalError(text)
      setMessage(null)
      showFeedbackToast('error', 'Save failed', text)
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
      showFeedbackToast('success', 'Settings saved', 'Symbol limit saved.')
      void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
    },
    onError: (err: unknown) => {
      const text = extractError(err)
      setLocalError(text)
      setMessage(null)
      showFeedbackToast('error', 'Save failed', text)
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
      showFeedbackToast('success', 'Settings saved', 'Symbol limit removed.')
      void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
    },
    onError: (err: unknown) => {
      const text = extractError(err)
      setLocalError(text)
      setMessage(null)
      showFeedbackToast('error', 'Save failed', text)
    },
  })

  const defaultLimitMutation = useMutation({
    mutationFn: (limitStr: string) => {
      if (!account) throw new Error('Account not loaded')
      const val = parseFloat(limitStr)
      if (isNaN(val) || val <= 0) {
        throw new Error('Default symbol limit must be greater than 0.')
      }
      return updateDefaultSymbolLimit(account.id, val)
    },
    onSuccess: () => {
      setMessage('Default symbol limit saved.')
      setLocalError(null)
      showFeedbackToast('success', 'Settings saved', 'Default symbol limit saved.')
      void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
      void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
    },
    onError: (err: unknown) => {
      const text = extractError(err)
      setLocalError(text)
      setMessage(null)
      showFeedbackToast('error', 'Save failed', text)
    },
  })

  const showIbkrId = Boolean(
    account?.name &&
      account.ibkr_account &&
      account.name.trim().toUpperCase() !== account.ibkr_account.trim().toUpperCase(),
  )

  return (
    <main className="page settings-page">
      <header className="settings-header-banner">
        <div className="settings-header-title">
          <h1>ACCOUNT SETTINGS</h1>
          <div className="settings-header-meta">
            <span>
              Account: <strong>{account?.name || cleanAccount}</strong>
            </span>
            {showIbkrId ? (
              <>
                <span>·</span>
                <span className="mono">{account?.ibkr_account}</span>
              </>
            ) : null}
            {account ? (
              <>
                <span className={`account-status-pill ${enabled ? 'enabled' : 'disabled'}`}>
                  ● {enabled ? 'ENABLED' : 'DISABLED'}
                </span>
                {isTradingPaused ? (
                  <span className="account-status-pill paused" style={{ background: '#78350f', color: '#fcd34d' }}>
                    ⏸ PAUSED
                  </span>
                ) : null}
                {isKillSwitchActive ? (
                  <span className="account-status-pill disabled" style={{ background: '#7f1d1d', color: '#fca5a5' }}>
                    ⛔ STOPPED
                  </span>
                ) : null}
              </>
            ) : null}
          </div>
          <p className="field-hint" style={{ marginTop: 6, maxWidth: 640 }}>
            Production OEMS controls. Changes apply to new signals; open paired exposures remain until closed or squared off.
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <Link to={`/account/${cleanAccount}`} className="btn primary">
            View Dashboard →
          </Link>
        </div>
      </header>

      {message ? (
        <p className="settings-page-feedback ok" role="status">
          {message}
        </p>
      ) : null}
      {localError ? (
        <p className="settings-page-feedback err" role="alert">
          {localError}
        </p>
      ) : null}

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
            {/* ── ACCOUNT ── */}
            <section className="settings-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>ACCOUNT</h2>
                  <label className="toggle-row">
                    <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
                    <span>{enabled ? 'Enabled' : 'Disabled'}</span>
                  </label>
                </div>
                <div className="settings-grid">
                  <label className="field" style={{ minWidth: 220 }}>
                    <span>Account Name</span>
                    <input className="inline-input" type="text" value={account.name} disabled />
                    <span className="field-hint">IBKR: {account.ibkr_account}</span>
                  </label>
                  <label className="field">
                    <span>Trading Capital</span>
                    <div className="money-field">
                      <span className="money-prefix">$</span>
                      <input type="number" min="0" step="1000" value={margin} onChange={(e) => setMargin(e.target.value)} />
                    </div>
                    <span className="field-hint">{fmtUsd(margin)}</span>
                    {brokerMargin?.effective_free_margin ? (
                      <span className="field-hint">
                        Broker free: {fmtUsd(brokerMargin.effective_free_margin)}
                        {brokerMargin.is_stale ? ' (stale)' : ''}
                      </span>
                    ) : null}
                  </label>
                </div>
              </div>
            </section>

            {/* ── RISK CONTROLS ── */}
            <section className="settings-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>RISK CONTROLS</h2>
                  <span className="field-hint" style={{ fontSize: 10, letterSpacing: '0.06em' }}>DAILY &amp; EXPOSURE</span>
                </div>

                {/* Daily Risk */}
                <div style={{ border: '1px solid var(--line)', borderRadius: 6, background: 'var(--panel-2)', padding: '12px 14px', marginBottom: 12 }}>
                  <div className="settings-block-h" style={{ marginBottom: 8, borderBottom: 'none', paddingBottom: 0 }}>
                    <h3 style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.08em', margin: 0 }}>DAILY RISK</h3>
                    <label className="toggle-row">
                      <input type="checkbox" checked={accountRiskEnabled} onChange={(e) => setAccountRiskEnabled(e.target.checked)} />
                      <span>{accountRiskEnabled ? 'Enabled' : 'Disabled'}</span>
                    </label>
                  </div>
                  <p className="field-hint" style={{ marginBottom: 10 }}>
                    Session PnL vs signed stop/target. Stop fires at PnL ≤ stop, target at PnL ≥ target. 0 is breakeven. Disable to bypass.
                  </p>
                  <div className="settings-grid">
                    <label className="field">
                      <span>Daily Target</span>
                      <div className="money-field">
                        {dailyTargetUnit === 'ABSOLUTE' ? <span className="money-prefix">$</span> : null}
                        <input type="number" step={dailyTargetUnit === 'PERCENT' ? '0.01' : '1'} value={dailyTarget} onChange={(e) => setDailyTarget(e.target.value)} />
                        <select className="inline-input" value={dailyTargetUnit} onChange={(e) => setDailyTargetUnit(e.target.value)}>
                          <option value="ABSOLUTE">USD</option>
                          <option value="PERCENT">% capital</option>
                        </select>
                      </div>
                    </label>
                    <label className="field">
                      <span>Daily Stop</span>
                      <div className="money-field">
                        {dailyStopUnit === 'ABSOLUTE' ? <span className="money-prefix">$</span> : null}
                        <input type="number" step={dailyStopUnit === 'PERCENT' ? '0.01' : '1'} value={dailyStop} onChange={(e) => setDailyStop(e.target.value)} />
                        <select className="inline-input" value={dailyStopUnit} onChange={(e) => setDailyStopUnit(e.target.value)}>
                          <option value="ABSOLUTE">USD</option>
                          <option value="PERCENT">% capital</option>
                        </select>
                      </div>
                    </label>
                  </div>
                </div>

                {/* Cancel Exposure */}
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'flex-start',
                    gap: 16,
                    padding: '12px 14px',
                    border: '1px solid var(--line)',
                    borderRadius: 6,
                    background: 'var(--panel-2)',
                    marginBottom: 12,
                  }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.08em', display: 'flex', alignItems: 'center', gap: 8 }}>
                      CANCEL EXPOSURE
                      <span
                        style={{
                          fontSize: 9,
                          fontWeight: 700,
                          letterSpacing: '0.06em',
                          padding: '2px 6px',
                          borderRadius: 4,
                          background: cancelExposure ? 'rgba(62,207,142,0.15)' : 'rgba(224,179,76,0.15)',
                          color: cancelExposure ? 'var(--green)' : 'var(--amber)',
                          border: `1px solid ${cancelExposure ? 'rgba(62,207,142,0.3)' : 'rgba(224,179,76,0.3)'}`,
                        }}
                      >
                        {cancelExposure ? 'ON' : 'OFF'}
                      </span>
                    </div>
                    <p className="field-hint" style={{ marginTop: 6, lineHeight: 1.5, maxWidth: 520 }}>
                      {cancelExposure
                        ? 'Enabled — incoming signals may partially cancel an existing paired exposure. Only use when single-leg unwinds are intentional.'
                        : 'Prevent a signal from cancelling one side of an existing paired exposure unless the corresponding pair leg is also being closed.'}
                    </p>
                    <p className="field-hint" style={{ marginTop: 4, color: 'var(--dim)', fontSize: 10 }}>
                      Example OFF: holding AAPL BUY + EWC SELL, incoming AAPL SELL + XYZ BUY is rejected. Exact close AAPL SELL + EWC BUY is allowed.
                    </p>
                  </div>
                  <label className="toggle-row" style={{ flexShrink: 0, marginTop: 2 }}>
                    <input type="checkbox" checked={cancelExposure} onChange={(e) => setCancelExposure(e.target.checked)} />
                    <span style={{ fontWeight: 700, color: cancelExposure ? 'var(--green)' : 'var(--muted)' }}>{cancelExposure ? 'On' : 'Off'}</span>
                  </label>
                </div>

                <div className="settings-grid" style={{ marginTop: 12 }}>
                  <button type="button" className="btn primary" disabled={accountMutation.isPending} onClick={() => accountMutation.mutate()}>
                    {accountMutation.isPending ? 'Saving…' : 'Save Risk Controls'}
                  </button>
                  <span className="field-hint">Applies to new signals. Existing exposures unchanged.</span>
                </div>
              </div>
            </section>

            {/* ── SYMBOL RISK LIMITS ── */}
            <section className="settings-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>SYMBOL RISK LIMITS</h2>
                </div>
                <p className="field-hint">Global default fallback and per-symbol overrides for account {cleanAccount}.</p>
                <div style={{ marginTop: 12, padding: 12, background: 'rgba(255,255,255,0.03)', borderRadius: 6, border: '1px solid rgba(255,255,255,0.08)' }}>
                  <label className="field" style={{ marginBottom: 8 }}>
                    <span style={{ fontWeight: 600, color: 'var(--amber)', fontSize: 10, letterSpacing: '0.06em' }}>DEFAULT SYMBOL LIMIT (FALLBACK)</span>
                    <div className="money-field">
                      <span className="money-prefix">$</span>
                      <input type="number" min="1" step="100000" value={defaultLimitInput} onChange={(e) => setDefaultLimitInput(e.target.value)} />
                    </div>
                  </label>
                  <p className="field-hint dim" style={{ marginBottom: 8 }}>Applies to any symbol without a specific override.</p>
                  <button type="button" className="btn primary" disabled={!defaultLimitInput.trim() || defaultLimitMutation.isPending} onClick={() => defaultLimitMutation.mutate(defaultLimitInput.trim())}>
                    Save Default Limit
                  </button>
                </div>
                <div style={{ marginTop: 16 }}>
                  <h3 style={{ fontSize: 10, fontWeight: 700, letterSpacing: '0.06em', marginBottom: 6, color: 'var(--muted)' }}>SPECIFIC OVERRIDES</h3>
                </div>
                <div style={{ marginTop: 6, overflowX: 'auto' }}>
                  <table>
                    <thead>
                      <tr>
                        <th>SYMBOL</th>
                        <th>EFFECTIVE LIMIT</th>
                        <th>TYPE</th>
                        <th style={{ textAlign: 'right' }}>ACTION</th>
                      </tr>
                    </thead>
                    <tbody>
                      {account.symbol_limits.map((lim) => (
                        <tr key={lim.symbol}>
                          <td className="mono bold">{lim.symbol}</td>
                          <td className="mono">{fmtUsd(lim.money_limit)}</td>
                          <td>
                            <span style={{ fontSize: 10, background: 'rgba(59,130,246,0.2)', color: '#60a5fa', padding: '2px 6px', borderRadius: 4, fontWeight: 600 }}>OVERRIDE</span>
                          </td>
                          <td style={{ textAlign: 'right' }}>
                            <button type="button" className="btn danger" disabled={deleteLimitMutation.isPending} onClick={() => deleteLimitMutation.mutate(lim.symbol)}>
                              Remove
                            </button>
                          </td>
                        </tr>
                      ))}
                      {account.symbol_limits.length === 0 ? (
                        <tr>
                          <td colSpan={4} className="empty">
                            No overrides. All symbols use default {fmtUsd(account.default_symbol_limit || 10000000)}.
                          </td>
                        </tr>
                      ) : null}
                    </tbody>
                  </table>
                </div>
                <div className="settings-grid" style={{ marginTop: 12 }}>
                  <label className="field">
                    <span>Symbol</span>
                    <input className="inline-input" type="text" placeholder="e.g. SIL" value={newSymbol} onChange={(e) => setNewSymbol(e.target.value.toUpperCase())} />
                  </label>
                  <label className="field">
                    <span>Money Limit ($)</span>
                    <div className="money-field">
                      <span className="money-prefix">$</span>
                      <input type="number" min="1" step="1000" placeholder="25000" value={newLimit} onChange={(e) => setNewLimit(e.target.value)} />
                    </div>
                  </label>
                  <button type="button" className="btn primary" disabled={!newSymbol.trim() || !newLimit.trim() || limitMutation.isPending} onClick={() => limitMutation.mutate({ symbol: newSymbol.trim(), limit: newLimit.trim() })}>
                    + Add Limit
                  </button>
                </div>
              </div>
            </section>

          </div>

          <div className="settings-column">
            {/* ── EXECUTION CONTROLS ── */}
            <section className="settings-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>EXECUTION CONTROLS</h2>
                  <span className="field-hint" style={{ fontSize: 10 }}>AUTO SQUARE-OFF &amp; RETRY</span>
                </div>
                <ExecutionSettingsCard />
              </div>
            </section>

            {/* ── MARGIN CONTROLS ── */}
            <section className="settings-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>MARGIN CONTROLS</h2>
                </div>
                <MarginSettingsCard />
              </div>
            </section>

            {/* ── TRADING STATE ── */}
            <section className="settings-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>TRADING STATE</h2>
                  {isTradingPaused ? <span className="account-status-pill paused" style={{ background: '#78350f', color: '#fcd34d', fontSize: 10 }}>⏸ PAUSED</span> : <span className="field-hint">ACTIVE</span>}
                </div>
                {isTradingPaused ? (
                  <div style={{ background: '#451a03', border: '1px solid #92400e', padding: '12px 14px', borderRadius: 6, marginBottom: 12 }}>
                    <strong style={{ color: '#fbbf24', fontSize: 12, display: 'block', marginBottom: 4 }}>⏸ TRADING PAUSED</strong>
                    <p className="field-hint" style={{ color: '#fde68a', margin: 0, lineHeight: 1.5 }}>
                      New opening signals are blocked. Open positions and protective exits remain active.
                    </p>
                  </div>
                ) : (
                  <p className="field-hint" style={{ marginBottom: 12 }}>Pause new opening signals without closing existing exposures.</p>
                )}
                <div style={{ display: 'flex', gap: 8 }}>
                  {isTradingPaused ? (
                    <button type="button" className="btn primary" style={{ padding: '10px 16px', fontSize: 11 }} disabled={resumeMutation.isPending} onClick={() => resumeMutation.mutate()}>
                      {resumeMutation.isPending ? 'RESUMING…' : '▶ RESUME TRADING'}
                    </button>
                  ) : (
                    <button type="button" className="btn" style={{ padding: '10px 16px', fontSize: 11, borderColor: '#b45309', color: '#fbbf24' }} disabled={pauseMutation.isPending} onClick={() => pauseMutation.mutate()}>
                      {pauseMutation.isPending ? 'PAUSING…' : '⏸ PAUSE TRADING'}
                    </button>
                  )}
                </div>
              </div>
            </section>

            {/* ── EMERGENCY ACTIONS ── */}
            <section className="settings-card" style={{ borderColor: 'rgba(239,107,115,0.35)', background: 'rgba(239,107,115,0.04)' }}>
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2 style={{ color: 'var(--red)' }}>EMERGENCY ACTIONS</h2>
                </div>
                {isKillSwitchActive ? (
                  <div style={{ background: '#3b1219', border: '1px solid #7f1d1d', padding: '12px 14px', borderRadius: 6, marginBottom: 12 }}>
                    <strong style={{ color: '#f87171', fontSize: 12, display: 'block', marginBottom: 4 }}>⛔ STOPPED — KILL SWITCH ACTIVE</strong>
                    <p className="field-hint" style={{ color: '#fca5a5', margin: 0, lineHeight: 1.5 }}>
                      {killSwitchData?.requested_by === 'auto_risk' || (killSwitchData?.requested_by == null && accountRiskEnabled)
                        ? 'Re-armed by daily risk. Adjust stop/target or disable daily risk, then Start Again.'
                        : 'Account blocked from new opens. Use Start Again to re-enable.'}
                    </p>
                  </div>
                ) : (
                  <p className="field-hint" style={{ marginBottom: 12, lineHeight: 1.5 }}>
                    Irreversible: immediately close every open pair for {cleanAccount}. Use only for operational emergency.
                  </p>
                )}
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  {isKillSwitchActive ? (
                    <button type="button" className="btn primary" style={{ padding: '10px 16px', fontSize: 11, background: '#16a34a', borderColor: '#15803d' }} onClick={() => setIsStartAgainOpen(true)}>
                      ▶ START AGAIN
                    </button>
                  ) : null}
                  <button type="button" className="btn danger" style={{ padding: '10px 16px', fontSize: 11 }} onClick={() => setIsKillSwitchOpen(true)}>
                    ⚠ SQUARE OFF ALL POSITIONS
                  </button>
                </div>
              </div>
            </section>

            <div style={{ padding: '8px 4px' }}>
              <p className="field-hint" style={{ fontSize: 10, lineHeight: 1.6, color: 'var(--dim)' }}>
                All settings persist per account. Allocation and risk edits affect new pairs only. Open pairs retain frozen exit levels until closed.
              </p>
            </div>
          </div>
        </div>
      ) : null}

      {account ? (
        <>
          <KillSwitchModal
            isOpen={isKillSwitchOpen}
            accountId={account.id}
            ibkrAccount={account.ibkr_account}
            openCount={accountOpenPositionsCount}
            onClose={() => setIsKillSwitchOpen(false)}
            onSuccess={(closedCount, scope) => {
              const scopeLabel = scope === 'ACCOUNT_POSITION_FLATTEN' || scope === 'Flatten Account'
                ? 'Flatten Account'
                : scope === 'MANUAL_POSITION_FLATTEN' || scope === 'Flatten Manual Positions'
                ? 'Flatten Manual Positions'
                : 'Flatten Signal Positions'
              const text = `Kill Switch executed (${scopeLabel}): squared off ${closedCount} position(s).`
              skipNextArmToastRef.current = true
              setMessage(text)
              setLocalError(null)
              showFeedbackToast('success', 'Kill switch executed', text)
              void queryClient.invalidateQueries({ queryKey: ['config', 'kill-switch', account.id] })
              void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
            }}
          />
          <StartAgainModal
            isOpen={isStartAgainOpen}
            accountId={account.id}
            ibkrAccount={account.ibkr_account}
            onClose={() => setIsStartAgainOpen(false)}
            onSuccess={() => {
              const text = 'Account execution state changed back to ACTIVE. Account is allowed to receive trading signals again.'
              setMessage(text)
              setLocalError(null)
              showFeedbackToast('success', 'Account restarted', text)
              void queryClient.invalidateQueries({ queryKey: ['config', 'kill-switch', account.id] })
              void queryClient.invalidateQueries({ queryKey: ['config', 'account', cleanAccount] })
              void queryClient.invalidateQueries({ queryKey: ['config', 'accounts'] })
            }}
          />
        </>
      ) : null}
    </main>
  )
}
