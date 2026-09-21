import { expect, test } from '@playwright/test'
import { isoToZonedInput, zonedInputToIso } from '../../src/utils/zonedTime'

// The audit date-range filter sends these to the server. A wrong offset silently
// returns the wrong window, which looks like "the filter is broken".
test('New York wall time converts to the correct UTC instant (EDT)', () => {
  expect(zonedInputToIso('2026-09-21T00:00', 'America/New_York')).toBe('2026-09-21T04:00:00.000Z')
  expect(zonedInputToIso('2026-09-21T09:30', 'America/New_York')).toBe('2026-09-21T13:30:00.000Z')
})

test('New York wall time converts correctly after DST ends (EST)', () => {
  expect(zonedInputToIso('2026-12-01T00:00', 'America/New_York')).toBe('2026-12-01T05:00:00.000Z')
})

test('half-hour offset zone converts correctly', () => {
  expect(zonedInputToIso('2026-09-21T00:00', 'Asia/Kolkata')).toBe('2026-09-20T18:30:00.000Z')
})

test('UTC is identity', () => {
  expect(zonedInputToIso('2026-09-21T12:00', 'UTC')).toBe('2026-09-21T12:00:00.000Z')
})

test('invalid input yields undefined rather than a bogus range', () => {
  expect(zonedInputToIso('', 'UTC')).toBeUndefined()
  expect(zonedInputToIso('2026-09-21', 'UTC')).toBeUndefined()
})

test('round-trips through the datetime-local input', () => {
  for (const tz of ['America/New_York', 'Asia/Kolkata', 'UTC']) {
    const wall = '2026-09-21T14:25'
    const iso = zonedInputToIso(wall, tz)!
    expect(isoToZonedInput(new Date(iso), tz), tz).toBe(wall)
  }
})
