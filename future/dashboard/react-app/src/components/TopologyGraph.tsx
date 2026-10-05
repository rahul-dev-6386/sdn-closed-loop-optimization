/**
 * Topology graph.
 *
 * Drawn as SVG on a fixed grid rather than a force layout: this network has a
 * known, stable shape, and a layout that re-arranges on every render would make
 * the operator re-learn the picture several times a minute. Node positions come
 * from a deterministic slot assignment, so the graph is stable across updates.
 *
 * The graticule behind the graph echoes the trace's calibration grid, tying the
 * two panels into one instrument.
 */
import { useMemo } from 'react'
import type { Topology } from '../api'

interface Props {
  topology: Topology | null
  /** Switches carrying traffic right now. */
  activeLinks: Set<string>
  selected: string | null
  onSelect: (name: string | null) => void
}

const W = 620
const H = 360
const PAD_X = 62
const PAD_Y = 54

/**
 * Layered layout: BFS depth from the ingress switch becomes the x axis, and
 * siblings at the same depth spread on the y axis.
 *
 * A single horizontal row was the obvious first choice and it was wrong: it
 * collapses the diamond into a straight chain s1-s2-s3-s4, hiding the very loop
 * this panel exists to show. Layering reproduces the real shape -- s1 alone,
 * s2 and s3 side by side, s4 alone -- and generalises to the larger topologies
 * in P13, where the loop structure differs but the layering does not.
 */
function layout(topology: Pick<Topology, 'switches' | 'hosts' | 'links'>) {
  const positions = new Map<string, { x: number; y: number }>()
  const switches = topology.switches
  if (switches.length === 0) return positions

  const neighbours = new Map<string, Set<string>>()
  for (const s of switches) neighbours.set(s, new Set())
  for (const link of topology.links) {
    if (link.host_link) continue
    neighbours.get(link.a)?.add(link.b)
    neighbours.get(link.b)?.add(link.a)
  }

  // Root at the switch carrying the most hosts: that is the ingress in this
  // topology, and anchoring there keeps the drawing oriented as operators read it.
  const hostCount = new Map<string, number>()
  for (const h of topology.hosts) {
    hostCount.set(h.switch, (hostCount.get(h.switch) ?? 0) + 1)
  }
  const root = [...switches].sort(
    (a, b) => (hostCount.get(b) ?? 0) - (hostCount.get(a) ?? 0),
  )[0]

  // BFS depth.
  const depth = new Map<string, number>([[root, 0]])
  const queue = [root]
  while (queue.length) {
    const node = queue.shift()!
    for (const next of neighbours.get(node) ?? []) {
      if (!depth.has(next)) {
        depth.set(next, depth.get(node)! + 1)
        queue.push(next)
      }
    }
  }
  // Disconnected switches (shouldn't happen) fall to the last layer.
  const maxDepth = Math.max(0, ...depth.values())
  for (const s of switches) if (!depth.has(s)) depth.set(s, maxDepth)

  const layers = new Map<number, string[]>()
  for (const s of switches) {
    const d = depth.get(s)!
    layers.set(d, [...(layers.get(d) ?? []), s])
  }

  const maxLayer = Math.max(...layers.keys())
  const usableW = W - PAD_X * 2
  const usableH = H - PAD_Y * 2

  for (const [d, members] of layers) {
    const x = PAD_X + (usableW * (maxLayer === 0 ? 0.5 : d / maxLayer))
    members.forEach((name, i) => {
      // Single-member layers sit on the centre line; siblings spread symmetrically.
      const offset =
        members.length === 1
          ? 0
          : (i - (members.length - 1) / 2) * (usableH / (members.length + 0.6))
      positions.set(name, { x, y: H / 2 + offset })
    })
  }

  // Hosts sit outboard of their switch, above for the first, below for the rest.
  const seen = new Map<string, number>()
  for (const host of topology.hosts) {
    const anchor = positions.get(host.switch)
    if (!anchor) continue
    const n = seen.get(host.switch) ?? 0
    seen.set(host.switch, n + 1)
    positions.set(host.name, {
      x: anchor.x,
      y: anchor.y + (n % 2 === 0 ? -52 : 52),
    })
  }
  return positions
}

export function TopologyGraph({ topology, activeLinks, selected, onSelect }: Props) {
  const positions = useMemo(
    () => layout(topology ?? { switches: [], hosts: [], links: [] }),
    [topology],
  )

  if (!topology || topology.switches.length === 0) {
    return (
      <div className="notice">
        <span className="notice__head">No topology</span>
        <span className="notice__body">
          The API could not read <code>config.yaml</code>. Check that it exists at
          the path the API was started with.
        </span>
      </div>
    )
  }

  return (
    <svg
      className="graph"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label="Network topology"
      preserveAspectRatio="xMidYMid meet"
    >
      {/* Calibration grid -- structural, matching the trace panel. */}
      <g aria-hidden="true">
        {Array.from({ length: Math.floor(W / 40) + 1 }, (_, i) => (
          <line key={`v${i}`} className="trace__grid" x1={i * 40} y1={0} x2={i * 40} y2={H} />
        ))}
        {Array.from({ length: Math.floor(H / 40) + 1 }, (_, i) => (
          <line key={`h${i}`} className="trace__grid" x1={0} y1={i * 40} x2={W} y2={i * 40} />
        ))}
      </g>

      {/* Links */}
      <g>
        {topology.links.map((link, i) => {
          const from = positions.get(link.a)
          const to = positions.get(link.b)
          if (!from || !to) return null
          const key = [link.a, link.b].sort().join('--')
          const active = activeLinks.has(key)
          return (
            <line
              key={`${key}-${i}`}
              className="graph__link"
              data-active={active}
              data-carrying={active}
              x1={from.x}
              y1={from.y}
              x2={to.x}
              y2={to.y}
              strokeDasharray={link.host_link ? '2 3' : undefined}
            />
          )
        })}
      </g>

      {/* Nodes */}
      <g>
        {topology.switches.map((name) => {
          const p = positions.get(name)
          if (!p) return null
          const isSelected = selected === name
          return (
            <g
              key={name}
              className="graph__node"
              data-active={isSelected}
              onClick={() => onSelect(isSelected ? null : name)}
              style={{ cursor: 'pointer' }}
            >
              <title>{`${name} — switch`}</title>
              <circle cx={p.x} cy={p.y} r={13} />
              <text className="graph__label" x={p.x} y={p.y + 3.5} textAnchor="middle">
                {name}
              </text>
            </g>
          )
        })}

        {topology.hosts.map((host) => {
          const p = positions.get(host.name)
          if (!p) return null
          const isSelected = selected === host.name
          return (
            <g
              key={host.name}
              className="graph__node"
              data-active={isSelected}
              onClick={() => onSelect(isSelected ? null : host.name)}
              style={{ cursor: 'pointer' }}
            >
              <title>{`${host.name} — ${host.ip}`}</title>
              <circle cx={p.x} cy={p.y} r={9} />
              <text className="graph__label" x={p.x} y={p.y + 3} textAnchor="middle">
                {host.name}
              </text>
              <text className="graph__ip" x={p.x} y={p.y + 20} textAnchor="middle">
                {host.ip}
              </text>
            </g>
          )
        })}
      </g>

      {/* Port adjacency is NOT drawn here. It was tried as labels radiating from
          the selected node and they collided with the host names and IPs,
          crowding the one panel that has to stay legible at a glance. It is
          shown as a readout strip in the panel header instead. */}
    </svg>
  )
}