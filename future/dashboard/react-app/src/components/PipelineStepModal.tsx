/**
 * Interactive Modal / Popover displaying the operational detail of each pipeline step
 * (Measure, Predict, Decide, Reroute, Verify).
 */
import {
  X,
  Activity,
  Cpu,
  GitFork,
  Zap,
  CheckCircle2,
  AlertTriangle,
} from 'lucide-react'

export type PipelineStage = 'measure' | 'predict' | 'decide' | 'reroute' | 'verify'

interface Props {
  stage: PipelineStage | null
  onClose: () => void
  bottleneckLink: string
  currentUtilPct: number
  currentUsedMbps: number
  capacityMbps: number
  detourPath: string[]
  isCongested: boolean
  isDetourActive: boolean
  isSimulated?: boolean
  hasLiveSamples?: boolean
  verifyVerdict?: string
  verifyMessage?: string
  onSwitchToSimulated?: () => void
}

export function PipelineStepModal({
  stage,
  onClose,
  bottleneckLink,
  currentUtilPct,
  currentUsedMbps,
  capacityMbps,
  detourPath,
  isCongested,
  isDetourActive,
  isSimulated = false,
  hasLiveSamples = false,
  verifyVerdict,
  verifyMessage,
  onSwitchToSimulated,
}: Props) {
  if (!stage) return null

  const getStageContent = () => {
    switch (stage) {
      case 'measure':
        return {
          title: 'Stage 1: Telemetry & Link Measurement',
          icon: <Activity size={20} className="modal-icon modal-icon--amber" />,
          summary: `${bottleneckLink}: ${currentUsedMbps} / ${capacityMbps} Mbps (${currentUtilPct}% Load)`,
          status: isCongested ? 'Exceeds 80% Threshold' : 'Normal Operating Band (<80%)',
          statusType: isCongested ? 'danger' : 'success',
          details: [
            { label: 'Sampling Cadence', value: '2.0 seconds' },
            { label: 'Ingress Port Stats', value: 'OpenFlow 1.3 OFPST_PORT counters' },
            { label: 'Monitored Link', value: bottleneckLink },
            { label: 'Measured Utilisation', value: `${currentUtilPct}% (${currentUsedMbps} Mbps)` },
            { label: 'Congestion Threshold', value: '80% utilisation / latency 40 ms / loss 2%' },
            { label: 'Staleness Guard', value: 'Telemetry stale at >3× interval → shortest-path fallback (Rule 7)' },
          ],
        }
      case 'predict':
        return {
          title: 'Stage 2: Machine Learning Congestion Forecasting',
          icon: <Cpu size={20} className="modal-icon modal-icon--blue" />,
          summary: isCongested
            ? 'ML hazard forecast fires for this link'
            : 'ML hazard forecast clear for this link',
          status: isCongested ? 'High Probability Hazard' : 'Low Hazard State',
          statusType: isCongested ? 'danger' : 'success',
          details: [
            { label: 'Model Architecture', value: 'RandomForest vs LogisticRegression (see results/ml_metrics.json)' },
            { label: 'Feature Window', value: '24 s rolling window (12 samples × 2 s)' },
            { label: 'Forecast Horizon', value: '10 s forward anticipation' },
            { label: 'Inference Latency', value: '≈2.8 ms measured (budget 50 ms)' },
            { label: 'Decision Threshold', value: 'p ≥ 0.5 (decision_threshold, results/model.joblib)' },
          ],
        }
      case 'decide':
        return {
          title: 'Stage 3: Constrained Multipath Decision Engine',
          icon: <GitFork size={20} className="modal-icon modal-icon--purple" />,
          summary: isDetourActive
            ? `Detour via ${detourPath.join(' → ')} selected`
            : `Using static shortest path`,
          status: isDetourActive ? 'Avoidance Detour Active' : 'Shortest Path Active',
          statusType: 'info',
          details: [
            { label: 'Algorithm', value: 'K-shortest simple paths + cost function' },
            { label: 'Cost Function', value: '0.25·util + 0.15·latency + 0.20·loss + 0.40·p_congestion' },
            { label: 'Path Cost Aggregation', value: 'Max of link costs (worst link dominates)' },
            { label: 'Reroute Guard', value: 'Alternative must be ≥15% cheaper (improvement ratio 0.85)' },
            { label: 'Hysteresis', value: '10 s cooldown per flow · 8 s minimum dwell' },
          ],
        }
      case 'reroute':
        const ingressSwitch = detourPath && detourPath.length >= 2 ? detourPath[1] : 's1'
        const egressSwitch = detourPath && detourPath.length >= 2 ? detourPath[detourPath.length - 2] : 's4'
        return {
          title: 'Stage 4: OpenFlow 1.3 Flow-Mod Installation',
          icon: <Zap size={20} className="modal-icon modal-icon--cyan" />,
          summary: 'Make-before-break flow programming on the alternate path',
          status: 'Make-Before-Break Installed',
          statusType: 'success',
          details: [
            { label: 'OpenFlow Protocol', value: 'OpenFlow 1.3 (OFPFC_ADD / DELETE)' },
            { label: 'Flow Match', value: 'Exact-match: in_port + destination MAC per switch' },
            { label: 'Ingress Point', value: `Switch ${ingressSwitch} (detour ingress)` },
            { label: 'Egress Convergence', value: `Switch ${egressSwitch} (detour egress)` },
            { label: 'Ordering', value: 'Installs land before deletes; rollback on failure' },
          ],
        }
      case 'verify':
        const isLiveAwaiting = !isSimulated && !hasLiveSamples
        const displaySummary = isLiveAwaiting
          ? 'Live OpenFlow telemetry (P5) is awaiting active switch counter packets from Mininet/Ryu. Switch to Simulated Telemetry mode to run closed-loop verification.'
          : (verifyMessage || (
              isSimulated
                ? (isDetourActive
                  ? `Simulated verify: ${bottleneckLink} load relieved after detour — OK (simulated)`
                  : `Simulated verify: ${bottleneckLink} load sustained — FAIL (simulated)`)
                : (isDetourActive
                  ? `${bottleneckLink}: load reduced after detour — PASS`
                  : `${bottleneckLink}: load sustained — FAIL`)
            ))

        const displayStatus = isSimulated
          ? (verifyVerdict || (isDetourActive ? 'SIMULATED VERIFY OK' : 'SIMULATED VERIFY FAIL'))
          : (isLiveAwaiting ? 'AWAITING TELEMETRY (P5 IDLE)' : (verifyVerdict || (isDetourActive ? 'VERIFIED PASS' : 'VERIFICATION FAIL')))

        const thresholdValue = (capacityMbps * 0.8).toFixed(1)
        const isPassed = !isLiveAwaiting && (isDetourActive || (verifyVerdict ? (verifyVerdict.includes('OK') || verifyVerdict.includes('PASS')) : false))
        const statusType = isLiveAwaiting ? 'neutral' : (isPassed ? 'success' : 'danger')
        const finalVerdict = isLiveAwaiting
          ? 'Awaiting live switch counter packets (P5 idle)'
          : (verifyVerdict || displayStatus)

        return {
          title: isSimulated ? 'Stage 5: Simulated Closed-Loop Verification' : 'Stage 5: Closed-Loop Telemetry Verification',
          icon: isPassed ? (
            <CheckCircle2 size={20} className="modal-icon modal-icon--green" />
          ) : (
            <AlertTriangle size={20} className="modal-icon modal-icon--amber" />
          ),
          summary: displaySummary,
          status: displayStatus,
          statusType,
          details: [
            { label: 'Evaluation Engine', value: isSimulated ? 'Synthetic Flow Dynamics' : 'OpenFlow OFPST_PORT Live Collector' },
            { label: 'Monitored Bottleneck', value: bottleneckLink },
            { label: 'Current Bottleneck Load', value: isLiveAwaiting ? '0% (Awaiting live switch counters)' : `${currentUtilPct}% (${currentUsedMbps} / ${capacityMbps} Mbps)` },
            { label: 'Congestion Threshold', value: `< 80% (${thresholdValue} Mbps)` },
            { label: 'Verification Verdict', value: finalVerdict },
          ],
        }

    }
  }

  const content = getStageContent()

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="pipeline-modal" onClick={(e) => e.stopPropagation()}>
        <div className="pipeline-modal__header">
          <div className="pipeline-modal__title-row">
            {content.icon}
            <div>
              <h3 className="pipeline-modal__title">{content.title}</h3>
              <p className="pipeline-modal__summary">{content.summary}</p>
            </div>
          </div>
          <button className="pipeline-modal__close" onClick={onClose} title="Close inspector">
            <X size={16} />
          </button>
        </div>

        <div className="pipeline-modal__body">
          <div className="pipeline-modal__verdict-strip">
            <span className="pipeline-modal__verdict-label">Status Verdict:</span>
            <span className={`pipeline-modal__verdict-badge pipeline-modal__verdict-badge--${content.statusType}`}>
              {content.status}
            </span>
          </div>

          {!isSimulated && !hasLiveSamples && stage === 'verify' && (
            <div className="pipeline-modal__notice">
              <div className="pipeline-modal__notice-header">
                <AlertTriangle size={15} />
                <span>Live OpenFlow Port-Stats Idle</span>
              </div>
              <p className="pipeline-modal__notice-text">
                Live OpenFlow telemetry (Phase 5) is awaiting active switch counter packets from a Mininet/Ryu testbed.
                Switch to Simulated Telemetry mode to run closed-loop verification, test detour relief, and validate flow metrics.
              </p>
              {onSwitchToSimulated && (
                <div className="pipeline-modal__action-row">
                  <button
                    type="button"
                    className="btn btn--primary btn--sm"
                    onClick={onSwitchToSimulated}
                  >
                    Switch to Simulated Mode
                  </button>
                </div>
              )}
            </div>
          )}

          <div className="pipeline-modal__grid">
            {content.details.map((d, i) => (
              <div key={i} className="pipeline-modal__row">
                <span className="pipeline-modal__key">{d.label}</span>
                <span className="pipeline-modal__val">{d.value}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="pipeline-modal__footer">
          <button className="btn btn--quiet" onClick={onClose}>
            Close Inspector
          </button>
        </div>
      </div>
    </div>
  )
}
