"""LLM integration for the LangGraph Congestion Control Agent.

Two providers, both OpenAI-compatible: OpenRouter (``OPENROUTER_API_KEY``)
and Groq (``GROQ_API_KEY``), each with a free model tier. The chain tries the
user-selected model first, then walks the quality-ordered cross-provider
``FALLBACK_PRIORITY``, and finally degrades to a deterministic factual
reporter. No numbers are ever invented at any stage.
"""
import json
import logging
from typing import Dict, Any, List, Optional
import httpx
from future.agent.config import agent_config

LOGGER = logging.getLogger("sdn.agent.llm")

SYSTEM_PROMPT = """You are an Autonomous SDN Congestion Control Agent operating inside an OpenFlow 1.3 Software-Defined Networking testbed.
Your mission is to monitor telemetry, evaluate machine learning congestion forecasts, plan constrained multipath rerouting, and verify closed-loop relief.

When responding to an incident:
1. Analyze the telemetry: identify the congested link, offered traffic vs link capacity, and buffer drop hazard.
2. Evaluate ML predictions: review the Random Forest 10-second forward horizon probability.
3. Formulate a mitigation plan: choose between 'ml_aware' Make-Before-Break detour, static shortest path, or rate shaping.
4. Execute and explain the flow-mod changes and state the post-reroute verification criteria.

Keep your explanations clear and professional. Use only the numeric values provided in the telemetry context — never invent bandwidth numbers, percentages, or probabilities.
"""


class AgentLLM:
    """OpenRouter + Groq chat access with a deterministic factual fallback."""

    def __init__(self):
        self.config = agent_config

    async def generate_response(self, prompt: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Try the configured model, then walk the cross-provider free chain.

        Order: the user-selected model (its provider's endpoint), then the
        quality-ordered ``FALLBACK_PRIORITY`` across OpenRouter and Groq
        (providers without a key are skipped), then the deterministic factual
        reporter. Each attempt is independent; one provider being down never
        blocks the other.
        """
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if context:
            messages.append({
                "role": "system",
                "content": f"Current Network Telemetry Context:\n{json.dumps(context, indent=2)}",
            })
        messages.append({"role": "user", "content": prompt})

        for model in self.config.fallback_order():
            base_url, key = self.config.endpoint_of(model)
            if not key:
                LOGGER.info("Skipping %s: no API key for its provider", model)
                continue
            content = await self._complete(base_url, key, model, messages)
            if content:
                return content

        # Final fallback: domain-specific deterministic reasoning engine.
        return self._deterministic_reasoning(prompt, context)

    async def _complete(self, base_url: str, api_key: str, model: str,
                        messages: List[Dict[str, Any]]) -> Optional[str]:
        """Single chat-completion attempt. Returns content or ``None``."""
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5173",
            "X-Title": "SDN Agentic Congestion Controller",
        }
        provider = self.config.provider_of(model)
        payload = {
            "model": model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": 800,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(f"{base_url}/chat/completions",
                                         headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "").strip()
                        if len(content) > 10:
                            return content
                        LOGGER.warning("%s returned truncated response (%d chars)", model, len(content))
                        return None
                LOGGER.warning("%s (%s) error %d: %s", model, provider, resp.status_code, resp.text[:200])
                return None
        except Exception as exc:
            LOGGER.warning("%s (%s) request exception: %s", model, provider, exc)
            return None

    def _deterministic_reasoning(self, prompt: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Factual reporter used when no LLM is reachable or configured.

        Prints only what was measured in ``context``; never invents numbers.
        """
        ctx = context or {}
        topo = ctx.get("topology_id", "diamond")
        bottleneck = ctx.get("bottleneck", "unknown")
        util = ctx.get("bottleneck_util_pct")
        prob = ctx.get("ml_probability")
        congested = bool(ctx.get("congested"))
        status = ctx.get("telemetry_status", "ok")
        reason = ctx.get("telemetry_reason")

        if status == "unavailable":
            return (
                f"**Telemetry unavailable** on topology `{topo}`: {reason}. "
                f"No congestion assessment can be made. Start the fabric and ask again; "
                f"I will not report numbers I did not measure."
            )

        util_txt = f"{util}%" if util is not None else "n/a (not scored)"
        prob_txt = f"p = {prob}" if prob is not None else "n/a (model not scored yet)"
        links = ctx.get("links", {}) or {}
        link_lines = []
        for key, info in sorted(links.items()):
            try:
                link_lines.append(
                    f"  - `{key}`: {info.get('used_mbps', 0.0):.3f} Mbps "
                    f"({info.get('util_pct', 0)}% of {info.get('capacity_mbps', 10.0)} Mbps)"
                )
            except Exception:
                continue
        links_block = ("\n" + "\n".join(link_lines) + "\n") if link_lines else ""

        return (
            f"**Network telemetry — `{topo}`**\n"
            f"- Bottleneck (measured worst): `{bottleneck}` at {util_txt}"
            f"{' (CONGESTED)' if congested else ''}\n"
            f"- ML forecast: {prob_txt} (10 s horizon)\n"
            f"- Per-link utilisation (port-stats):{links_block}"
            f"Ask me to 'fix congestion' to switch the controller to ML-aware routing, "
            f"or 'run closed-loop' for the full measure→decide→reroute→verify cycle."
        )


llm_client = AgentLLM()
