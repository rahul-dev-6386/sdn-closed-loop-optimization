"""Configuration for OpenRouter-powered LangGraph Congestion Control Agent."""
import os
from typing import Dict, Any, List
from pathlib import Path
import yaml

DEFAULT_FREE_MODELS = [
    {
        "id": "nvidia/nemotron-3.5-lightning:free",
        "name": "NVIDIA Nemotron 3.5 Lightning (Free - High Quality)",
        "context": 65536,
    },
    {
        "id": "google/gemma-4-31b-it:free",
        "name": "Google Gemma 4 31B (Free)",
        "context": 32768,
    },
    {
        "id": "google/gemma-4-26b-a4b-it:free",
        "name": "Google Gemma 4 26B A4B (Free)",
        "context": 32768,
    },
    {
        "id": "liquid/lfm-2.5-2.6b:free",
        "name": "LiquidAI LFM 2.5 2.6B (Free)",
        "context": 32768,
    },
    {
        "id": "thinkingmachines/inkling:free",
        "name": "Thinking Machines Inkling (Free)",
        "context": 32768,
    },
]

CONFIG_FILE_PATH = Path(__file__).resolve().parent.parent.parent / "config.yaml"
ENV_FILE_PATH = Path(__file__).resolve().parent.parent.parent / ".env"


class AgentConfig:
    def __init__(self):
        self.api_key: str = os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url: str = "https://openrouter.ai/api/v1"
        self.model: str = "nvidia/nemotron-3.5-lightning:free"
        self.temperature: float = 0.2
        self.congestion_threshold_pct: float = 80.0
        self.load_from_config()


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
        """Return safe config dict without exposing full API key."""
        masked_key = ""
        if self.api_key:
            masked_key = self.api_key[:6] + "..." + self.api_key[-4:] if len(self.api_key) > 10 else "***"
        return {
            "has_api_key": bool(self.api_key),
            "masked_key": masked_key,
            "model": self.model,
            "available_models": DEFAULT_FREE_MODELS,
            "congestion_threshold_pct": self.congestion_threshold_pct,
        }


# Singleton config instance
agent_config = AgentConfig()
