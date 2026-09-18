import axios from 'axios'

/**
 * Non-sensitive client context sent with every API request for audit attribution.
 *
 * - The device id is a random identifier for this browser install, kept in
 *   localStorage. It is not a fingerprint: clearing site data resets it.
 * - The backend records both values as client-reported and unverified; identity,
 *   role and IP are always derived server-side.
 */

const DEVICE_ID_KEY = 'ibkr_trading_client_device_id'
const DEVICE_ID_PATTERN = /^[A-Za-z0-9-]{8,64}$/

export const APP_VERSION: string = __APP_VERSION__

function randomId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  // randomUUID requires a secure context; getRandomValues does not.
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function getClientDeviceId(): string | null {
  try {
    const existing = localStorage.getItem(DEVICE_ID_KEY)
    if (existing && DEVICE_ID_PATTERN.test(existing)) return existing
    const created = randomId()
    localStorage.setItem(DEVICE_ID_KEY, created)
    return created
  } catch {
    return null
  }
}

export function installClientContextHeaders(): void {
  const deviceId = getClientDeviceId()
  if (deviceId) axios.defaults.headers.common['X-Client-Device-Id'] = deviceId
  axios.defaults.headers.common['X-Client-App-Version'] = APP_VERSION
}
