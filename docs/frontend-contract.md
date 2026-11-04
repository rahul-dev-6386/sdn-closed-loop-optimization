# Frontend-to-Backend Contract: Closed-Loop Congestion Control

This document defines the formal data and API contract between the frontend dashboard (Vite / React) and the backend control plane (FastAPI / OpenFlow Controller / Telemetry Daemon).

---

## 1. Ground Rules & Schema Validation Principles

1. **Additive & Backward-Compatible**: No existing endpoint, field, or event name may be removed or renamed.
2. **Edge Validation**: All payloads received over HTTP or WebSocket are validated at the edge using schemas. Unknown fields are safely ignored. Invalid or malformed payloads produce an explicit `"bad_data"` error state; they are **never** silently coerced to `0` or default values.
3. **Canonical Link Keys**: Links are uniquely identified by their lexicographically sorted endpoint identifiers separated by a double dash (`--`):
   $$\text{canonicalLinkKey}(a, b) = [\min(a, b), \max(a, b)].\text{join}("--")$$
   For example, `s1--s2` and `s2--s1` always resolve to `s1--s2`.
4. **Link-Specific Capacity**: Utilization percentages are strictly calculated against the link's own declared capacity:
   $$\text{pct} = \text{round}\left(\frac{\text{used\_mbps}}{\text{capacity\_mbps}} \times 100\right)$$
   Capacity is **never** assumed to be a constant 10 Mbps.
5. **Single Formatter Module**: Formatting of units (`fmtMbps`, `fmtPct`, `fmtAge`, `fmtTime`) is centralized.
6. **Time Synchronization**: Server clock offset is calculated on every HTTP response or stream message:
   $$\Delta_{\text{clock}} = \text{server\_time} - \text{client\_time}$$
   Relative durations ("Xs ago") use client monotonic time corrected by $\Delta_{\text{clock}}$.

---

## 2. UI Element to Backend Field Mapping & Fallbacks

Legend:
- `[PRESENT]`: Field currently emitted by the backend.
- `[PROPOSED / NEEDED]`: Field required by the UI that the backend does not yet emit natively.
- `[FALLBACK]`: Guaranteed UI behavior when the field or endpoint is absent.

| UI Component | Field / Data | Endpoint / Stream Message | Backend Field Path | Status | Fallback if Missing |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Header** | App Title & Subtitle | Static + Catalog | `catalog.name`, `catalog.id` | `[PRESENT]` | "Closed-Loop Congestion Control", "Dynamic Fabric" |
| | Strategy Selector | `GET /api/status` | `status.strategy` or user selection | `[PRESENT]` | Default to `ml_aware` |
| | Pipeline Stepper | `GET /api/status`, `ws:snapshot` | `status.pipeline_stage`, `cycle_timestamp` | `[PROPOSED]` | Derive stage from recent event transitions; show `"n/a"` for cycle |
| | Stream Status Dot | WebSocket State | Client socket `readyState`, `last_sample_ts` | `[PRESENT]` | Transition to `STALE` if `> 3s`, `DOWN` if `> 15s` |
| | Controller Health Dot | `GET /api/status` | `status.controller.connected` | `[PRESENT]` | Show `DOWN` in red with retry tooltip |
| | Server Time Offset | HTTP Header / WS | `Date` header / `frame.timestamp` | `[PRESENT]` | Use client local clock if header is stripped |
| **Side Rail (Left)** | Switch List & Ports | `GET /api/topologies/{id}`, `GET /api/flows` | `switches[]`, `flows.switches[sw]` | `[PRESENT]` | Derive switch names from topology definition |
| | Forwarding Rules Count | `GET /api/flows` | `flows.switches[sw].forwarding` | `[PRESENT]` | Show active path route fallback `>= 1 fwd`, never `-` |
| | Total Switch Flows | `GET /api/flows` | `flows.switches[sw].count` | `[PRESENT]` | Fallback to `2 flows` (controller miss + probe) |
| | Switch Load Indicator | `ws:snapshot`, `GET /api/telemetry` | Max link utilization on switch ports | `[PRESENT]` | Show neutral grey dot if no telemetry received |
| | Host List & IP | `GET /api/topologies/{id}` | `hosts[].name`, `hosts[].ip`, `hosts[].switch` | `[PRESENT]` | Hide IP if missing, show host name |
| **Canvas (Topology)** | 2D/3D Node Coordinates | `GET /api/topologies/{id}` | `nodes[].x`, `nodes[].y`, `nodes[].layer` | `[PROPOSED]` | Deterministic tiered layout generator (spine, core, dist, access, hosts) |
| | Link Bandwidth / Capacity | `GET /api/topologies/{id}` | `links[].bw_mbps` | `[PRESENT]` | Default to `10.0` Mbps only if link spec omits `bw_mbps` |
| | Link Utilization (Mbps) | `ws:snapshot`, `GET /api/telemetry` | `frame.links[key].used_mbps` | `[PRESENT]` | Fallback to `"no data"`, show dimmed link |
| | Active Forwarding Path | `GET /api/status`, `ws:snapshot` | `status.active_path` or `frame.active_path` | `[PRESENT]` | Default to `default_path` from topology catalog |
| | Alternate Detour Path | `GET /api/topologies/{id}` | `alternate_path` | `[PRESENT]` | Disable detour toggle if no alternate path exists |
| | Congestion Spike Indicator | `POST /api/traffic/spike` | Client trigger state or `frame.spike_active` | `[PRESENT]` | Render warning pin at link midpoint |
| **Status Strip** | Bottleneck Link Name | `GET /api/topologies/{id}` | `bottleneck_link` | `[PRESENT]` | Compute link with maximum utilization |
| | Bottleneck Load Pill | `ws:snapshot`, `linkMetrics` | `used_mbps`, `capacity_mbps`, `pct` | `[PRESENT]` | Show `"no data"` if telemetry is not yet active |
| | Detour Action Summary | `GET /api/status` | `status.last_decision` | `[PROPOSED]` | Derive: `"Using path [path] ([util]%, [status])"` |
| | Post-Reroute Verify Text | `GET /api/status`, client ring | Measured post-reroute bottleneck samples | `[PROPOSED]` | Compute dynamically: PASS (stays `<80%` for 5s), FAIL, or PENDING |
| **Decision Card** | Active Path Sequence | Derived | `activePath.join(" → ")` | `[PRESENT]` | Show `"No active path"` |
| | Bottleneck Utilization | `linkMetrics[bottleneck]` | `used_mbps / capacity_mbps (pct%)` | `[PRESENT]` | Unified formatter matching chart and strip |
| | Verify Result Window | Client Evaluator | Before util, After util, samples count, duration | `[PROPOSED]` | Show `"PENDING (collecting samples...)"` |
| **Strip-Chart** | Time-Series Data Points | `ws:snapshot`, `GET /api/telemetry/history` | `points[].t`, `points[].util`, `points[].mbps` | `[PRESENT]` | Ring buffer (300 samples) per link keyed by topology |
| | Flow Breakdown | `ws:snapshot` | `baseline_mbps`, `spike_mbps`, `dropped_mbps` | `[PROPOSED]` | Derive carried vs spike from capacity and base load |
| | Timeline Annotations | `ws:snapshot`, Client events | `events[].timestamp`, `events[].kind` | `[PRESENT]` | Deduplicate and render in upper annotation lane |
| | Congestion Limit Line | Static Config / Topology | `threshold` (default 0.8) | `[PRESENT]` | `80% Congestion Limit ([capacity*0.8] Mbps)` on right edge |
| **All Links List** | Ranked Utilization List | `linkMetrics` | Array of all non-host links in topology | `[PRESENT]` | Render all links sorted descending by utilization % |
| **Node Inspector** | Selected Node / Link Details | User selection + Topology store | Port mapping, flows, traffic counters | `[PRESENT]` | Automatically clear when switching topologies |
| **Controller Events** | Event Feed | `GET /api/events`, `ws:events` | `events[].id`, `kind`, `summary`, `timestamp` | `[PRESENT]` | Derive decisions from state changes if omitted |
| | Stall Collapse | Client State Filter | Ongoing `telemetry_stall` consolidation | `[PRESENT]` | Display single active row: `"Telemetry stalled — Xs, ongoing"` |
| **OpenFlow Tables** | Switch Flow Rules | `GET /api/flows?topo_id={id}` | `switches[sw].entries[]` | `[PRESENT]` | Structured parser (priority, match, actions, packets, bytes, age) |
| | Forwarding vs System Rules | Parsed from entry | Action contains `output:X` vs `CONTROLLER`/`NORMAL` | `[PROPOSED]` | Filter out table-miss/probe rules from forwarding count |

---

## 3. Data Schemas (Edge Validation)

Incoming payloads are validated against the following specifications:

### 3.1 Topology Object (`GET /api/topologies/{id}`)
```typescript
interface TopologyContract {
  id: string
  name: string
  category?: string
  description?: string
  switches: string[]
  hosts: Array<{
    name: string
    ip: string
    switch: string
    x?: number
    y?: number
  }>
  links: Array<{
    a: string
    b: string
    bw_mbps: number       // Required. Link capacity in Mbps.
    delay_ms?: number
    host_link?: boolean
  }>
  default_path: string[]
  alternate_path: string[]
  bottleneck_link: string
  node_coordinates?: Record<string, { x: number; y: number; layer?: number }>
}
```

### 3.2 Telemetry Frame (`ws:snapshot`)
```typescript
interface TelemetrySnapshotContract {
  type: "snapshot"
  timestamp: number       // Epoch seconds (float)
  topology_id?: string    // ID of active topology
  links: Record<string, {
    used_mbps: number
    capacity_mbps: number
    util: number          // 0.0 to 1.0+
    baseline_mbps?: number
    spike_mbps?: number
    dropped_mbps?: number
  }>
  active_path?: string[]
  spike_active?: boolean
  detour_active?: boolean
  strategy?: string
}
```

### 3.3 Flow Table Entry Parser Output
```typescript
interface ParsedFlowRule {
  id: string
  raw: string
  cookie: string
  priority: number
  table: number
  durationSec: number
  packets: number
  bytes: number
  match: string
  actions: string
  isForwarding: boolean   // true if outputting to datapath port
  isDetour: boolean       // true if altered egress port for rerouting
  isSystem: boolean       // true if CONTROLLER probe or table-miss (priority 0)
}
```

---

## 4. Multi-Topology Store Architecture

To prevent cross-topology state leaks (such as stale node inspections or mismatched chart histories):

```
Store (by topology_id)
  ├── metadata: TopologyContract
  ├── linkMetrics: Record<CanonicalLinkKey, LinkLoadSummary>
  ├── linkHistory: Record<CanonicalLinkKey, RingBuffer<TracePoint, 300>>
  ├── activePath: string[]
  ├── selectedTargetLink: CanonicalLinkKey
  ├── selectedNodeOrLink: string | null
  └── verifyEvaluator: VerifyEvaluatorState
```

When `topology_id` changes:
1. All pending HTTP requests and subscriptions for the previous `topology_id` are cancelled via `AbortController`.
2. Selection, hover, inspector, and temporary chart markers are cleared.
3. Active target link is initialized to the new topology's declared `bottleneck_link`.
4. The canvas triggers `fitToView()` after the container re-renders.
