export interface IngestJobMetadata {
  request_id?: string
  received_at?: string
  [key: string]: unknown
}

export interface IngestJobItem {
  job_id: string
  signal_id: string
  trade_id: string | null
  strategy_id: string
  action: string
  status: string
  account_scope: string | null
  account_id: number | null
  ibkr_account: string | null
  correlation_id: string
  idempotency_key: string
  attempt_count: number
  max_attempts: number
  last_error: string | null
  deferral_reason: string | null
  received_at: string | null
  queued_at: string | null
  claimed_at: string | null
  processing_started_at: string | null
  completed_at: string | null
  raw_body: string
  parsed_json: Record<string, unknown>
  metadata: IngestJobMetadata
}

export interface IngestJobCounts {
  all: number
  total: number
  queued: number
  processing: number
  completed: number
  rejected: number
  failed: number
  deferred: number
  recovery: number
  dead_letter: number
}

export interface IngestJobsResponse {
  jobs: IngestJobItem[]
  page: number
  page_size: number
  total: number
  total_pages: number
  counts: IngestJobCounts
}

export interface IngestJobsQueryParams {
  page?: number
  page_size?: number
  status?: string
  account_id?: number
  ibkr_account?: string
  search?: string
}
