import type { AuditEventSummary, AuditResult } from '../../types/audit'

export function resultClass(result: AuditResult | string): string {
  switch (result) {
    case 'SUCCEEDED':
      return 'audit-result ok'
    case 'ACCEPTED':
      return 'audit-result accepted'
    case 'PENDING':
    case 'PARTIAL':
    case 'UNKNOWN':
      return 'audit-result warn'
    case 'REJECTED':
    case 'DENIED':
    case 'FAILED':
      return 'audit-result bad'
    default:
      return 'audit-result'
  }
}

export const RESULT_HINT: Record<string, string> = {
  PENDING: 'Intent recorded before execution; no outcome was recorded (process may have stopped).',
  ACCEPTED: 'Accepted/queued; completion happens asynchronously.',
  PARTIAL: 'Partially completed.',
  REJECTED: 'Refused by validation or business rules.',
  DENIED: 'Refused by authentication/authorization.',
  FAILED: 'Attempted but failed.',
  UNKNOWN: 'Outcome not known (legacy record or cancelled request).',
  SUCCEEDED: 'Completed.',
}

export function actorLabel(row: Pick<AuditEventSummary, 'actor_type' | 'actor_email' | 'target_id'>): string {
  switch (row.actor_type) {
    case 'USER':
      return row.actor_email ?? 'Unknown user'
    case 'SERVICE':
      return 'External service (shared secret)'
    case 'ANONYMOUS':
      return 'Unauthenticated caller'
    case 'UNATTRIBUTED':
      return row.actor_email ?? 'Not recorded (legacy)'
    default:
      return row.actor_email ?? '—'
  }
}

export function categoryLabel(category: string): string {
  return category
    .split('_')
    .map((w) => w.charAt(0) + w.slice(1).toLowerCase())
    .join(' ')
}

export function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return '—'
  return id.length > n ? `${id.slice(0, n)}…` : id
}

export function displayValue(v: unknown): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'string') return v === '' ? '""' : v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  return JSON.stringify(v)
}
