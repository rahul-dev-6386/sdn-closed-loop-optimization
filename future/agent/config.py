"""Configuration for the LangGraph Congestion Control Agent.

Two providers are supported out of the box, each with a free tier:

* **OpenRouter** — `free`-suffixed models served through OpenRouter's
  OpenAI-compatible endpoint. Key: ``OPENROUTER_API_KEY``.
* **Groq** — models served through Groq's OpenAI-compatible endpoint.
  Key: ``GROQ_API_KEY``. Free tier is currently ~1,000 requests/day per
  model at 30 RPM (Nov 2026 catalogue: Qwen3.8 27B and GPT-OSS 120B are the
  highest-quality free options, per public benchmark comparisons).

The agent attempts the user-selected model first, then walks
``FALLBACK_PRIORITY`` across both providers, and finally degrades to the
deterministic factual reporter. No numbers are ever invented at any stage.
"""
import os
from typing import Dict, Any, List
from pathlib import Path
import yaml

DEFAULT_FREE_MODELS = [
    {
        "id": "nvidia/nemotron-3.5-lightning:free",
        "name": "NVIDIA Nemotron 3.5 Lightning (Free - High Quality)",
        "context": 65536,
        "provider": "openrouter",
    },
    {
        "id": "google/gemma-4-31b-it:free",
        "name": "Google Gemma 4 31B (Free)",
        "context": 32768,
        "provider": "openrouter",
    },
    {
        "id": "google/gemma-4-26b-a4b-it:free",
        "name": "Google Gemma 4 26B A4B (Free)",
        "context": 32768,
        "provider": "openrouter",
    },
    {
        "id": "liquid/lfm-2.5-2.6b:free",
        "name": "LiquidAI LFM 2.5 2.6B (Free)",
        "context": 32768,
        "provider": "openrouter",
    },
    {
        "id": "thinkingmachines/inkling:free",
        "name": "Thinking Machines Inkling (Free)",
        "context": 32768,
        "provider": "openrouter",
    },
]

GROQ_FREE_MODELS = [
    {
        "id": "qwen/qwen3.8-27b",
        "name": "Qwen3.8 27B (Groq, Free - best benchmark score)",
        "context": 131072,
        "provider": "groq",
    },
    {
        "id": "openai/gpt-oss-120b",
        "name": "OpenAI GPT-OSS 120B (Groq, Free - flagship)",
        "context": 131072,
        "provider": "groq",
    },
    {
        "id": "openai/gpt-oss-20b",
        "name": "OpenAI GPT-OSS 20B (Groq, Free - fast)",
        "context": 131072,
        "provider": "groq",
    },
]

ALL_FREE_MODELS = DEFAULT_FREE_MODELS + GROQ_FREE_MODELS

#: Curated quality-ordered fallback across both providers. The user-selected
#: model is always attempted first; the rest follow in this order.
FALLBACK_PRIORITY = [
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "nvidia/nemotron-3.5-lightning:free",
    "openai/gpt-oss-20b",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "thinkingmachines/inkling:free",
    "liquid/lfm-2.5-2.6b:free",
]

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

CONFIG_FILE_PATH = Path(__file__).resolve().parent.parent.parent / "config.yaml"
ENV_FILE_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


class AgentConfig:
    def __init__(self):
        self.api_key: str = os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url: str = "https://openrouter.ai/api/v1"
        self.groq_api_key: str = os.environ.get("GROQ_API_KEY", "")
        self.groq_base_url: str = GROQ_BASE_URL
        self.model: str = "nvidia/nemotron-3.5-lightning:free"
        self.temperature: float = 0.2
        self.congestion_threshold_pct: float = 80.0
        self.load_from_config()

    def provider_of(self, model_id: str) -> str:
        """Return the provider serving ``model_id`` ('openrouter' or 'groq')."""
        for entry in GROQ_FREE_MODELS:
            if entry["id"] == model_id:
                return "groq"
        return "openrouter"

    def endpoint_of(self, model_id: str) -> Any:
        """(base_url, api_key) for the provider serving ``model_id``."""
        if self.provider_of(model_id) == "groq":
            return self.groq_base_url, self.groq_api_key
        return self.base_url, self.api_key

    def fallback_order(self) -> List[str]:
        """Selected model first, then the quality-ordered cross-provider chain."""
        order = [self.model]
        for mid in FALLBACK_PRIORITY:
            if mid not in order:
                order.append(mid)
        return order

    def load_from_config(self) -> None:
        """Load api key and model preference from .env and config.yaml if present."""
        # 1. Check .env file
        if ENV_FILE_PATH.is_file():
            try:
                for line in ENV_FILE_PATH.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("OPENROUTER_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            self.api_key = val
                            os.environ["OPENROUTER_API_KEY"] = val
                    elif line.startswith("GROQ_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            self.groq_api_key = val
                            os.environ["GROQ_API_KEY"] = val
            except Exception:
                pass

        # 2. Check config.yaml
        if CONFIG_FILE_PATH.is_file():
            try:
                with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                    agent_sec = data.get("agent", {})
                    if agent_sec.get("openrouter_api_key"):
                        self.api_key = agent_sec.get("openrouter_api_key")
                    if agent_sec.get("groq_api_key"):
                        self.groq_api_key = agent_sec.get("groq_api_key")
                    if agent_sec.get("model"):
                        self.model = agent_sec.get("model")
            except Exception:
                pass

    def save_to_config(self, api_key: str, model: str) -> None:
        """Save api key and model preference to config.yaml."""
        self.api_key = api_key.strip()
        if model:
            self.model = model.strip()
        try:
            data = {}
            if CONFIG_FILE_PATH.is_file():
                with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            if "agent" not in data:
                data["agent"] = {}
            data["agent"]["openrouter_api_key"] = self.api_key
            data["agent"]["model"] = self.model
            with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, default_flow_style=False)
        except Exception:
            pass

    def to_dict(self) -> Dict[str, Any]:
        """Return safe config dict without exposing full API keys."""
        masked_key = ""
        if self.api_key:
            masked_key = self.api_key[:6] + "..." + self.api_key[-4:] if len(self.api_key) > 10 else "***"
        groq_masked_key = ""
        if self.groq_api_key:
            groq_masked_key = "gsk-..." + self.groq_api_key[-4:] if len(self.groq_api_key) > 8 else "***"
        return {
            "has_api_key": bool(self.api_key),
            "masked_key": masked_key,
            "has_groq_api_key": bool(self.groq_api_key),
            "groq_masked_key": groq_masked_key,
            "model": self.model,
            "available_models": ALL_FREE_MODELS,
            "congestion_threshold_pct": self.congestion_threshold_pct,
        }


# Singleton config instance
agent_config = AgentConfig()
