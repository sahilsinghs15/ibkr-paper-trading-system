/**
 * Conversions between wall-clock inputs (datetime-local "YYYY-MM-DDTHH:mm")
 * interpreted in an IANA timezone and UTC instants.
 */

function wallParts(date: Date, tz: string): number[] {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(date)
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? 0)
  return [get('year'), get('month'), get('day'), get('hour') % 24, get('minute'), get('second')]
}

/** Offset (ms) of `tz` from UTC at instant `date`. */
function tzOffsetMs(date: Date, tz: string): number {
  const [y, mo, d, h, mi, s] = wallParts(date, tz)
  return Date.UTC(y, mo - 1, d, h, mi, s) - Math.floor(date.getTime() / 1000) * 1000
}

/** "YYYY-MM-DDTHH:mm" wall time in `tz` -> ISO UTC string (undefined if invalid). */
export function zonedInputToIso(value: string, tz: string): string | undefined {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value)
  if (!m) return undefined
  const guess = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5])
  // Two passes settle DST transitions.
  let instant = guess - tzOffsetMs(new Date(guess), tz)
  instant = guess - tzOffsetMs(new Date(instant), tz)
  return new Date(instant).toISOString()
}

/** Instant -> "YYYY-MM-DDTHH:mm" wall time in `tz` (for datetime-local inputs). */
export function isoToZonedInput(date: Date, tz: string): string {
  const [y, mo, d, h, mi] = wallParts(date, tz)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${y}-${pad(mo)}-${pad(d)}T${pad(h)}:${pad(mi)}`
}
