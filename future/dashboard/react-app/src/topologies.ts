/**
 * Client-side catalog of 10 distinct SDN topologies.
 *
 * Implements Section 3.2:
 * - Deterministic, non-overlapping coordinates in [0, 800] x [0, 500] coordinate space.
 * - Explicit tiering: Spine over Leaf, Core over Distribution over Access.
 * - Hosts positioned on the outermost perimeters with clean offsets.
 * - Exact link capacities (bw_mbps) avoiding any default 10 Mbps assumptions.
 */

export interface HostDef {
  name: string
  ip: string
  switch: string
  x?: number
  y?: number
}

export interface LinkDef {
  a: string
  b: string
  bw_mbps: number
  delay_ms?: number
  host_link?: boolean
}

export interface NodeCoord {
  x: number
  y: number
  layer?: number
}

export interface TopologyDef {
  id: string
  name: string
  category: string
  description: string
  switches: string[]
  hosts: HostDef[]
  links: LinkDef[]
  defaultPath: string[]
  alternatePath: string[]
  bottleneckLink: string
  node_coordinates: Record<string, NodeCoord>
}

export const TOPOLOGIES: TopologyDef[] = [
  {
    id: 'diamond',
    name: 'Classic Diamond (4-Switch Multipath)',
    category: 'Multipath Benchmark',
    description: 'The canonical SDN multipath benchmark network. Path A via s2, Path B via s3.',
    switches: ['s1', 's2', 's3', 's4'],
    hosts: [
      { name: 'h1', ip: '10.0.0.1', switch: 's1' },
      { name: 'h4', ip: '10.0.0.4', switch: 's4' },
    ],
    links: [
      { a: 's1', b: 's2', bw_mbps: 10, delay_ms: 5 },
      { a: 's1', b: 's3', bw_mbps: 10, delay_ms: 5 },
      { a: 's2', b: 's4', bw_mbps: 10, delay_ms: 5 },
      { a: 's3', b: 's4', bw_mbps: 10, delay_ms: 5 },
      { a: 's1', b: 'h1', bw_mbps: 10, host_link: true },
      { a: 's4', b: 'h4', bw_mbps: 10, host_link: true },
    ],
    defaultPath: ['h1', 's1', 's2', 's4', 'h4'],
    alternatePath: ['h1', 's1', 's3', 's4', 'h4'],
    bottleneckLink: 's1--s2',
    node_coordinates: {
      s1: { x: 220, y: 250, layer: 0 },
      s2: { x: 400, y: 130, layer: 1 },
      s3: { x: 400, y: 370, layer: 1 },
      s4: { x: 580, y: 250, layer: 2 },
      h1: { x: 80, y: 250, layer: -1 },
      h4: { x: 720, y: 250, layer: 3 },
    },
  },
  {
    id: 'fat_tree',
    name: 'Fat-Tree Clos (8-Switch 2-Tier)',
    category: 'Data Center',
    description: 'Data center Clos fabric with 2 Core, 2 Aggregation, and 4 Edge switches.',
    switches: ['c1', 'c2', 'a1', 'a2', 'e1', 'e2', 'e3', 'e4'],
    hosts: [
      { name: 'h1', ip: '10.0.0.1', switch: 'e1' },
      { name: 'h2', ip: '10.0.0.2', switch: 'e2' },
      { name: 'h3', ip: '10.0.0.3', switch: 'e3' },
      { name: 'h4', ip: '10.0.0.4', switch: 'e4' },
    ],
    links: [
      { a: 'e1', b: 'a1', bw_mbps: 20, delay_ms: 2 },
      { a: 'e2', b: 'a1', bw_mbps: 20, delay_ms: 2 },
      { a: 'e3', b: 'a2', bw_mbps: 20, delay_ms: 2 },
      { a: 'e4', b: 'a2', bw_mbps: 20, delay_ms: 2 },
      { a: 'a1', b: 'c1', bw_mbps: 40, delay_ms: 1 },
      { a: 'a1', b: 'c2', bw_mbps: 40, delay_ms: 1 },
      { a: 'a2', b: 'c1', bw_mbps: 40, delay_ms: 1 },
      { a: 'a2', b: 'c2', bw_mbps: 40, delay_ms: 1 },
      { a: 'e1', b: 'h1', bw_mbps: 20, host_link: true },
      { a: 'e2', b: 'h2', bw_mbps: 20, host_link: true },
      { a: 'e3', b: 'h3', bw_mbps: 20, host_link: true },
      { a: 'e4', b: 'h4', bw_mbps: 20, host_link: true },
    ],
    defaultPath: ['h1', 'e1', 'a1', 'c1', 'a2', 'e4', 'h4'],
    alternatePath: ['h1', 'e1', 'a1', 'c2', 'a2', 'e4', 'h4'],
    bottleneckLink: 'a1--c1',
    node_coordinates: {
      c1: { x: 300, y: 90, layer: 0 },
      c2: { x: 500, y: 90, layer: 0 },
      a1: { x: 300, y: 220, layer: 1 },
      a2: { x: 500, y: 220, layer: 1 },
      e1: { x: 140, y: 350, layer: 2 },
      e2: { x: 320, y: 350, layer: 2 },
      e3: { x: 480, y: 350, layer: 2 },
      e4: { x: 660, y: 350, layer: 2 },
      h1: { x: 140, y: 445, layer: 3 },
      h2: { x: 320, y: 445, layer: 3 },
      h3: { x: 480, y: 445, layer: 3 },
      h4: { x: 660, y: 445, layer: 3 },
    },
  },
  {
    id: 'spine_leaf',
    name: 'Spine-Leaf Fabric (6-Switch ECMP)',
    category: 'Cloud Fabric',
    description: '2 Spines and 4 Leaves providing non-blocking East-West traffic flow.',
    switches: ['sp1', 'sp2', 'lf1', 'lf2', 'lf3', 'lf4'],
    hosts: [
      { name: 'h1', ip: '10.0.0.1', switch: 'lf1' },
      { name: 'h4', ip: '10.0.0.4', switch: 'lf4' },
    ],
    links: [
      { a: 'lf1', b: 'sp1', bw_mbps: 25, delay_ms: 1.5 },
      { a: 'lf1', b: 'sp2', bw_mbps: 25, delay_ms: 1.5 },
      { a: 'lf2', b: 'sp1', bw_mbps: 25, delay_ms: 1.5 },
      { a: 'lf2', b: 'sp2', bw_mbps: 25, delay_ms: 1.5 },
      { a: 'lf3', b: 'sp1', bw_mbps: 25, delay_ms: 1.5 },
      { a: 'lf3', b: 'sp2', bw_mbps: 25, delay_ms: 1.5 },
      { a: 'lf4', b: 'sp1', bw_mbps: 25, delay_ms: 1.5 },
      { a: 'lf4', b: 'sp2', bw_mbps: 25, delay_ms: 1.5 },
      { a: 'lf1', b: 'h1', bw_mbps: 25, host_link: true },
      { a: 'lf4', b: 'h4', bw_mbps: 25, host_link: true },
    ],
    defaultPath: ['h1', 'lf1', 'sp1', 'lf4', 'h4'],
    alternatePath: ['h1', 'lf1', 'sp2', 'lf4', 'h4'],
    bottleneckLink: 'lf1--sp1',
    node_coordinates: {
      sp1: { x: 280, y: 120, layer: 0 },
      sp2: { x: 520, y: 120, layer: 0 },
      lf1: { x: 140, y: 330, layer: 1 },
      lf2: { x: 310, y: 330, layer: 1 },
      lf3: { x: 490, y: 330, layer: 1 },
      lf4: { x: 660, y: 330, layer: 1 },
      h1: { x: 50, y: 330, layer: 1 },
      h4: { x: 750, y: 330, layer: 1 },
    },
  },
  {
    id: 'highway_bypass',
    name: 'Highway with Express Bypass (6-Switch)',
    category: 'Highway / Trunk',
    description: 'Linear arterial route with an express flyover bypass spanning intermediate junctions.',
    switches: ['s1', 's2', 's3', 's4', 's5', 's6'],
    hosts: [
      { name: 'h1', ip: '10.0.0.1', switch: 's1' },
      { name: 'h6', ip: '10.0.0.6', switch: 's6' },
    ],
    links: [
      { a: 's1', b: 's2', bw_mbps: 10, delay_ms: 3 },
      { a: 's2', b: 's3', bw_mbps: 10, delay_ms: 3 },
      { a: 's3', b: 's4', bw_mbps: 10, delay_ms: 3 },
      { a: 's4', b: 's5', bw_mbps: 10, delay_ms: 3 },
      { a: 's5', b: 's6', bw_mbps: 10, delay_ms: 3 },
      { a: 's2', b: 's5', bw_mbps: 25, delay_ms: 6 },
      { a: 's1', b: 'h1', bw_mbps: 10, host_link: true },
      { a: 's6', b: 'h6', bw_mbps: 10, host_link: true },
    ],
    defaultPath: ['h1', 's1', 's2', 's3', 's4', 's5', 's6', 'h6'],
    alternatePath: ['h1', 's1', 's2', 's5', 's6', 'h6'],
    bottleneckLink: 's3--s4',
    node_coordinates: {
      s1: { x: 120, y: 320, layer: 0 },
      s2: { x: 230, y: 320, layer: 1 },
      s3: { x: 350, y: 320, layer: 2 },
      s4: { x: 470, y: 320, layer: 3 },
      s5: { x: 590, y: 320, layer: 4 },
      s6: { x: 700, y: 320, layer: 5 },
      h1: { x: 40, y: 320, layer: -1 },
      h6: { x: 770, y: 320, layer: 6 },
    },
  },
  {
    id: 'double_diamond',
    name: 'Double Diamond (7-Switch Dual-Stage)',
    category: 'Multi-Stage Bottleneck',
    description: 'Two consecutive diamond bottlenecks in series, providing 4 combinatorial routing paths.',
    switches: ['s1', 's2', 's3', 's4', 's5', 's6', 's7'],
    hosts: [
      { name: 'h1', ip: '10.0.0.1', switch: 's1' },
      { name: 'h7', ip: '10.0.0.7', switch: 's7' },
    ],
    links: [
      { a: 's1', b: 's2', bw_mbps: 10, delay_ms: 4 },
      { a: 's1', b: 's3', bw_mbps: 10, delay_ms: 4 },
      { a: 's2', b: 's4', bw_mbps: 10, delay_ms: 4 },
      { a: 's3', b: 's4', bw_mbps: 10, delay_ms: 4 },
      { a: 's4', b: 's5', bw_mbps: 10, delay_ms: 4 },
      { a: 's4', b: 's6', bw_mbps: 10, delay_ms: 4 },
      { a: 's5', b: 's7', bw_mbps: 10, delay_ms: 4 },
      { a: 's6', b: 's7', bw_mbps: 10, delay_ms: 4 },
      { a: 's1', b: 'h1', bw_mbps: 10, host_link: true },
      { a: 's7', b: 'h7', bw_mbps: 10, host_link: true },
    ],
    defaultPath: ['h1', 's1', 's2', 's4', 's5', 's7', 'h7'],
    alternatePath: ['h1', 's1', 's3', 's4', 's6', 's7', 'h7'],
    bottleneckLink: 's1--s2',
    node_coordinates: {
      s1: { x: 160, y: 250, layer: 0 },
      s2: { x: 290, y: 140, layer: 1 },
      s3: { x: 290, y: 360, layer: 1 },
      s4: { x: 420, y: 250, layer: 2 },
      s5: { x: 550, y: 140, layer: 3 },
      s6: { x: 550, y: 360, layer: 3 },
      s7: { x: 680, y: 250, layer: 4 },
      h1: { x: 70, y: 250, layer: -1 },
      h7: { x: 770, y: 250, layer: 5 },
    },
  },
  {
    id: 'ladder_grid',
    name: 'Ladder / 2x3 Grid (6-Switch)',
    category: 'Grid / Mesh',
    description: 'Two parallel rails connected by transversal rungs, mimicking city grid road systems.',
    switches: ['t1', 't2', 't3', 'b1', 'b2', 'b3'],
    hosts: [
      { name: 'h1', ip: '10.0.0.1', switch: 't1' },
      { name: 'h2', ip: '10.0.0.2', switch: 'b3' },
    ],
    links: [
      { a: 't1', b: 't2', bw_mbps: 15, delay_ms: 3 },
      { a: 't2', b: 't3', bw_mbps: 15, delay_ms: 3 },
      { a: 'b1', b: 'b2', bw_mbps: 15, delay_ms: 3 },
      { a: 'b2', b: 'b3', bw_mbps: 15, delay_ms: 3 },
      { a: 't1', b: 'b1', bw_mbps: 10, delay_ms: 4 },
      { a: 't2', b: 'b2', bw_mbps: 10, delay_ms: 4 },
      { a: 't3', b: 'b3', bw_mbps: 10, delay_ms: 4 },
      { a: 't1', b: 'h1', bw_mbps: 15, host_link: true },
      { a: 'b3', b: 'h2', bw_mbps: 15, host_link: true },
    ],
    defaultPath: ['h1', 't1', 't2', 't3', 'b3', 'h2'],
    alternatePath: ['h1', 't1', 'b1', 'b2', 'b3', 'h2'],
    bottleneckLink: 't2--t3',
    node_coordinates: {
      t1: { x: 220, y: 160, layer: 0 },
      t2: { x: 410, y: 160, layer: 1 },
      t3: { x: 600, y: 160, layer: 2 },
      b1: { x: 220, y: 340, layer: 0 },
      b2: { x: 410, y: 340, layer: 1 },
      b3: { x: 600, y: 340, layer: 2 },
      h1: { x: 90, y: 160, layer: -1 },
      h2: { x: 730, y: 340, layer: 3 },
    },
  },
  {
    id: 'ring_chords',
    name: 'Ring with Cross Chords (6-Switch)',
    category: 'Metro / Ring',
    description: 'Metropolitan ring network with diametrical cross-chords for rapid detour around congested sectors.',
    switches: ['r1', 'r2', 'r3', 'r4', 'r5', 'r6'],
    hosts: [
      { name: 'h1', ip: '10.0.0.1', switch: 'r1' },
      { name: 'h4', ip: '10.0.0.4', switch: 'r4' },
    ],
    links: [
      { a: 'r1', b: 'r2', bw_mbps: 12, delay_ms: 3.5 },
      { a: 'r2', b: 'r3', bw_mbps: 12, delay_ms: 3.5 },
      { a: 'r3', b: 'r4', bw_mbps: 12, delay_ms: 3.5 },
      { a: 'r4', b: 'r5', bw_mbps: 12, delay_ms: 3.5 },
      { a: 'r5', b: 'r6', bw_mbps: 12, delay_ms: 3.5 },
      { a: 'r6', b: 'r1', bw_mbps: 12, delay_ms: 3.5 },
      { a: 'r1', b: 'r4', bw_mbps: 20, delay_ms: 5 },
      { a: 'r2', b: 'r5', bw_mbps: 20, delay_ms: 5 },
      { a: 'r1', b: 'h1', bw_mbps: 12, host_link: true },
      { a: 'r4', b: 'h4', bw_mbps: 12, host_link: true },
    ],
    defaultPath: ['h1', 'r1', 'r2', 'r3', 'r4', 'h4'],
    alternatePath: ['h1', 'r1', 'r4', 'h4'],
    bottleneckLink: 'r2--r3',
    node_coordinates: {
      r1: { x: 190, y: 250, layer: 0 },
      r2: { x: 290, y: 130, layer: 1 },
      r3: { x: 510, y: 130, layer: 2 },
      r4: { x: 610, y: 250, layer: 3 },
      r5: { x: 510, y: 370, layer: 2 },
      r6: { x: 290, y: 370, layer: 1 },
      h1: { x: 80, y: 250, layer: -1 },
      h4: { x: 720, y: 250, layer: 4 },
    },
  },
  {
    id: 'full_mesh',
    name: 'Full Mesh K5 (5-Switch All-to-All)',
    category: 'High Redundancy Mesh',
    description: 'Fully connected 5-node graph with 10 bidirectional links, maximizing path diversity.',
    switches: ['m1', 'm2', 'm3', 'm4', 'm5'],
    hosts: [
      { name: 'h1', ip: '10.0.0.1', switch: 'm1' },
      { name: 'h5', ip: '10.0.0.5', switch: 'm5' },
    ],
    links: [
      { a: 'm1', b: 'm2', bw_mbps: 10, delay_ms: 2.5 },
      { a: 'm1', b: 'm3', bw_mbps: 10, delay_ms: 2.5 },
      { a: 'm1', b: 'm4', bw_mbps: 10, delay_ms: 2.5 },
      { a: 'm1', b: 'm5', bw_mbps: 10, delay_ms: 2.5 },
      { a: 'm2', b: 'm3', bw_mbps: 10, delay_ms: 2.5 },
      { a: 'm2', b: 'm4', bw_mbps: 10, delay_ms: 2.5 },
      { a: 'm2', b: 'm5', bw_mbps: 10, delay_ms: 2.5 },
      { a: 'm3', b: 'm4', bw_mbps: 10, delay_ms: 2.5 },
      { a: 'm3', b: 'm5', bw_mbps: 10, delay_ms: 2.5 },
      { a: 'm4', b: 'm5', bw_mbps: 10, delay_ms: 2.5 },
      { a: 'm1', b: 'h1', bw_mbps: 10, host_link: true },
      { a: 'm5', b: 'h5', bw_mbps: 10, host_link: true },
    ],
    defaultPath: ['h1', 'm1', 'm5', 'h5'],
    alternatePath: ['h1', 'm1', 'm3', 'm5', 'h5'],
    bottleneckLink: 'm1--m5',
    node_coordinates: {
      m1: { x: 220, y: 180, layer: 0 },
      m2: { x: 400, y: 110, layer: 1 },
      m3: { x: 580, y: 180, layer: 2 },
      m4: { x: 510, y: 360, layer: 2 },
      m5: { x: 290, y: 360, layer: 1 },
      h1: { x: 100, y: 180, layer: -1 },
      h5: { x: 170, y: 360, layer: -1 },
    },
  },
  {
    id: 'campus_wan',
    name: 'Enterprise Campus (Core-Dist-Access)',
    category: 'Enterprise Campus',
    description: 'Multi-tier enterprise campus with redundant Core, Distribution, and Access switches.',
    switches: ['core1', 'core2', 'dist1', 'dist2', 'acc1', 'acc2'],
    hosts: [
      { name: 'h1', ip: '10.0.0.1', switch: 'acc1' },
      { name: 'h2', ip: '10.0.0.2', switch: 'acc2' },
    ],
    links: [
      { a: 'acc1', b: 'dist1', bw_mbps: 10, delay_ms: 2 },
      { a: 'acc1', b: 'dist2', bw_mbps: 10, delay_ms: 2 },
      { a: 'acc2', b: 'dist1', bw_mbps: 10, delay_ms: 2 },
      { a: 'acc2', b: 'dist2', bw_mbps: 10, delay_ms: 2 },
      { a: 'dist1', b: 'core1', bw_mbps: 40, delay_ms: 1 },
      { a: 'dist1', b: 'core2', bw_mbps: 40, delay_ms: 1 },
      { a: 'dist2', b: 'core1', bw_mbps: 40, delay_ms: 1 },
      { a: 'dist2', b: 'core2', bw_mbps: 40, delay_ms: 1 },
      { a: 'core1', b: 'core2', bw_mbps: 100, delay_ms: 0.5 },
      { a: 'acc1', b: 'h1', bw_mbps: 10, host_link: true },
      { a: 'acc2', b: 'h2', bw_mbps: 10, host_link: true },
    ],
    defaultPath: ['h1', 'acc1', 'dist1', 'core1', 'dist2', 'acc2', 'h2'],
    alternatePath: ['h1', 'acc1', 'dist2', 'core2', 'dist1', 'acc2', 'h2'],
    bottleneckLink: 'dist1--core1',
    node_coordinates: {
      core1: { x: 290, y: 110, layer: 0 },
      core2: { x: 510, y: 110, layer: 0 },
      dist1: { x: 290, y: 240, layer: 1 },
      dist2: { x: 510, y: 240, layer: 1 },
      acc1: { x: 190, y: 370, layer: 2 },
      acc2: { x: 610, y: 370, layer: 2 },
      h1: { x: 80, y: 370, layer: 3 },
      h2: { x: 720, y: 370, layer: 3 },
    },
  },
  {
    id: 'abilene_wan',
    name: 'Abilene / Internet2 WAN Backbone (9-City)',
    category: 'National WAN Backbone',
    description: 'Modeled after the US research Internet2 backbone connecting Seattle, SF, Denver, Chicago, NYC, Atlanta, Dallas.',
    switches: ['SEA', 'SFO', 'LAX', 'DEN', 'ORD', 'DFW', 'ATL', 'WDC', 'NYC'],
    hosts: [
      { name: 'h_west', ip: '10.0.0.1', switch: 'SFO' },
      { name: 'h_east', ip: '10.0.0.2', switch: 'NYC' },
    ],
    links: [
      { a: 'SEA', b: 'SFO', bw_mbps: 10, delay_ms: 12 },
      { a: 'SFO', b: 'LAX', bw_mbps: 10, delay_ms: 8 },
      { a: 'SEA', b: 'DEN', bw_mbps: 10, delay_ms: 18 },
      { a: 'LAX', b: 'DEN', bw_mbps: 10, delay_ms: 16 },
      { a: 'LAX', b: 'DFW', bw_mbps: 10, delay_ms: 20 },
      { a: 'DEN', b: 'ORD', bw_mbps: 10, delay_ms: 15 },
      { a: 'DFW', b: 'ATL', bw_mbps: 10, delay_ms: 14 },
      { a: 'ORD', b: 'NYC', bw_mbps: 10, delay_ms: 11 },
      { a: 'ATL', b: 'WDC', bw_mbps: 10, delay_ms: 9 },
      { a: 'WDC', b: 'NYC', bw_mbps: 10, delay_ms: 5 },
      { a: 'SFO', b: 'h_west', bw_mbps: 10, host_link: true },
      { a: 'NYC', b: 'h_east', bw_mbps: 10, host_link: true },
    ],
    defaultPath: ['h_west', 'SFO', 'SEA', 'DEN', 'ORD', 'NYC', 'h_east'],
    alternatePath: ['h_west', 'SFO', 'LAX', 'DFW', 'ATL', 'WDC', 'NYC', 'h_east'],
    bottleneckLink: 'DEN--ORD',
    node_coordinates: {
      SEA: { x: 140, y: 120, layer: 0 },
      SFO: { x: 100, y: 250, layer: 0 },
      LAX: { x: 140, y: 380, layer: 0 },
      DEN: { x: 300, y: 220, layer: 1 },
      DFW: { x: 420, y: 360, layer: 2 },
      ORD: { x: 510, y: 180, layer: 2 },
      ATL: { x: 600, y: 330, layer: 3 },
      WDC: { x: 670, y: 240, layer: 3 },
      NYC: { x: 710, y: 150, layer: 4 },
      h_west: { x: 40, y: 250, layer: -1 },
      h_east: { x: 770, y: 150, layer: 5 },
    },
  },
]

export function getTopologyById(id: string): TopologyDef | undefined {
  return TOPOLOGIES.find((t) => t.id === id)
}
