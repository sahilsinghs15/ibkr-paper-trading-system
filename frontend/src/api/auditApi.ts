import axios from 'axios'
import type {
  AuditEventDetail,
  AuditEventsResponse,
  AuditFacets,
  AuditFilters,
} from '../types/audit'

const base = '/api/v1/audit'

function localToIso(value: string): string | undefined {
  if (!value) return undefined
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString()
}

/** Build query params; repeated keys (category=A&category=B) for multi-select. */
function toParams(
  filters: AuditFilters,
  paging: { limit: number; offset: number; sort: 'newest' | 'oldest' },
): URLSearchParams {
  const p = new URLSearchParams()
  const set = (key: string, value: string | undefined) => {
    const v = value?.trim()
    if (v) p.set(key, v)
  }
  set('date_from', localToIso(filters.date_from))
  set('date_to', localToIso(filters.date_to))
  set('actor', filters.actor)
  set('role', filters.role)
  set('ip', filters.ip)
  filters.categories.forEach((c) => p.append('category', c))
  set('action', filters.action)
  set('result', filters.result)
  set('account', filters.account)
  set('session_id', filters.session_id)
  set('device_id', filters.device_id)
  set('ref_id', filters.ref_id)
  set('q', filters.q)
  if (filters.provenance !== 'all') p.set('provenance', filters.provenance)
  p.set('limit', String(paging.limit))
  p.set('offset', String(paging.offset))
  p.set('sort', paging.sort)
  return p
}

export async function searchAuditEvents(
  filters: AuditFilters,
  paging: { limit: number; offset: number; sort: 'newest' | 'oldest' },
): Promise<AuditEventsResponse> {
  const { data } = await axios.get<AuditEventsResponse>(`${base}/events`, {
    params: toParams(filters, paging),
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
