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

export interface AuditEventsResponse {
  total: number
  limit: number
  offset: number
  items: AuditEventSummary[]
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
  target_types: string[]
}

export interface AuditFilters {
  date_from: string
  date_to: string
  actor: string
  role: string
  ip: string
  categories: string[]
  action: string
  result: string
  account: string
  session_id: string
  device_id: string
  ref_id: string
  q: string
  provenance: 'all' | 'native' | 'legacy'
}

export const EMPTY_AUDIT_FILTERS: AuditFilters = {
  date_from: '',
  date_to: '',
  actor: '',
  role: '',
  ip: '',
  categories: [],
  action: '',
  result: '',
  account: '',
  session_id: '',
  device_id: '',
  ref_id: '',
  q: '',
  provenance: 'all',
}
