# Agentic AI for SDN Congestion Detection & Remediation
## LangGraph-Based Autonomous Remediation Layer

### 0. Isolation Guarantee (Critical)

**The agentic AI feature is entirely separate from the existing SDN control plane. No existing functionality is modified, deprecated, or relied upon.**

**Isolation boundaries:**

| Boundary | Implementation |
|----------|---------------|
| **Runtime isolation** | Agent runs as a separate process/context; if it crashes, the existing controller continues unaffected |
| **Code isolation** | New modules added under `agentic/` directory; existing `controller/`, `ml/`, `tests/` untouched |
| **API isolation** | New `POST /api/agent/` endpoints added alongside existing `/`api/` routes; no existing endpoints modified |
| **Database isolation** | Agent uses in-memory state + optional separate SQLite; existing `data/sdn.db` schema unchanged |
| **Test isolation** | All 637 existing tests remain hermetic; agent features have separate test suite |
| **Deployment isolation** | Agent can be toggled ON/OFF via feature flag; default OFF in production |

**Feature flag:**
- Environment variable: `ENABLE_AGENTIC_AI=false` (default)
- When OFF: all `/api/agent/*` endpoints return 404, agent graph is never initialized
- When ON: agent operates as designed, full functionality

**Zero-degradation guarantee:**
- Existing `make test` → 637 passed, 0 failed (unchanged)
- Existing `make verify-p5-live` → unchanged behavior
- Existing controller → no performance impact from agent being present (agent is idle when feature flag OFF)

### 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Human Operator                            │
│  "Fix congestion on s1--s2"  ───→  LangGraph Agent  ───→       │
│                                       │                      │
│                                       ▼                      │
│                            OpenRouter LLM                      │
│                                      │                       │
│                                      ▼                       │
│                            Decision Logic                      │
│                                       │                       │
│                ┌─────────────────────────┴─────────────────────┐│
│                │                                         ││
│                ▼                                         ▼│
│      Existing SDN Control Plane                                │
│  ┌─────────────┐  ┌─────────────────┐  ┌─────────────────┐ │
│  │ ryu-manager │  │ topology.py   │  │ telemetry.py    │ │
│  │ (controller)│  │ (NetworkX)    │  │ (port-stats)    │ │
│  └─────────────┘  └─────────────────┘  └─────────────────┘ │
│          │                   │                   │        │
│          │                   │                   │        │
│          ▼                   ▼                   ▼        ▼
│  ┌───────────────────────────────────────────────────────┐ │
│  │ OpenFlow 1.3 Datapaths → Flow Rules → Forwarding     │ │
│  └───────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### 3. Agent Observability (What the Agent Can See)

The agent has access to real-time telemetry data collected by P5:

| Metric | Source | Description |
|--------|--------|-------------|
| `utilization_pct` | Port stats | TX rate / capacity * 100 |
| `tx_rate_mbps` | Port stats | Transmitted rate in Mbps |
| `rx_rate_mbps` | Port stats | Received rate in Mbps |
| `latency_ms` | Active probes | RTT between switch pair |
| `loss_pct` | Port stats + probes | Drop/error + probe loss percentage |
| `is_stale` | Telemetry | Sample older than stall threshold |
| `congested` | Ground truth | utilization >= 80% OR loss >= 2% OR latency >= 40ms |

The agent also sees:
- **Topology**: switch attachments, link capacities, bottleneck links
- **Current routing mode**: static_shortest, ecmp, reactive_threshold, ml_aware
- **Active detour status**: whether a detour is currently installed
- **Verify engine state**: PASS/FAIL/PENDING status of post-reroute verification

### 4. Agent Actions (What the Agent Can Do)

The agent can trigger these actions through the existing API:

| Action | API Endpoint | Parameters | Effect |
|--------|-------------|------------|--------|
| `set_controller_mode` | `POST /api/controller/mode` | `mode` (ml_aware/ecmp/reactive_threshold/static_shortest) | Changes the routing strategy |
| `toggle_detour` | `POST /api/spike` (with src/dst/mbps/proto) | `src`, `dst`, `mbps`, `proto` | Installs/removes detour flows |
| `spike_start` / `spike_stop` | `POST/DELETE /api/traffic/spike` | Rate, duration, src, dst, proto | Injects test traffic for probing |
| `refresh_backend_state` | `GET /api/topology`, `GET /api/status` | topo_id | Pulls fresh topology/status |
| `predict_congestion` | `GET /api/predictions` | — | Returns ML prediction availability |

### 5. LangGraph State Machine

The agent uses a **structured state graph** with the following states:

```python
from typing import TypedDict, Literal
import operator

class AgentState(TypedDict):
    # Observed metrics
    utilization_pct: float
    tx_rate_mbps: float
    latency_ms: float
    loss_pct: float
    is_stale: bool
    
    # Context
    bottleneck_link: str
    topology_id: str
    routing_mode: str
    is_detour_active: bool
    
    # Decision
    action: Literal['NONE', 'SWITCH_MODE', 'TOGGLE_DETOUR', 'SPIKE_AND_PROBE', 'VERIFY']
    reason: str
    
    # Execution results
    previous_mode: str
    verify_result: Literal['PASS', 'FAIL', 'PENDING', 'IDLE']
    
    # LLM output
    llm_raw_response: str
    confidence: float

def should_continue(state: AgentState) -> bool:
    """Conditional edge: continue if we need verification, otherwise stop."""
    if state['action'] == 'VERIFY':
        return state['verify_result'] == 'PASS'
    return True
```

**Graph edges:**
1. `START` → `OBSERVE` (agent reads current metrics)
2. `OBSERVE` → `REASON` (LLM decides action)
3. `REASON` → `ACT` (execute chosen action)
4. `ACT` → `VERIFY` (wait for post-remediation samples)
5. `VERIFY` → `COMPLETE` (if PASS) or `REASON` again (if FAIL/PENDING)
6. `COMPLETE` → `END`

### 6. LLM Prompt Design

The agent prompts the LLM with a **structured, constrained prompt**:

```
You are an SDN network congestion remediation agent. Your job is to detect and fix network congestion in a Software Defined Network.

OBSERVED METRICS (current state):
- Bottleneck link: s1--s2
- Current utilization: 85.2% (threshold: 80%)
- TX rate: 10.1 Mbps (capacity: 10.0 Mbps)
- RX rate: 9.8 Mbps
- Latency: 12.4 ms (baseline: 5.0 ms)
- Loss: 1.2% (threshold: 2.0%)
- Is stale: False
- Current routing mode: ml_aware
- Detour active: False

AVAILABLE ACTIONS:
1. SWITCH_MODE — Change routing mode to one of: 
   - "static_shortest" (fixed single path)
   - "ecmp" (equal-cost multi-path)
   - "reactive_threshold" (wait for 80% then reroute)
   - "ml_aware" (proactive ML-based rerouting)
   
2. TOGGLE_DETOUR — Install or remove a detour around the bottleneck
   
3. SPIKE_AND_PROBE — Inject test traffic (14 Mbps UDP on h1->h4) and measure
   latency/utilization response (for diagnosis)
   
4. VERIFY — Check if current remediation is effective (requires waiting for
   N consecutive samples under threshold)

CONSTRAINTS:
- Never switch to static_shortest if utilization > 80% (will worsen congestion)
- Make-before-break: always maintain a baseline shortest-path route
- Hysteresis: require 5 consecutive samples under 80% threshold for PASS
- Never act if telemetry is stale (is_stale == True → report and wait)

TASK: Based on the observed metrics, decide what action to take.
- If congestion is detected and no detour is active → consider TOGGLE_DETOUR or SWITCH_MODE
- If congestion is detected and detour is active → consider VERIFY
- If no congestion → NONE
- If unclear → SPIKE_AND_PROBE for diagnosis

Return your decision in JSON format:
{
  "action": "NONE|SWITCH_MODE|TOGGLE_DETOUR|SPIKE_AND_PROBE|VERIFY",
  "reason": " concise explanation (max 100 chars)",
  "confidence": 0.0-1.0,
  "target_mode": "routing mode string (if SWITCH_MODE)",
  "detour_action": "install|remove|none (if TOGGLE_DETOUR)"
}
```

### 7. Safety Mechanisms (Critical)

The agent **must not** operate without these guards:

| Guard | Implementation | Source |
|-------|---------------|--------|
| **Utilization threshold** | Only act if utilization >= 70% (not 80%, gives headroom) | P9 cost function Rule 5 |
| **Stale check** | If `is_stale == True` → refuse action, report "telemetry stale" | P5 staleness detection |
| **Make-before-break** | Never remove detour without installing replacement first | P9 safety design |
| **Hysteresis** | Require 5 consecutive samples under 80% for PASS | P9 verify engine |
| **No switch under load** | Cannot switch FROM ml_aware TO static_shortest when util > 80% | P9 design rule |
| **Human confirmation required** | Agent requires explicit "Fix congestion" trigger from operator | Design choice |

### 8. OpenRouter Integration

The agent uses OpenRouter API with a **model cost-performance selection**:

```python
OPENROUTER_CONFIG = {
    "free_tier_models": [
        "anthropic/claude-3-haiku",       # Fast, cheap, good for structured decisions
        "mistralai/mistral-7b-instruct", # Good instruction following
        "meta-llama/llama-3-8b-instruct" # Balanced capability
    ],
    "selection_strategy": "first_available",  # or "cheapest_above_quality"
    "max_tokens": 500,
    "temperature": 0.0,  # Deterministic decisions
    "response_format": {"type": "json_object"}
}
```

The prompt is designed to get **deterministic, structured output** at temperature 0.0, ensuring the agent always returns valid JSON.

### 9. Workflow Examples

#### Example A: Congestion Detected, No Detour Active

```
Operator: "Fix congestion on s1--s2"
          ↓
Agent OBSERVES: util=85%, stale=False, mode=ml_aware, detour=False
          ↓
Agent REASONS (LLM): "Utilization above 80% threshold, no detour installed.
                       Switch to ml_aware (already in ml_aware) or toggle detour."
          ↓
Agent ACTS: TOGGLE_DETOUR → install
          ↓
Agent VERIFIES: Wait 10s for 5 consecutive samples under 80%
          ↓
Agent REPORTS: "Detour installed, utilization now 62%, VERIFIED PASS"
```

#### Example B: Congestion Detected, Detour Already Active

```
Operator: "Fix congestion on s1--s2"
          ↓
Agent OBSERVES: util=85%, stale=False, mode=ml_aware, detour=True
          ↓
Agent REASONS (LLM): "Congestion persists despite detour. Check verification state."
          ↓
Agent ACTS: VERIFY → check verify engine
          ↓
Agent REPORTS: "Detour active but still FAIL (still >80% for 12s). Switch mode to reactive_threshold."
          ↓
Agent ACTS: SWITCH_MODE → reactive_threshold
          ↓
Agent VERIFIES: Wait for convergence
          ↓
Agent REPORTS: "Mode switched to reactive_threshold, utilization trending down to 68%"
```

#### Example C: No Congestion

```
Operator: "Fix congestion on s1--s2"
          ↓
Agent OBSERVES: util=45%, stale=False, mode=ml_aware, detour=False
          ↓
Agent REASONS (LLM): "Utilization well below 80% threshold. No action needed."
          ↓
Agent ACTS: NONE
          ↓
Agent REPORTS: "No congestion detected. Current utilization: 45%."
```

### 10. API Extensions Needed

Add these endpoints to the existing FastAPI (P12):

```python
# New endpoint: Trigger agentic remediation
@app.post("/api/agent/remediate-congestion")
async def agent_remediate_congestion(
    request: AgentRemediateRequest,
    current_user: User = Depends(get_current_user)
):
    """Trigger the LangGraph agent to detect and fix congestion."""
    # 1. Validate request
    # 2. Initialize agent state with current telemetry
    # 3. Run graph until COMPLETE or MAX_STEPS
    # 4. Return final status report
    pass

# New endpoint: Agent status check
@app.get("/api/agent/status")
async def agent_status():
    """Return current agent state without triggering action."""
    pass
```

Where `AgentRemediateRequest` has:
```python
class AgentRemediateRequest(BaseModel):
    bottleneck_link: str  # e.g., "s1--s2"
    trigger_reason: str   # e.g., "operator_request", "auto_detected"
    allow_mode_switch: bool = True
    allow_detour: bool = True
```

### 11. Implementation Phases

| Phase | Description | Effort |
|-------|-------------|--------|
| **P0 — Foundation** | LangGraph agent skeleton, telemetry hooks, OpenRouter integration, prompt engineering | 2 weeks |
| **P1 — Safety Guards** | Implement all safety checks (stale, threshold, hysteresis, make-before-break) | 1 week |
| **P2 — API Integration** | Add FastAPI endpoints, wire into existing controller state | 1 week |
| **P3 — Testing & Refinement** | Unit tests for each action path, refine prompts, hysteresis tuning | 2 weeks |
| **P4 — Operator UI** | Add "Fix congestion" button to P12 dashboard | 1 week |
| **Total** | **8 weeks** | |

### 12. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| **LLM gives bad decision** | Temperature 0.0, strict JSON schema validation, fallback to "NONE" on parse failure |
| **Agent causes network outage** | Safety guards (stale check, threshold, hysteresis), human-in-the-loop design |
| **Model unavailable on OpenRouter** | Free tier fallback list, graceful degradation to "manual control only" |
| **Telemetry stale during agent run** | Immediate abort if `is_stale` becomes True during execution |
| **Hysteresis causes delayed response** | Configurable thresholds, emergency bypass with explicit operator confirmation |

### 13. Dependencies

- `langgraph` — state graph framework
- `langchain-openrouter` — OpenRouter API client
- Existing: `controller/`, `ml/`, `tests/`, `api/` modules
- Optional: `streamlit` or `fastapi` for operator UI

---

**This design adds an agentic layer **on top of** the existing verified SDN control plane, not a replacement. All agent actions flow through the existing Ryu controller and OpenFlow datapaths, preserving the safety properties established in P1-P9.**