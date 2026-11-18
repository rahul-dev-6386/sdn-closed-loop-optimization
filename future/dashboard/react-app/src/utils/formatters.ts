/**
 * Unified Formatting & Unit Conversion Module.
 *
 * Implements Ground Rule 0:
 * - One formatter module (fmtMbps, fmtPct, fmtAge, fmtTime) used across all panels.
 * - Percentage is strictly derived from (used / linkCapacityMbps) * 100.
 * - Warns in development mode if |pct - used/cap| > 1 point.
 * - Canonical link IDs (sorted endpoints: s1--s2 === s2--s1).
 */

export type SourceType = 'measured' | 'predicted' | 'simulated' | 'derived'

/**
 * Normalizes two endpoint node names into a canonical link key.
 * Always ensures s1--s2 and s2--s1 produce the exact same key.
 */
export function canonicalLinkKey(a: string, b: string): string {
  return [a, b].sort().join('--')
}

/**
 * Human-readable display label for a link key, using en-dash.
 */
export function fmtLinkLabel(key: string): string {
  if (!key) return ''
  return key.replace(/--/g, '–').replace(/-/g, '–')
}

/**
 * Formats bandwidth in Mbps.
 */
export function fmtMbps(used: number, cap?: number): string {
  if (cap !== undefined && cap > 0) {
    return `${used.toFixed(1)} / ${cap} Mbps`
  }
  return `${used.toFixed(1)} Mbps`
}

/**
 * Calculates and formats utilization percentage based on a link's own capacity.
 * Warns in dev mode if deviation is greater than 1 point.
 */
export function fmtPct(used: number, capacity: number): string {
  const cap = Math.max(0.1, capacity)
  const exact = (used / cap) * 100
  const pct = Math.round(exact)

  if (import.meta.env.DEV) {
    if (Math.abs(pct - exact) > 1.0) {
      console.warn(`[fmtPct] Deviation detected: used=${used}, cap=${cap}, calculated=${pct}%, exact=${exact.toFixed(2)}%`)
    }
  }
  return `${pct}%`
}

/**
 * Single source of truth link metric summary string:
 * e.g. "s1–s2 · 8.8 / 10 Mbps (88%)"
 */
export function fmtLinkLoad(linkName: string, used: number, capacity: number): string {
  const cap = Math.max(0.1, capacity)
  const pct = Math.round((used / cap) * 100)
  return `${fmtLinkLabel(linkName)} · ${used.toFixed(1)} / ${cap} Mbps (${pct}%)`
}

/**
 * Formats elapsed duration into human readable "Xs ago".
 */
export function fmtAge(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  return `${h}h ago`
}

/**
 * Formats a timestamp (epoch seconds) in unified ISO time with timezone label.
 */
export function fmtTime(epochSec: number, tzLabel = 'UTC'): string {
  const d = new Date(epochSec * 1000)
  const hours = String(d.getUTCHours()).padStart(2, '0')
  const mins = String(d.getUTCMinutes()).padStart(2, '0')
  const secs = String(d.getUTCSeconds()).padStart(2, '0')
  return `${hours}:${mins}:${secs} ${tzLabel}`
}

/**
 * Generates descriptive source metadata for UI tooltips.
 */
export function sourceTooltip(source: SourceType, detail?: string): string {
  switch (source) {
    case 'measured':
      return `Source: Measured live telemetry (OVS kernel datapath counters)${detail ? ` - ${detail}` : ''}`
    case 'predicted':
      return `Source: ML congestion forecast model (RandomForest p>=0.5)${detail ? ` - ${detail}` : ''}`
    case 'simulated':
      return `Source: Synthetic demo spike injection${detail ? ` - ${detail}` : ''}`
    case 'derived':
      return `Source: Client-derived state transition${detail ? ` - ${detail}` : ''}`
  }
}
