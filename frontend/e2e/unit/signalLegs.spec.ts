import { expect, test } from '@playwright/test'

import {
  formatRetryLabel,
  groupLogicalLegs,
  latestRetryAttempt,
  RETRY_EVENT_KIND,
} from '../../src/utils/signalLegs'
import type { SignalOrderLeg } from '../../src/store/signalStore'

function order(p: Partial<SignalOrderLeg> & { symbol: string }): SignalOrderLeg {
  return {
    id: 0,
    leg: 'L0',
    basket_id: 1,
    buy_sell: 'BUY',
    quantity: 0,
    fill_qty: 0,
    status: 'FILLED',
    is_compensation: false,
    ...p,
  } as SignalOrderLeg
}

// Mirrors backend tests/test_naked_pair_protection_fix.py test 1: EWP fills
// 300 of 399, the remainder is cancelled, a retry fills the final 99.
test('retry orders collapse into the original leg, not a third leg', () => {
  const legs = groupLogicalLegs([
    order({ id: 1, leg: 'L0', symbol: 'EWP', quantity: 399, fill_qty: 300, status: 'CANCELLED' }),
    order({ id: 2, leg: 'L1', symbol: 'EWU', quantity: 546, fill_qty: 546, buy_sell: 'SELL' }),
    order({ id: 3, leg: 'L0', symbol: 'EWP', quantity: 99, fill_qty: 99 }),
  ])

  expect(legs).toHaveLength(2)

  const ewp = legs.find((l) => l.symbol === 'EWP')!
  expect(ewp.req).toBe(399) // original qty, not the 99-share retry remainder
  expect(ewp.fill).toBe(399) // cumulative across both attempts
  expect(ewp.retries).toBe(1)
  expect(ewp.isFull).toBe(true)

  const ewu = legs.find((l) => l.symbol === 'EWU')!
  expect(ewu.retries).toBe(0)
  expect(ewu.isFull).toBe(true)
})

test('same symbol on two distinct legs stays separate', () => {
  const legs = groupLogicalLegs([
    order({ id: 1, leg: 'L0', symbol: 'EWP', quantity: 100, fill_qty: 100 }),
    order({ id: 2, leg: 'L1', symbol: 'EWU', quantity: 200, fill_qty: 200 }),
    order({ id: 3, leg: 'L2', symbol: 'EWP', quantity: 50, fill_qty: 50 }),
  ])
  expect(legs).toHaveLength(3)
  expect(legs.every((l) => l.retries === 0)).toBe(true)
})

test('legs from different baskets never merge', () => {
  const legs = groupLogicalLegs([
    order({ id: 1, basket_id: 1, leg: 'L0', symbol: 'EWP', quantity: 100, fill_qty: 100 }),
    order({ id: 2, basket_id: 2, leg: 'L0', symbol: 'EWP', quantity: 100, fill_qty: 100 }),
  ])
  expect(legs).toHaveLength(2)
})

test('partial leg is reported partial, not full', () => {
  const [leg] = groupLogicalLegs([
    order({ id: 1, leg: 'L0', symbol: 'EWP', quantity: 399, fill_qty: 300, status: 'CANCELLED' }),
  ])
  expect(leg.isFull).toBe(false)
  expect(leg.isPartial).toBe(true)
})

test('retry attempt is read from the event kind the backend actually emits', () => {
  const events = [
    { id: 1, kind: RETRY_EVENT_KIND, detail: { retry: 1, max_retries: 3 } },
    { id: 2, kind: RETRY_EVENT_KIND, detail: { retry: 2, max_retries: 3 } },
  ]
  const info = latestRetryAttempt(events, [])
  expect(info).toEqual({ attempt: 2, max: 3 })
  expect(formatRetryLabel(info!)).toBe('Retry 2/3')
})

test('legacy BASKET_RETRY events are not counted', () => {
  // The old frontend looked for this kind; the backend never emitted it, so
  // retry progress silently never rendered.
  const info = latestRetryAttempt([{ id: 1, kind: 'BASKET_RETRY', detail: { attempt: 2 } }], [])
  expect(info).toBeNull()
})

test('falls back to per-leg retry count when events are absent', () => {
  const legs = groupLogicalLegs([
    order({ id: 1, leg: 'L0', symbol: 'EWP', quantity: 399, fill_qty: 300, status: 'CANCELLED' }),
    order({ id: 2, leg: 'L0', symbol: 'EWP', quantity: 99, fill_qty: 99 }),
  ])
  const info = latestRetryAttempt([], legs)
  expect(info).toEqual({ attempt: 1, max: null })
  expect(formatRetryLabel(info!)).toBe('Retry 1')
})

test('no retries reports null', () => {
  const legs = groupLogicalLegs([
    order({ id: 1, leg: 'L0', symbol: 'EWP', quantity: 100, fill_qty: 100 }),
  ])
  expect(latestRetryAttempt([], legs)).toBeNull()
})
