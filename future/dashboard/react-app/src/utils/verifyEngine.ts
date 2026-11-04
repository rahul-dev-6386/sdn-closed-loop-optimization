/**
 * Dynamic Closed-Loop Post-Reroute Verification Engine.
 *
 * Implements Section 6.1:
 * - Computes verification verdict dynamically from measured post-reroute samples
 *   of the actual bottleneck link.
 * - PASS only if the bottleneck link stays strictly under the threshold for N
 *   consecutive samples (default 5 samples ~10s).
 * - FAIL if the bottleneck link remains >= threshold after the propagation window.
 * - PENDING while evaluating or awaiting sample stabilization.
 * - Details before-value, after-value, timestamps, and evaluation window.
 */
import { fmtLinkLabel, fmtPct } from './formatters'

export type VerifyVerdict = 'IDLE' | 'PENDING' | 'PASS' | 'FAIL'

export interface VerifyState {
  status: VerifyVerdict
  passed: boolean
  bottleneckLink: string
  detourActive: boolean
  detourStartSec: number | null
  beforeLoadPct: number
  currentLoadPct: number
  thresholdPct: number
  consecutiveBelow: number
  requiredConsecutive: number
  windowSec: number
  evalTimeSec: number
  message: string
  detail: {
    beforeText: string
    currentText: string
    windowText: string
    conditionText: string
  }
}

export class VerifyEngine {
  private bottleneckLink: string
  private detourActive = false
  private detourStartSec: number | null = null
  private beforeLoadPct = 88
  private consecutiveBelow = 0
  private requiredConsecutive = 5
  private thresholdPct = 80
  private lastVerdict: VerifyVerdict = 'IDLE'

  constructor(bottleneckLink: string, thresholdPct = 80, requiredConsecutive = 5) {
    this.bottleneckLink = bottleneckLink
    this.thresholdPct = thresholdPct
    this.requiredConsecutive = requiredConsecutive
  }

  public setBottleneck(link: string) {
    if (this.bottleneckLink !== link) {
      this.bottleneckLink = link
      this.reset()
    }
  }

  public reset() {
    this.detourActive = false
    this.detourStartSec = null
    this.consecutiveBelow = 0
    this.lastVerdict = 'IDLE'
  }

  public getVerdict(): VerifyVerdict {
    return this.lastVerdict
  }

  public evaluate(
    detourActive: boolean,
    currentUtil: number,
    capacity: number,
    nowSec: number = Date.now() / 1000,
    isSimulated = false,
  ): VerifyState {
    const currentLoadPct = Math.round((currentUtil / Math.max(0.1, capacity)) * 100)

    // State transition into Detour ON
    if (detourActive && !this.detourActive) {
      this.detourActive = true
      this.detourStartSec = nowSec
      this.beforeLoadPct = currentLoadPct > 0 ? currentLoadPct : 88
      this.consecutiveBelow = 0
      this.lastVerdict = 'PENDING'
    } else if (!detourActive && this.detourActive) {
      // Detour was turned off
      this.detourActive = false
      this.detourStartSec = null
      this.consecutiveBelow = 0
      this.lastVerdict = 'IDLE'
    }

    if (!this.detourActive || this.detourStartSec === null) {
      return {
        status: 'IDLE',
        passed: true,
        bottleneckLink: this.bottleneckLink,
        detourActive: false,
        detourStartSec: null,
        beforeLoadPct: this.beforeLoadPct,
        currentLoadPct,
        thresholdPct: this.thresholdPct,
        consecutiveBelow: 0,
        requiredConsecutive: this.requiredConsecutive,
        windowSec: 0,
        evalTimeSec: nowSec,
        message: isSimulated
          ? 'Simulated direct routing active (no detour deployed)'
          : 'Direct routing active (no detour deployed)',
        detail: {
          beforeText: 'n/a',
          currentText: `${fmtPct(currentUtil, capacity)} (${currentUtil.toFixed(1)} / ${capacity} Mbps)`,
          windowText: '0s',
          conditionText: isSimulated
            ? 'Normal simulated operations below threshold'
            : 'Normal operations below threshold',
        },
      }
    }

    const windowSec = Math.max(1, Math.round(nowSec - this.detourStartSec))
    const isBelow = currentLoadPct < this.thresholdPct

    if (isBelow) {
      this.consecutiveBelow += 1
    } else {
      this.consecutiveBelow = 0
    }

    let verdict: VerifyVerdict = 'PENDING'
    let message = ''
    const prefix = isSimulated ? 'Simulated verify: ' : ''
    const simSuffix = isSimulated ? ' (simulated)' : ''

    if (this.consecutiveBelow >= this.requiredConsecutive) {
      verdict = 'PASS'
      message = `${prefix}${fmtLinkLabel(this.bottleneckLink)}: ${this.beforeLoadPct}% → ${currentLoadPct}% after detour (${windowSec}s) — PASS${simSuffix}`
    } else if (!isBelow && windowSec >= 6) {
      verdict = 'FAIL'
      message = `${prefix}${fmtLinkLabel(this.bottleneckLink)}: still ${currentLoadPct}% (exceeds ${this.thresholdPct}% limit) after detour (${windowSec}s) — FAIL${simSuffix}`
    } else {
      verdict = 'PENDING'
      message = isBelow
        ? `${prefix}${fmtLinkLabel(this.bottleneckLink)}: verifying reduction (${this.consecutiveBelow}/${this.requiredConsecutive} samples < ${this.thresholdPct}%)...${simSuffix}`
        : `${prefix}${fmtLinkLabel(this.bottleneckLink)}: waiting for detour convergence (${windowSec}s)...${simSuffix}`
    }

    this.lastVerdict = verdict

    return {
      status: verdict,
      passed: verdict === 'PASS',
      bottleneckLink: this.bottleneckLink,
      detourActive: true,
      detourStartSec: this.detourStartSec,
      beforeLoadPct: this.beforeLoadPct,
      currentLoadPct,
      thresholdPct: this.thresholdPct,
      consecutiveBelow: this.consecutiveBelow,
      requiredConsecutive: this.requiredConsecutive,
      windowSec,
      evalTimeSec: nowSec,
      message,
      detail: {
        beforeText: `${this.beforeLoadPct}% (prior to detour)`,
        currentText: `${currentLoadPct}% (${fmtPct(currentUtil, capacity)})`,
        windowText: `${windowSec}s evaluation window`,
        conditionText:
          verdict === 'PASS'
            ? `Target sustained < ${this.thresholdPct}% for ${this.requiredConsecutive} consecutive cycles`
            : verdict === 'FAIL'
            ? `Bottleneck failed to drop below ${this.thresholdPct}% threshold`
            : `Stabilizing: ${this.consecutiveBelow}/${this.requiredConsecutive} cycles under threshold`,
      },
    }
  }
}
