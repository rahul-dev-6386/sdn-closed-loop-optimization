/**
 * OpenFlow 1.3 Flow Table Rule Parser.
 *
 * Implements Section 5:
 * - Parses raw ovs-ofctl dump-flows lines into structured table records:
 *   priority | match | actions | packets | bytes | age
 * - Distinguishes between system rules (dl_type=0x88b5 probe, priority 0 table-miss)
 *   and forwarding rules (output:X, FLOOD, NORMAL).
 * - Flags reroute-installed detour flows and new rules.
 * - Graceful fallback to raw text if unexpected syntax is encountered.
 */

export interface ParsedFlowRule {
  id: string
  raw: string
  parseFailed: boolean
  cookie: string
  durationSec: number
  ageText: string
  table: number
  packets: number
  bytes: number
  priority: number
  match: string
  actions: string
  isForwarding: boolean
  isSystem: boolean
  isDetour: boolean
  tag: 'NEW' | 'DETOUR' | 'SYSTEM' | 'FORWARD' | null
}

export function parseFlowRule(raw: string, index: number, isDetourActive = false): ParsedFlowRule {
  const line = raw.trim()

  try {
    const actionsIdx = line.indexOf('actions=')
    if (actionsIdx === -1) {
      throw new Error('No actions delimiter found')
    }

    const beforeActions = line.substring(0, actionsIdx)
    const actionsPart = line.substring(actionsIdx + 8).trim()

    // Extract cookie
    const cookieMatch = beforeActions.match(/cookie=([0-9a-fx]+)/i)
    const cookie = cookieMatch ? cookieMatch[1] : '0x0'

    // Extract duration
    const durationMatch = beforeActions.match(/duration=([0-9.]+)s/i)
    const durationSec = durationMatch ? parseFloat(durationMatch[1]) : 0

    // Format age
    let ageText = `${Math.round(durationSec)}s`
    if (durationSec > 3600) {
      ageText = `${Math.floor(durationSec / 3600)}h ${Math.floor((durationSec % 3600) / 60)}m`
    } else if (durationSec > 60) {
      ageText = `${Math.floor(durationSec / 60)}m ${Math.round(durationSec % 60)}s`
    }

    // Extract table
    const tableMatch = beforeActions.match(/table=([0-9]+)/i)
    const table = tableMatch ? parseInt(tableMatch[1], 10) : 0

    // Extract packets and bytes
    const packetsMatch = beforeActions.match(/n_packets=([0-9]+)/i)
    const packets = packetsMatch ? parseInt(packetsMatch[1], 10) : 0

    const bytesMatch = beforeActions.match(/n_bytes=([0-9]+)/i)
    const bytes = bytesMatch ? parseInt(bytesMatch[1], 10) : 0

    // Extract priority
    const priorityMatch = beforeActions.match(/priority=([0-9]+)/i)
    const priority = priorityMatch ? parseInt(priorityMatch[1], 10) : 0

    // Extract match criteria (everything after priority=XXX, until comma or end)
    let match = '* (any)'
    if (priorityMatch) {
      const postPriority = beforeActions.substring(priorityMatch.index! + priorityMatch[0].length).trim()
      const cleanMatch = postPriority.replace(/^,\s*/, '').replace(/,\s*$/, '').trim()
      if (cleanMatch) {
        match = cleanMatch
      }
    }

    // Classification
    const isProbe = match.includes('0x88b5') || actionsPart.includes('CONTROLLER')
    const isTableMiss = priority === 0 && actionsPart.includes('CONTROLLER')
    const isSystem = isProbe || isTableMiss
    const isForwarding = !isSystem && (actionsPart.includes('output') || actionsPart.includes('FLOOD') || actionsPart.includes('NORMAL'))

    // Tagging
    const isDetour = isDetourActive && (actionsPart.includes('output:2') || actionsPart.includes('output:3'))
    let tag: ParsedFlowRule['tag'] = null
    if (isDetour) {
      tag = 'DETOUR'
    } else if (isSystem) {
      tag = 'SYSTEM'
    } else if (isForwarding) {
      tag = 'FORWARD'
    }

    return {
      id: `rule-${index}-${priority}`,
      raw: line,
      parseFailed: false,
      cookie,
      durationSec,
      ageText,
      table,
      packets,
      bytes,
      priority,
      match,
      actions: actionsPart,
      isForwarding,
      isSystem,
      isDetour,
      tag,
    }
  } catch {
    return {
      id: `rule-${index}-raw`,
      raw: line,
      parseFailed: true,
      cookie: 'n/a',
      durationSec: 0,
      ageText: 'n/a',
      table: 0,
      packets: 0,
      bytes: 0,
      priority: 0,
      match: 'raw',
      actions: line,
      isForwarding: false,
      isSystem: false,
      isDetour: false,
      tag: null,
    }
  }
}
