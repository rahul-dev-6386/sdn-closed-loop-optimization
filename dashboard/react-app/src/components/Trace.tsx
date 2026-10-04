/**
 * Strip-chart trace of link utilisation with the model's forecast.
 *
 * The background grid is a real calibration grid: the horizontal rules are
 * capacity divisions, and the dashed line is the configured congestion
 * threshold read from the API. Both carry meaning, which is the whole reason
 * the panel looks like an instrument rather than a generic chart.
 *
 * Until P5 telemetry exists there is no series to plot, and the panel says so
 * plainly instead of drawing an empty axis.
 */
import { useMemo } from 'react'

export interface TracePoint {
  t: number
  util: number
  predicted?: number
  link?: string
}

interface Props {
  points: TracePoint[]
  /** Congestion threshold in [0,1]; drawn as the reference line. */
  threshold: number
  linkCapacityMbps?: number | null
}

const W = 520
const H = 330
const PAD_L = 34
const PAD_R = 12
const PAD_T = 12
const PAD_B = 22

export function Trace({ points, threshold, linkCapacityMbps }: Props) {
  const innerW = W - PAD_L - PAD_R
  const innerH = H - PAD_T - PAD_B

  const { path, area, modelPath, last } = useMemo(() => {
    if (points.length < 2) {
      return { path: '', area: '', modelPath: '', last: null as TracePoint | null }
    }
    const t0 = points[0].t
    const t1 = points[points.length - 1].t
    const span = Math.max(t1 - t0, 1)

    const x = (t: number) => PAD_L + ((t - t0) / span) * innerW
    // Scale to 110% of the threshold so the reference line is visible but the
    // curve can still overshoot it when a link is genuinely overloaded.
    const ymax = Math.min(Math.max(threshold * 1.25, 0.25), 1)
    const y = (v: number) => PAD_T + innerH - Math.min(v, ymax) / ymax * innerH

    const d = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.t).toFixed(1)},${y(p.util).toFixed(1)}`).join(' ')
    const a = `${d} L${x(t1).toFixed(1)},${PAD_T + innerH} L${x(t0).toFixed(1)},${PAD_T + innerH} Z`
    const m = points
      .filter((p) => p.predicted !== undefined)
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.t).toFixed(1)},${y(p.predicted!).toFixed(1)}`)
      .join(' ')
    return { path: d, area: a, modelPath: m, last: points[points.length - 1] }
  }, [points, threshold, innerW, innerH])

  const ymax = Math.min(Math.max(threshold * 1.25, 0.25), 1)
  const yOf = (v: number) => PAD_T + innerH - Math.min(v, ymax) / ymax * innerH

  return (
    <svg className="trace" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Link utilisation over time">
      {/* Graticule: minor divisions, then the 0/50/100% majors. */}
      <g aria-hidden="true">
        {[0, 0.25, 0.5, 0.75, 1].map((f) => (
          <line
            key={f}
            className={f === 0.5 ? 'trace__grid trace__grid--major' : 'trace__grid'}
            x1={PAD_L} x2={W - PAD_R} y1={yOf(f)} y2={yOf(f)}
          />
        ))}
        {Array.from({ length: 9 }, (_, i) => (
          <line key={i} className="trace__grid" x1={PAD_L + (i * innerW) / 8} x2={PAD_L + (i * innerW) / 8} y1={PAD_T} y2={PAD_T + innerH} />
        ))}
      </g>

      {/* Congestion threshold -- the number the whole system is tuned against. */}
      <line className="trace__threshold" x1={PAD_L} x2={W - PAD_R} y1={yOf(threshold)} y2={yOf(threshold)} />
      <text className="trace__axis" x={PAD_L - 4} y={yOf(threshold) + 3} textAnchor="end">
        {`${Math.round(threshold * 100)}%`}
      </text>

      {/* Y axis */}
      <text className="trace__axis" x={PAD_L - 4} y={PAD_T + 4} textAnchor="end">100</text>
      <text className="trace__axis" x={PAD_L - 4} y={PAD_T + innerH + 3} textAnchor="end">0</text>

      {path ? (
        <>
          <path className="trace__area" d={area} />
          {modelPath && <path className="trace__series trace__series--model" d={modelPath} />}
          <path className="trace__series" d={path} />
        </>
      ) : (
        <text className="trace__empty" x={W / 2} y={H / 2} textAnchor="middle">
          No telemetry yet — P5 not running
        </text>
      )}

      {last && (
        <text className="trace__axis" x={W - PAD_R} y={PAD_T + 10} textAnchor="end">
          {last.link ? `${last.link} · ${(last.util * 100).toFixed(0)}%` : `${(last.util * 100).toFixed(0)}%`}
          {linkCapacityMbps ? ` of ${linkCapacityMbps} Mbps` : ''}
        </text>
      )}
    </svg>
  )
}