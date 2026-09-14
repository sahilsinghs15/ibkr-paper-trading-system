/**
 * Browser-compatible idempotency key generator for manual trading.
 * - Uses crypto.randomUUID() when available (secure contexts).
 * - Falls back to crypto.getRandomValues() for HTTP deployments.
 * - Never falls back to predictable counters or Date.now().
 * - Throws if no secure random source exists — caller must not create an order.
 */
export function genManualIdemKey(): string {
  if (typeof crypto !== 'undefined' && typeof (crypto as unknown as { randomUUID?: unknown }).randomUUID === 'function') {
    const uuid = (crypto as unknown as { randomUUID: () => string }).randomUUID()
    return `MAN_IDEM_${uuid.replace(/-/g, '').slice(0, 16).toUpperCase()}`
  }

  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    const bytes = new Uint8Array(16)
    crypto.getRandomValues(bytes)
    const id = Array.from(bytes)
      .map((b) => b.toString(16).padStart(2, '0'))
      .join('')
      .slice(0, 16)
      .toUpperCase()
    return `MAN_IDEM_${id}`
  }

  throw new Error('Secure random number generation is unavailable; cannot safely create a manual order idempotency key.')
}
