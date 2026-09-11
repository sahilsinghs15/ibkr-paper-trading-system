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
  patchAllocation,
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

interface AllocationDraft {
  allocPct: number
  enabled: boolean
  maxOpenPositions: number
  pairMaxAllocationPct: number
  target: string
  stop: string
  timeLimit: number
  targetUnit: string
  stopUnit: string
  exitAutomationEnabled: boolean
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
  const [drafts, setDrafts] = useState<Record<number, AllocationDraft>>({})
  const [newSymbol, setNewSymbol] = useState('')
  const [newLimit, setNewLimit] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)
  const [isKillSwitchOpen, setIsKillSwitchOpen] = useState(false)
  const [isStartAgainOpen, setIsStartAgainOpen] = useState(false)

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
      setDrafts(
        Object.fromEntries(
          account.allocations.map((a) => [
            a.id,
            {
              allocPct: pctFromDecimal(a.alloc_pct),
              enabled: a.enabled,
              maxOpenPositions: a.max_open_positions,
              pairMaxAllocationPct: pctFromDecimal(a.pair_max_allocation_pct),
              target: thresholdInputValue(a.target, a.target_unit || 'ABSOLUTE'),
              stop: thresholdInputValue(a.stop, a.stop_unit || 'ABSOLUTE'),
              timeLimit: a.time_limit,
              targetUnit: a.target_unit || 'ABSOLUTE',
              stopUnit: a.stop_unit || 'ABSOLUTE',
              exitAutomationEnabled: Boolean(a.exit_automation_enabled),
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
        daily_target: thresholdPayload(dailyTarget, dailyTargetUnit),
        daily_stop: thresholdPayload(dailyStop, dailyStopUnit),
        daily_target_unit: dailyTargetUnit,
        daily_stop_unit: dailyStopUnit,
        account_risk_enabled: accountRiskEnabled,
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

  const allocationMutation = useMutation({
    mutationFn: ({ id, draft }: { id: number; draft: AllocationDraft }) =>
      patchAllocation(id, {
        alloc_pct: decimalFromPct(draft.allocPct),
        enabled: draft.enabled,
        max_open_positions: draft.maxOpenPositions,
        pair_max_allocation_pct: decimalFromPct(draft.pairMaxAllocationPct),
        target: thresholdPayload(draft.target, draft.targetUnit),
        stop: thresholdPayload(draft.stop, draft.stopUnit),
        time_limit: draft.timeLimit,
        target_unit: draft.targetUnit,
        stop_unit: draft.stopUnit,
        exit_automation_enabled: draft.exitAutomationEnabled,
      }),
    onSuccess: () => {
      setMessage('Strategy allocation saved.')
      setLocalError(null)
      showFeedbackToast('success', 'Settings saved', 'Strategy allocation saved.')
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
  const showIbkrId = Boolean(
    account?.name &&
      account.ibkr_account &&
      account.name.trim().toUpperCase() !== account.ibkr_account.trim().toUpperCase(),
  )

  return (
    <main className="page settings-page">
      <header className="settings-header-banner">
        <div className="settings-header-title">
          <h1>MODEL BLUE ACCOUNT SETTINGS</h1>
          <div className="settings-header-meta">
            <span>
              Account:{' '}
              <strong>{account?.name || cleanAccount}</strong>
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
                    ⏸️ PAUSED
                  </span>
                ) : null}
                {isKillSwitchActive ? (
                  <span className="account-status-pill disabled" style={{ background: '#7f1d1d', color: '#fca5a5' }}>
                    ⛔ STOPPED (KILL SWITCH)
                  </span>
                ) : null}
              </>
            ) : null}
          </div>
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
                    <span>Trading capital</span>
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
                    {brokerMargin?.effective_free_margin ? (
                      <span className="field-hint">
                        Broker free: {fmtUsd(brokerMargin.effective_free_margin)}
                        {brokerMargin.is_stale ? ' (stale)' : ''}
                      </span>
                    ) : null}
                  </label>
                </div>

                <div className="settings-block-h" style={{ marginTop: 16 }}>
                  <h2>ACCOUNT DAILY RISK</h2>
                  <label className="toggle-row">
                    <input
                      type="checkbox"
                      checked={accountRiskEnabled}
                      onChange={(e) => setAccountRiskEnabled(e.target.checked)}
                    />
                    <span>{accountRiskEnabled ? 'Enabled' : 'Disabled'}</span>
                  </label>
                </div>
                <p className="field-hint">
                  Session PnL (realized today + open unrealized) vs signed daily
                  stop / target levels. Stop fires at PnL ≤ stop (e.g. −100).
                  Target fires at PnL ≥ target (e.g. −10 or 0). 0 is breakeven,
                  not off. Use the switch above to disable.
                </p>
                <div className="settings-grid">
                  <label className="field">
                    <span>Daily target</span>
                    <div className="money-field">
                      {dailyTargetUnit === 'ABSOLUTE' ? (
                        <span className="money-prefix">$</span>
                      ) : null}
                      <input
                        type="number"
                        step={dailyTargetUnit === 'PERCENT' ? '0.01' : '1'}
                        value={dailyTarget}
                        onChange={(e) => setDailyTarget(e.target.value)}
                      />
                      <select
                        className="inline-input"
                        value={dailyTargetUnit}
                        onChange={(e) => setDailyTargetUnit(e.target.value)}
                      >
                        <option value="ABSOLUTE">USD</option>
                        <option value="PERCENT">% of capital</option>
                      </select>
                    </div>
                  </label>
                  <label className="field">
                    <span>Daily stop</span>
                    <div className="money-field">
                      {dailyStopUnit === 'ABSOLUTE' ? (
                        <span className="money-prefix">$</span>
                      ) : null}
                      <input
                        type="number"
                        step={dailyStopUnit === 'PERCENT' ? '0.01' : '1'}
                        value={dailyStop}
                        onChange={(e) => setDailyStop(e.target.value)}
                      />
                      <select
                        className="inline-input"
                        value={dailyStopUnit}
                        onChange={(e) => setDailyStopUnit(e.target.value)}
                      >
                        <option value="ABSOLUTE">USD</option>
                        <option value="PERCENT">% of capital</option>
                      </select>
                    </div>
                  </label>
                </div>

                <div className="settings-grid">
                  <button
                    type="button"
                    className="btn primary"
                    disabled={accountMutation.isPending}
                    onClick={() => accountMutation.mutate()}
                  >
                    {accountMutation.isPending ? 'Saving…' : 'Save Changes'}
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
                pairMaxAllocationPct: pctFromDecimal(a.pair_max_allocation_pct),
                target: thresholdInputValue(a.target, a.target_unit || 'ABSOLUTE'),
                stop: thresholdInputValue(a.stop, a.stop_unit || 'ABSOLUTE'),
                timeLimit: a.time_limit,
                targetUnit: a.target_unit || 'ABSOLUTE',
                stopUnit: a.stop_unit || 'ABSOLUTE',
                exitAutomationEnabled: Boolean(a.exit_automation_enabled),
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

                        <label className="field">
                          <span>Per-pair allocation</span>
                          <div className="money-field">
                            <input
                              className="inline-input"
                              type="number"
                              min="0.01"
                              max="100"
                              step="0.01"
                              value={draft.pairMaxAllocationPct}
                              onChange={(e) =>
                                updateDraft(a.id, {
                                  pairMaxAllocationPct: parseFloat(e.target.value) || 0,
                                })
                              }
                            />
                            <span className="money-suffix">%</span>
                          </div>
                          <span className="field-hint">
                            {fmtPct(draft.pairMaxAllocationPct)} of {fmtUsd(String(committed))} ={' '}
                            {fmtUsd(
                              String((committed * draft.pairMaxAllocationPct) / 100),
                            )}{' '}
                            per pair
                            {committed > 0 && draft.pairMaxAllocationPct > 0
                              ? ` · room for ${Math.floor(
                                  100 / draft.pairMaxAllocationPct,
                                )} pairs`
                              : ''}
                          </span>
                        </label>

                        <div className="settings-block-h">
                          <h3>Exit automation</h3>
                          <label className="toggle-row">
                            <input
                              type="checkbox"
                              checked={draft.exitAutomationEnabled}
                              onChange={(e) =>
                                updateDraft(a.id, {
                                  exitAutomationEnabled: e.target.checked,
                                })
                              }
                            />
                            <span>
                              {draft.exitAutomationEnabled ? 'Enabled' : 'Disabled'}
                            </span>
                          </label>
                        </div>
                        <p className="field-hint">
                          Target/stop/time_limit are copied onto each pair at open.
                          Threshold edits here apply to new pairs only; click an Open
                          Positions row to change an already-open pair. Toggling
                          automation here also arms or disarms currently open pairs of
                          this strategy. Values are signed PnL levels: stop
                          −100 fires at ≤ −$100, target −10 fires at ≥ −$10,
                          target 0 is breakeven. The arm switch turns this off.
                        </p>

                        <label className="field">
                          <span>Pair target</span>
                          <div className="money-field">
                            {draft.targetUnit === 'ABSOLUTE' ? (
                              <span className="money-prefix">$</span>
                            ) : null}
                            <input
                              type="number"
                              step={draft.targetUnit === 'PERCENT' ? '0.01' : '1'}
                              value={draft.target}
                              onChange={(e) =>
                                updateDraft(a.id, { target: e.target.value })
                              }
                            />
                            <select
                              className="inline-input"
                              value={draft.targetUnit}
                              onChange={(e) =>
                                updateDraft(a.id, { targetUnit: e.target.value })
                              }
                            >
                              <option value="ABSOLUTE">USD</option>
                              <option value="PERCENT">% of pair</option>
                            </select>
                          </div>
                        </label>

                        <label className="field">
                          <span>Pair stop</span>
                          <div className="money-field">
                            {draft.stopUnit === 'ABSOLUTE' ? (
                              <span className="money-prefix">$</span>
                            ) : null}
                            <input
                              type="number"
                              step={draft.stopUnit === 'PERCENT' ? '0.01' : '1'}
                              value={draft.stop}
                              onChange={(e) =>
                                updateDraft(a.id, { stop: e.target.value })
                              }
                            />
                            <select
                              className="inline-input"
                              value={draft.stopUnit}
                              onChange={(e) =>
                                updateDraft(a.id, { stopUnit: e.target.value })
                              }
                            >
                              <option value="ABSOLUTE">USD</option>
                              <option value="PERCENT">% of pair</option>
                            </select>
                          </div>
                        </label>

                        <label className="field">
                          <span>Time limit (seconds)</span>
                          <input
                            className="inline-input"
                            type="number"
                            min="0"
                            step="60"
                            value={draft.timeLimit}
                            onChange={(e) =>
                              updateDraft(a.id, {
                                timeLimit: parseInt(e.target.value, 10) || 0,
                              })
                            }
                          />
                          <span className="field-hint">
                            {draft.timeLimit > 0
                              ? `${Math.round(draft.timeLimit / 60)} min`
                              : 'disabled'}
                          </span>
                        </label>

                        <button
                          type="button"
                          className="btn primary"
                          disabled={allocationMutation.isPending}
                          onClick={() => allocationMutation.mutate({ id: a.id, draft })}
                        >
                          {allocationMutation.isPending ? 'Saving…' : 'Save Allocation'}
                        </button>
                      </div>
                    </div>
                  </div>
                </section>
              )
            })}

            {/* Auto Square-Off & Retry */}
            <ExecutionSettingsCard />
            <MarginSettingsCard />
          </div>

          <div className="settings-column">
            {/* Per-Symbol Money Limits */}
            <section className="settings-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>SYMBOL RISK LIMITS</h2>
                </div>
                <p className="field-hint">
                  Configure the global default symbol limit for account{' '}
                  <span className="mono">{cleanAccount}</span> and optional specific symbol overrides.
                </p>

                {/* Default Symbol Limit Block */}
                <div style={{ marginTop: 12, padding: '12px', background: 'rgba(255,255,255,0.03)', borderRadius: '6px', border: '1px solid rgba(255,255,255,0.08)' }}>
                  <label className="field" style={{ marginBottom: 8 }}>
                    <span style={{ fontWeight: 600, color: 'var(--amber)' }}>DEFAULT SYMBOL LIMIT (FALLBACK)</span>
                    <div className="money-field">
                      <span className="money-prefix">$</span>
                      <input
                        type="number"
                        min="1"
                        step="100000"
                        value={defaultLimitInput}
                        onChange={(e) => setDefaultLimitInput(e.target.value)}
                      />
                    </div>
                  </label>
                  <p className="field-hint dim" style={{ marginBottom: 8 }}>
                    Automatically applies to any symbol without a specific override below.
                  </p>
                  <button
                    type="button"
                    className="btn primary"
                    disabled={!defaultLimitInput.trim() || defaultLimitMutation.isPending}
                    onClick={() => defaultLimitMutation.mutate(defaultLimitInput.trim())}
                  >
                    Save Default Limit
                  </button>
                </div>

                <div style={{ marginTop: 16 }}>
                  <h3 style={{ fontSize: '11px', fontWeight: 700, letterSpacing: '0.05em', marginBottom: 6 }}>
                    SPECIFIC SYMBOL OVERRIDES
                  </h3>
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
                            <span style={{ fontSize: '10px', background: 'rgba(59, 130, 246, 0.2)', color: '#60a5fa', padding: '2px 6px', borderRadius: '4px', fontWeight: 600 }}>
                              SPECIFIC OVERRIDE
                            </span>
                          </td>
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
                          <td colSpan={4} className="empty">
                            No specific overrides. All symbols currently fall back to the Default Limit ({fmtUsd(account.default_symbol_limit || 10000000)}).
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

            {/* Trading Pause */}
            <section className="settings-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>TRADING PAUSE</h2>
                </div>

                {isTradingPaused ? (
                  <div
                    className="trading-pause-banner"
                    style={{
                      background: '#451a03',
                      border: '1px solid #b45309',
                      padding: '12px 16px',
                      borderRadius: '6px',
                      marginBottom: 12,
                    }}
                  >
                    <strong style={{ color: '#fbbf24', fontSize: '13px', display: 'block', marginBottom: 4 }}>
                      ⏸️ TRADING PAUSED
                    </strong>
                    <p className="field-hint" style={{ color: '#fde68a', margin: 0 }}>
                      This account is currently paused for new opening trading signals. Currently open positions and protective exits (stop loss / target) remain active.
                    </p>
                  </div>
                ) : (
                  <p className="field-hint" style={{ color: 'var(--ink)' }}>
                    Temporarily pause incoming opening signals for account{' '}
                    <span className="mono bold">{cleanAccount}</span> without affecting open positions or protective exits.
                  </p>
                )}

                <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
                  {isTradingPaused ? (
                    <button
                      type="button"
                      className="btn primary"
                      style={{ padding: '10px 16px', fontSize: '11px' }}
                      disabled={resumeMutation.isPending}
                      onClick={() => resumeMutation.mutate()}
                    >
                      {resumeMutation.isPending ? 'RESUMING…' : '▶️ RESUME TRADING'}
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="btn"
                      style={{ padding: '10px 16px', fontSize: '11px', borderColor: '#b45309', color: '#fbbf24' }}
                      disabled={pauseMutation.isPending}
                      onClick={() => pauseMutation.mutate()}
                    >
                      {pauseMutation.isPending ? 'PAUSING…' : '⏸️ PAUSE TRADING'}
                    </button>
                  )}
                </div>
              </div>
            </section>

            {/* Emergency / Kill Switch */}
            <section className="settings-card danger-card">
              <div className="settings-block">
                <div className="settings-block-h">
                  <h2>EMERGENCY / KILL SWITCH</h2>
                </div>

                {isKillSwitchActive ? (
                  <div
                    className="killswitch-stopped-banner"
                    style={{
                      background: '#3b1219',
                      border: '1px solid #7f1d1d',
                      padding: '12px 16px',
                      borderRadius: '6px',
                      marginBottom: 12,
                    }}
                  >
                    <strong style={{ color: '#f87171', fontSize: '13px', display: 'block', marginBottom: 4 }}>
                      ⛔ STOPPED BY KILL SWITCH
                    </strong>
                    <p className="field-hint" style={{ color: '#fca5a5', margin: 0 }}>
                      {killSwitchData?.requested_by === 'auto_risk' ||
                      (killSwitchData?.requested_by == null && accountRiskEnabled)
                        ? 'Re-armed by account daily risk. New opening signals stay blocked until you change the daily stop/target or turn daily risk off, then Start Again.'
                        : 'This account is currently blocked from receiving or processing new opening trading signals.'}
                    </p>
                  </div>
                ) : (
                  <p className="field-hint" style={{ color: 'var(--ink)' }}>
                    Close every currently open position for account{' '}
                    <span className="mono bold">{cleanAccount}</span>.
                  </p>
                )}

                <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
                  {isKillSwitchActive ? (
                    <button
                      type="button"
                      className="btn primary"
                      style={{ padding: '10px 16px', fontSize: '11px', background: '#16a34a', borderColor: '#15803d' }}
                      onClick={() => setIsStartAgainOpen(true)}
                    >
                      ▶ START AGAIN
                    </button>
                  ) : null}
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

      {account ? (
        <>
          <KillSwitchModal
            isOpen={isKillSwitchOpen}
            accountId={account.id}
            ibkrAccount={account.ibkr_account}
            openCount={accountOpenPositionsCount}
            onClose={() => setIsKillSwitchOpen(false)}
            onSuccess={(closedCount) => {
              const text = `Kill Switch executed: squared off ${closedCount} position(s).`
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
              const text =
                'Account execution state changed back to ACTIVE. Account is allowed to receive trading signals again.'
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
