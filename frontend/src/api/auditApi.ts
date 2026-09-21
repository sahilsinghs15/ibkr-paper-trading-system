import axios from 'axios'
import type {
  AuditEventDetail,
  AuditEventsResponse,
  AuditFacets,
  AuditFilters,
} from '../types/audit'
import { zonedInputToIso } from '../utils/zonedTime'

const base = '/api/v1/audit'

export interface AuditPaging {
  limit: number
  offset: number
  sort: 'newest' | 'oldest'
}

/** Build query params; repeated keys (category=A&category=B) for multi-select. */
function toParams(filters: AuditFilters, paging: AuditPaging, displayTz: string): URLSearchParams {
  const p = new URLSearchParams()
  const set = (key: string, value: string | undefined) => {
    const v = value?.trim()
    if (v) p.set(key, v)
  }
  set('date_from', filters.date_from ? zonedInputToIso(filters.date_from, displayTz) : undefined)
  set('date_to', filters.date_to ? zonedInputToIso(filters.date_to, displayTz) : undefined)
  set('actor', filters.actor)
  // Repeated keys: the server reads category/action/result as lists and ORs
  // within each, so these widen a search rather than narrowing it.
  filters.categories.forEach((c) => p.append('category', c))
  filters.actions.forEach((a) => p.append('action', a))
  set('account', filters.account)
  filters.results.forEach((r) => p.append('result', r))
  set('q', filters.q)
  set('ip', filters.ip)
  set('session_id', filters.session_id)
  set('device_id', filters.device_id)
  set('browser', filters.browser)
  set('role', filters.role)
  set('order_id', filters.order_id)
  set('trade_id', filters.trade_id)
  set('position_id', filters.position_id)
  set('correlation_id', filters.correlation_id)
  set('ref_id', filters.ref_id)
  if (filters.provenance !== 'all') p.set('provenance', filters.provenance)
  p.set('limit', String(paging.limit))
  p.set('offset', String(paging.offset))
  p.set('sort', paging.sort)
  return p
}

export async function searchAuditEvents(
  filters: AuditFilters,
  paging: AuditPaging,
  displayTz: string,
): Promise<AuditEventsResponse> {
  const { data } = await axios.get<AuditEventsResponse>(`${base}/events`, {
    params: toParams(filters, paging, displayTz),
    headers: { 'Cache-Control': 'no-store' },
  })
  return data
}

export async function fetchAuditEvent(eventId: string): Promise<AuditEventDetail> {
  const { data } = await axios.get<AuditEventDetail>(
    `${base}/events/${encodeURIComponent(eventId)}`,
    { headers: { 'Cache-Control': 'no-store' } },
  )
  return data
}

export async function fetchAuditFacets(): Promise<AuditFacets> {
  const { data } = await axios.get<AuditFacets>(`${base}/facets`)
  return data
}

/** FastAPI error payloads: {detail: string} or {detail: [{msg}]} for 422s. */
export function auditErrorMessage(err: unknown): string {
  if (axios.isAxiosError(err)) {
    const detail = (err.response?.data as { detail?: unknown } | undefined)?.detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (d && typeof d === 'object' && 'msg' in d ? String(d.msg) : String(d)))
        .join('; ')
    }
    if (err.response?.status === 403) return 'Administrator role required.'
    return err.message
  }
  return err instanceof Error ? err.message : 'Request failed'
}
