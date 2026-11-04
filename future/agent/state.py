"""LangGraph State Definition for SDN Congestion Control Agent."""
from typing import TypedDict, List, Dict, Any, Optional
import time


class StepLogEntry(TypedDict):
    node: str
    title: str
    description: str
    timestamp: str
    status: str  # 'running' | 'success' | 'warning' | 'error'
    meta: Optional[Dict[str, Any]]


class AgentState(TypedDict):
    """The central state dictionary passed between nodes in the LangGraph workflow."""
    messages: List[Dict[str, Any]]
    task: str
    topology_id: str
    network_state: Dict[str, Any]
    predictions: Dict[str, Any]
    congestion_detected: bool
    bottleneck_link: str
    analysis: str
    plan: str
    action_taken: str
    verification: Dict[str, Any]
    iteration: int
    status: str  # 'idle' | 'monitoring' | 'analyzing' | 'planning' | 'acting' | 'verifying' | 'completed' | 'failed'
    steps_log: List[StepLogEntry]
    error: Optional[str]


def create_initial_state(task: str = "Analyze network and resolve congestion", topology_id: str = "diamond") -> AgentState:
    """Factory for generating a clean, initialized agent state."""
    return {
        "messages": [{"role": "user", "content": task, "time": time.strftime("%H:%M:%S")}],
        "task": task,
        "topology_id": topology_id,
        "network_state": {},
        "predictions": {},
        "congestion_detected": False,
        "bottleneck_link": "",
        "analysis": "",
        "plan": "",
        "action_taken": "",
        "verification": {},
        "iteration": 0,
        "status": "idle",
        "steps_log": [],
        "error": None,
    }
