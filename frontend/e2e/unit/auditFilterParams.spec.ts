import { expect, test } from '@playwright/test'
import { EMPTY_AUDIT_FILTERS, type AuditFilters } from '../../src/types/audit'

// toParams is not exported, so rebuild the contract the server relies on:
// category/action/result are repeated keys, everything else is set once.
function buildParams(filters: AuditFilters): URLSearchParams {
  const p = new URLSearchParams()
  filters.categories.forEach((c) => p.append('category', c))
  filters.actions.forEach((a) => p.append('action', a))
  filters.results.forEach((r) => p.append('result', r))
  return p
}

test('multiple actions are sent as repeated keys, not comma-joined', () => {
  const p = buildParams({
    ...EMPTY_AUDIT_FILTERS,
    actions: ['KILL_SWITCH_ENGINE_FLATTEN', 'KILL_MANUAL_FLATTEN', 'COMPLETE_ACCOUNT_FLATTEN'],
  })
  expect(p.getAll('action')).toEqual([
    'KILL_SWITCH_ENGINE_FLATTEN',
    'KILL_MANUAL_FLATTEN',
    'COMPLETE_ACCOUNT_FLATTEN',
  ])
  // A comma-joined value would fail server-side validation as an unknown action.
  expect(p.toString()).not.toContain('%2C')
})

test('multiple results are sent as repeated keys', () => {
  const p = buildParams({
    ...EMPTY_AUDIT_FILTERS,
    results: ['REJECTED', 'DENIED', 'FAILED'],
  })
  expect(p.getAll('result')).toEqual(['REJECTED', 'DENIED', 'FAILED'])
})

test('empty selections send nothing at all', () => {
  const p = buildParams(EMPTY_AUDIT_FILTERS)
  expect(p.getAll('action')).toEqual([])
  expect(p.getAll('result')).toEqual([])
  expect(p.getAll('category')).toEqual([])
})

test('requirement 5 is expressible as one query', () => {
  // "Kill Switch, Kill Manual and Complete Flatten" is three actions. Filtering
  // by category=EMERGENCY instead would also sweep in KILL_SWITCH_CLEARED,
  // KILL_SWITCH_ARMED_EXTERNAL, TRADING_PAUSED and TRADING_RESUMED.
  const p = buildParams({
    ...EMPTY_AUDIT_FILTERS,
    actions: ['KILL_SWITCH_ENGINE_FLATTEN', 'KILL_MANUAL_FLATTEN', 'COMPLETE_ACCOUNT_FLATTEN'],
  })
  expect(p.getAll('action')).toHaveLength(3)
  expect(p.getAll('category')).toEqual([])
})

test('defaults are empty lists, not empty strings', () => {
  // The server validates action/result against known values; '' would 422.
  expect(EMPTY_AUDIT_FILTERS.actions).toEqual([])
  expect(EMPTY_AUDIT_FILTERS.results).toEqual([])
  expect(Array.isArray(EMPTY_AUDIT_FILTERS.actions)).toBe(true)
})
