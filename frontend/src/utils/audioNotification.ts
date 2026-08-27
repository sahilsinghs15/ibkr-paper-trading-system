/**
 * Audio notification utility using Web Audio API.
 * Plays a subtle, professional double-tone chime when a NEW trading signal arrives.
 */

const SOUND_STORAGE_KEY = 'trading_app_sound_enabled'

let audioCtx: AudioContext | null = null

function getAudioContext(): AudioContext | null {
  if (typeof window === 'undefined') return null
  if (!audioCtx) {
    const AudioContextClass = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    if (AudioContextClass) {
      audioCtx = new AudioContextClass()
    }
  }
  if (audioCtx && audioCtx.state === 'suspended') {
    audioCtx.resume().catch(() => {
      /* ignore resume error until user interaction */
    })
  }
  return audioCtx
}

export function isSoundEnabled(): boolean {
  if (typeof window === 'undefined') return false
  const val = localStorage.getItem(SOUND_STORAGE_KEY)
  if (val === null) return true // Default ON
  return val === 'true'
}

export function setSoundEnabled(enabled: boolean): void {
  if (typeof window === 'undefined') return
  localStorage.setItem(SOUND_STORAGE_KEY, String(enabled))
  if (enabled) {
    getAudioContext()
  }
}

export function toggleSoundEnabled(): boolean {
  const next = !isSoundEnabled()
  setSoundEnabled(next)
  return next
}

/**
 * Unlock AudioContext upon user click/interaction to bypass browser autoplay restrictions.
 */
export function unlockAudioContext(): void {
  const ctx = getAudioContext()
  if (ctx && ctx.state === 'suspended') {
    ctx.resume().catch(() => {})
  }
}

/**
 * Extract active IBKR account filter from browser URL (e.g. /account/DUR919062).
 */
function getActiveAccountFromUrl(): string | null {
  if (typeof window === 'undefined') return null
  const pathname = window.location.pathname
  const match = pathname.match(/\/account\/([^/]+)/i)
  const clean = match ? match[1].trim().toUpperCase() : ''
  if (!clean || clean === 'UNKNOWN') return null
  return clean
}

/**
 * Play a subtle 150ms dual-tone sine wave chime for a NEW signal arrival.
 */
export function playSignalNotificationSound(signalAccount?: string | null): void {
  if (!isSoundEnabled()) return

  // Account scoping check
  const activeAccount = getActiveAccountFromUrl()
  if (activeAccount && signalAccount) {
    const cleanSignalAcc = String(signalAccount).trim().toUpperCase()
    if (cleanSignalAcc !== activeAccount) {
      return // Signal belongs to a different account, skip sound
    }
  }

  const ctx = getAudioContext()
  if (!ctx || ctx.state === 'suspended') {
    return // Blocked by browser autoplay until user clicks
  }

  try {
    const now = ctx.currentTime

    // Tone 1: 880Hz (A5)
    const osc1 = ctx.createOscillator()
    const gain1 = ctx.createGain()
    osc1.type = 'sine'
    osc1.frequency.setValueAtTime(880, now)
    gain1.gain.setValueAtTime(0.08, now)
    gain1.gain.exponentialRampToValueAtTime(0.001, now + 0.12)
    osc1.connect(gain1)
    gain1.connect(ctx.destination)
    osc1.start(now)
    osc1.stop(now + 0.12)

    // Tone 2: 1320Hz (E6) — 40ms delayed chime
    const osc2 = ctx.createOscillator()
    const gain2 = ctx.createGain()
    osc2.type = 'sine'
    osc2.frequency.setValueAtTime(1320, now + 0.04)
    gain2.gain.setValueAtTime(0.06, now + 0.04)
    gain2.gain.exponentialRampToValueAtTime(0.001, now + 0.18)
    osc2.connect(gain2)
    gain2.connect(ctx.destination)
    osc2.start(now + 0.04)
    osc2.stop(now + 0.18)
  } catch {
    /* ignore audio playback exceptions */
  }
}
