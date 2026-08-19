import { TZ_IN, TZ_NY, type DisplayTimezone } from '../types/position'

export function legKey(row: {
  account_id?: number | string | null
  trade_id?: string | null
  symbol?: string | null
}): string {
  return [row.account_id, row.trade_id, row.symbol].join('|')
}

export function tradeKey(row: {
  account_id?: number | string | null
  trade_id?: string | null
}): string {
  return [row.account_id, row.trade_id].join('|')
}

export function num(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  if (typeof v === 'string' && v.trim() === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

export function blank(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—'
  return String(v)
}

export function displayInstrument(v: unknown): string {
  const raw = String(v || '')
    .trim()
    .toUpperCase()
  if (!raw) return '—'
  // Paper demo executes requested STK as IBKR CFD; dashboard shows execution type.
  if (raw === 'STK') return 'CFD'
  return raw
}

export function fmtUsd(v: unknown): string {
  const n = num(v)
  if (n === null) return '—'
  return (
    '$' +
    n.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  )
}

export function fmtNum(v: unknown, digits: number): string {
  const n = num(v)
  if (n === null) return '—'
  return n.toLocaleString('en-US', {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  })
}

export function fmtPnl(v: unknown): string {
  const n = num(v)
  if (n === null) return '—'
  const abs = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
  if (n > 0) return '+$' + abs
  if (n < 0) return '-$' + abs
  return '$' + abs
}

export function pnlClass(v: unknown): string {
  const n = num(v)
  if (n === null || n === 0) return 'pnl-zero'
  return n > 0 ? 'pnl-pos' : 'pnl-neg'
}

export function tzShortLabel(tz: DisplayTimezone): string {
  return tz === TZ_IN ? 'IST' : 'ET'
}

export function tzLongLabel(tz: DisplayTimezone): string {
  return tz === TZ_IN ? 'India / IST' : 'New York / ET'
}

export function formatInTz(
  date: Date,
  tz: DisplayTimezone,
): { date: string; time: string; zone: string } {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
    timeZoneName: 'short',
  }).formatToParts(date)
  const get = (type: string) =>
    (parts.find((p) => p.type === type) || {}).value || ''
  return {
    date: `${get('year')}-${get('month')}-${get('day')}`,
    time: `${get('hour')}:${get('minute')}:${get('second')}`,
    zone: get('timeZoneName') || tzShortLabel(tz),
  }
}

export function fmtTime(
  iso: string | null | undefined,
  tz: DisplayTimezone,
  opts?: { withZone?: boolean },
): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const parts = formatInTz(d, tz)
  const stamp = `${parts.date} ${parts.time}`
  return opts?.withZone ? `${stamp} ${tzShortLabel(tz)}` : stamp
}

export function badgeClass(label: unknown): string {
  const v = String(label || '').toUpperCase()
  if (v === 'OPEN' || v === 'FILLED') return 'b-open'
  if (
    v === 'EXECUTING' ||
    v === 'PARTIALLY_FILLED' ||
    v === 'CLOSING' ||
    v === 'UNWINDING'
  )
    return 'b-exec'
  if (v === 'CLOSED') return 'b-closed'
  if (v === 'COMPENSATED') return 'b-comp'
  if (v === 'REJECTED' || v === 'CRITICAL' || v === 'ERROR') return 'b-rej'
  return ''
}

export function statusLabel(row: {
  event?: string | null
  close_in_progress?: boolean
  basket_state?: string | null
  order_status?: string | null
  status?: string | null
  position_state?: string | null
}): string {
  const event = String(row.event || '').toUpperCase()
  if (event === 'POSITION_PARTIAL_CLOSE' || row.close_in_progress) return 'CLOSING'
  const basket = String(row.basket_state || '').toUpperCase()
  if (['EXECUTING', 'UNWINDING', 'COMPENSATED', 'CRITICAL'].includes(basket))
    return basket
  const order = String(row.order_status || '').toUpperCase()
  if (['PARTIALLY_FILLED', 'REJECTED', 'ERROR'].includes(order)) return order
  return String(row.status || row.position_state || '—').toUpperCase()
}

export function markOf(row: {
  mark_price?: string | number | null
  last_price?: string | number | null
}): string {
  const mark = blank(row.mark_price)
  if (mark !== '—') return mark
  const last = blank(row.last_price)
  if (last !== '—') return last
  return '—'
}

export function loadTimezone(): DisplayTimezone {
  try {
    const saved = localStorage.getItem('modelBlue.displayTimezone')
    if (saved === TZ_NY || saved === TZ_IN) return saved
  } catch {
    /* ignore */
  }
  return TZ_NY
}

export function saveTimezone(tz: DisplayTimezone): void {
  try {
    localStorage.setItem('modelBlue.displayTimezone', tz)
  } catch {
    /* ignore */
  }
}
