export interface AuditLogItem {
  id: number
  ts: string
  process: string
  kind: string
  signal_id?: number | null
  order_id?: number | null
  basket_id?: number | null
  idempotency_key?: string | null
  detail: Record<string, unknown>
}

export interface AuditLogsResponse {
  total: number
  limit: number
  offset: number
  events: AuditLogItem[]
}

export interface AuditLogsQueryParams {
  category?: string
  process?: string
  kind?: string
  search?: string
  date_from?: string
  date_to?: string
  limit?: number
  offset?: number
}
