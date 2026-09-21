/** Operator audit trail (server-side authoritative). Mirrors app/schemas/audit_schemas.py. */

export type AuditResult =
  | 'PENDING'
  | 'SUCCEEDED'
  | 'ACCEPTED'
  | 'PARTIAL'
  | 'REJECTED'
  | 'DENIED'
  | 'FAILED'
  | 'UNKNOWN'

export type AuditActorType = 'USER' | 'SERVICE' | 'ANONYMOUS' | 'UNATTRIBUTED'

export interface AuditEventSummary {
  event_id: string
  occurred_at: string
  category: string
  action: string
  action_label: string
  result: AuditResult
  summary: string
  actor_type: AuditActorType
  actor_user_id: number | null
  actor_email: string | null
  actor_role: string | null
  session_id: string | null
  client_ip: string | null
  client_label: string | null
  client_device_id: string | null
  account_id: number | null
  ibkr_account: string | null
  target_type: string | null
  target_id: string | null
  provenance: string
}

export interface EmptySearchFilterHint {
  field: string
  label: string
  value: string
  matches: number
}

export interface AuditEventsResponse {
  total: number
  limit: number
  offset: number
  items: AuditEventSummary[]
  /** Set only when nothing matched: how many events each active filter matches alone. */
  empty_filter_hints?: EmptySearchFilterHint[] | null
}

export interface FieldChange {
  field: string
  before: unknown
  after: unknown
}

export interface AuthSessionInfo {
  session_id: string
  user_id: number
  user_email: string
  user_role: string
  auth_method: string
  created_at: string
  expires_at: string
  ended_at: string | null
  end_reason: string | null
  login_ip: string | null
  login_user_agent: string | null
  login_device_id: string | null
  login_audit_event_id: string | null
}

export interface InvestigationContext {
  session: AuthSessionInfo | null
  session_event_count: number | null
  concurrent_sessions: AuthSessionInfo[]
  device_first_seen_at: string | null
  actor_ips_within_24h: { ip: string; events: number }[]
}

export interface AuditEventDetail extends AuditEventSummary {
  recorded_at: string
  completed_at: string | null
  result_reason: string | null
  auth_method: string | null
  user_agent: string | null
  request_id: string | null
  correlation_id: string | null
  http_method: string | null
  http_path: string | null
  parameters: Record<string, unknown>
  before_state: unknown
  after_state: unknown
  changes: FieldChange[] | null
  related: Record<string, unknown>
  ref_ids: string[]
  context: Record<string, unknown>
  legacy_ref: string | null
  investigation: InvestigationContext
}

export interface AuditActionFacet {
  action: string
  label: string
}

export interface AuditCategoryFacet {
  category: string
  label: string
  actions: AuditActionFacet[]
}

export interface AuditFacets {
  categories: AuditCategoryFacet[]
  results: AuditResult[]
  actor_types: AuditActorType[]
  actors: string[]
  roles: string[]
  browsers: string[]
  target_types: string[]
}

/**
 * Search filters, grouped the way the UI presents them:
 * primary (operator), security (actor/source investigation) and
 * correlation (technical identifiers).
 */
export interface AuditFilters {
  // Primary
  date_from: string // wall time "YYYY-MM-DDTHH:mm" in the display timezone
  date_to: string
  actor: string
  categories: string[]
  action: string
  account: string
  result: string
  q: string
  // Advanced / security
  ip: string
  session_id: string
  device_id: string
  browser: string
  role: string
  // Technical correlation
  order_id: string
  trade_id: string
  position_id: string
  correlation_id: string
  ref_id: string // other identifier, set only by pivoting from an event
  provenance: 'all' | 'native' | 'legacy'
}

export const SECURITY_FILTER_KEYS = ['ip', 'session_id', 'device_id', 'browser', 'role'] as const
export const CORRELATION_FILTER_KEYS = [
  'order_id',
  'trade_id',
  'position_id',
  'correlation_id',
  'ref_id',
] as const

export const EMPTY_AUDIT_FILTERS: AuditFilters = {
  date_from: '',
  date_to: '',
  actor: '',
  categories: [],
  action: '',
  account: '',
  result: '',
  q: '',
  ip: '',
  session_id: '',
  device_id: '',
  browser: '',
  role: '',
  order_id: '',
  trade_id: '',
  position_id: '',
  correlation_id: '',
  ref_id: '',
  provenance: 'all',
}
