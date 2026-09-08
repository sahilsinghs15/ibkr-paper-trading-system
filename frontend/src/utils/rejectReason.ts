/** Account-scoped reject reason helpers for the dashboard. */

export type RejectDisplay = {
  category: string
  summary: string
  raw: string
}

/**
 * When fan-out concatenates `Account X: …; Account Y: …`, keep only the open
 * account's segment. Unscoped reasons are returned unchanged.
 */
export function scopeRejectReasonForAccount(
  raw: string | null | undefined,
  ibkrAccount: string | null | undefined,
): string | null {
  if (!raw) return null
  const text = String(raw).trim()
  if (!text) return null

  const filter = String(ibkrAccount || '').trim().toUpperCase()
  const segmentRe = /Account\s+(\S+?):\s*([\s\S]*?)(?=(?:;\s*)?Account\s+\S+?:|$)/gi
  const segments: Array<{ label: string; reason: string }> = []
  let match: RegExpExecArray | null
  while ((match = segmentRe.exec(text)) !== null) {
    const label = String(match[1] || '').trim().toUpperCase()
    const reason = String(match[2] || '').trim().replace(/[;\s]+$/g, '')
    if (label && reason) segments.push({ label, reason })
  }

  if (segments.length === 0) return text
  if (!filter) return text

  const hit = segments.find((s) => s.label === filter)
  return hit ? hit.reason : null
}

function fmtUsd(value: string | number): string {
  const n = typeof value === 'number' ? value : Number(String(value).replace(/,/g, ''))
  if (!Number.isFinite(n)) return String(value)
  return n.toLocaleString('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })
}

function stripRmsPrefix(text: string): string {
  return text
    .replace(/^RMS\s+check\s+\d+\s+rejected\s+intent:\s*/i, '')
    .trim()
}

function parseKnownReason(scoped: string): Omit<RejectDisplay, 'raw'> | null {
  const s = stripRmsPrefix(scoped)

  const money = s.match(
    /MONEY_LIMIT_EXCEEDED:\s*Symbol\s+'([^']+)'\s+total\s+exposure\s+of\s+([\d]+(?:\.\d+)?)\s+\(existing\s+([\d]+(?:\.\d+)?)\s*\+\s*new\s+([\d]+(?:\.\d+)?)\)\s+exceeds\s+limit\s+of\s+([\d]+(?:\.\d+)?)/i,
  )
  if (money) {
    const [, symbol, total, existing, newAmt, limit] = money
    return {
      category: 'Money limit',
      summary: `${symbol} money limit exceeded: ${fmtUsd(total)} (existing ${fmtUsd(existing)} + new ${fmtUsd(newAmt)}) vs ${fmtUsd(limit)} cap`,
    }
  }

  const openLimit = s.match(
    /OPEN_POSITION_LIMIT_REACHED:\s*Strategy\s+'([^']+)'\s+has\s+(\d+)\s+open\s+position\(s\),\s+meeting\s+or\s+exceeding\s+limit\s+of\s+(\d+)/i,
  )
  if (openLimit) {
    const [, strategy, current, limit] = openLimit
    return {
      category: 'Position limit',
      summary: `Open position limit reached: ${current}/${limit} for ${strategy}`,
    }
  }

  const marginStale = s.match(
    /MARGIN_SNAPSHOT_STALE:\s*snapshot\s+for\s+(\S+)\s+older\s+than\s+(\d+)s/i,
  )
  if (marginStale) {
    const [, account, seconds] = marginStale
    return {
      category: 'Margin data',
      summary: `Margin snapshot stale for ${account} (older than ${seconds}s)`,
    }
  }

  const minShare = s.match(
    /MODEL_BLUE_MIN_SHARE:\s*(\S+)\s+sizes\s+below\s+1\s+share\s+at\s+price\s+([\d]+(?:\.\d+)?)/i,
  )
  if (minShare) {
    const [, symbol, price] = minShare
    return {
      category: 'Sizing',
      summary: `${symbol} sized below 1 share at ${fmtUsd(price)}`,
    }
  }

  const minNotional = s.match(
    /MODEL_BLUE_MIN_NOTIONAL:\s*(\S+)\s+notional\s+([\d]+(?:\.\d+)?)\s+is\s+below\s+minimum\s+([\d]+(?:\.\d+)?)/i,
  )
  if (minNotional) {
    const [, symbol, notional, minimum] = minNotional
    return {
      category: 'Sizing',
      summary: `${symbol} notional ${fmtUsd(notional)} below ${fmtUsd(minimum)} minimum`,
    }
  }

  if (s.includes('NO_OPEN_POSITION')) {
    return {
      category: 'No position',
      summary: 'Cannot close: no open position for this trade ID',
    }
  }

  const duplicate = s.match(
    /DUPLICATE_EXECUTION:\s*'([^']+)'\s+already\s+executed/i,
  )
  if (duplicate) {
    return {
      category: 'Duplicate',
      summary: `Duplicate execution blocked — ${duplicate[1]} already executed`,
    }
  }

  if (s.includes('ambiguous') || s.includes('code=200')) {
    return {
      category: 'Broker',
      summary: 'IBKR contract description ambiguous',
    }
  }

  if (s.includes('COMMITTED_NOT_CONFIGURED') || s.includes('PAIR_BUDGET_NOT_CONFIGURED')) {
    return {
      category: 'Allocation',
      summary: 'Account capital or pair budget not configured',
    }
  }

  if (s.includes('KILL_SWITCH_ACTIVE')) {
    return {
      category: 'Kill switch',
      summary: 'Account is in emergency kill-switch mode — new opens blocked',
    }
  }

  if (s.includes('NO_ELIGIBLE_ACCOUNTS')) {
    return {
      category: 'Routing',
      summary: 'No eligible account subscriptions for this strategy',
    }
  }

  const marginReject = s.match(/MARGIN_(?:INSUFFICIENT|REJECT|COMFORT)/i)
  if (marginReject || s.includes('MARGIN_COMFORT') || s.includes('required=')) {
    const cleaned = s.replace(/^[A-Z_]+:\s*/, '').trim()
    return {
      category: 'Margin',
      summary: cleaned || 'Insufficient margin for this order',
    }
  }

  return null
}

function fallbackDisplay(scoped: string): Omit<RejectDisplay, 'raw'> {
  const cleaned = stripRmsPrefix(scoped)
    .replace(/^[A-Z_]+:\s*/, '')
    .trim()
  if (cleaned) {
    return {
      category: 'Rejected',
      summary: cleaned,
    }
  }
  return {
    category: 'Rejected',
    summary: 'Signal declined by execution pipeline',
  }
}

export function formatRejectReason(
  raw: string | null | undefined,
  ibkrAccount?: string | null,
): RejectDisplay {
  const scoped = scopeRejectReasonForAccount(raw, ibkrAccount)
  if (!scoped) {
    return {
      category: 'Rejected',
      summary: 'Signal declined by execution pipeline',
      raw: String(raw || '').trim(),
    }
  }

  const parsed = parseKnownReason(scoped)
  if (parsed) {
    return { ...parsed, raw: scoped }
  }

  const fallback = fallbackDisplay(scoped)
  return { ...fallback, raw: scoped }
}

/** Back-compat: returns human summary only. */
export function cleanRejectReason(
  raw: string | null | undefined,
  ibkrAccount?: string | null,
): string {
  return formatRejectReason(raw, ibkrAccount).summary
}
