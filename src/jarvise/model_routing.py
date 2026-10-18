"""OpenRouter Multi-Tier Model Routing Matrix for Jarvise and OpenClaw agents."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/openclaw/openclaw.json")


@dataclass(frozen=True)
class AgentModelConfig:
    role: str
    key: str
    name: str
    emoji: str
    theme: str
    primary_model: str
    fallback_models: list[str] = field(default_factory=list)
    temperature: float = 0.0
    core_task: str = ""
    cache_control: str = "ephemeral"
    compaction_threshold_kb: int = 300


DEFAULT_AGENT_MATRIX: dict[str, AgentModelConfig] = {
    "DOCTRINE_JUDGE": AgentModelConfig(
        role="DOCTRINE_JUDGE",
        key="doctrine_judge",
        name="Doctrine Judge",
        emoji="⚖️",
        theme="doctrine",
        primary_model="anthropic/claude-sonnet-5",
        fallback_models=["anthropic/claude-sonnet-4.6", "anthropic/claude-opus-5.5"],
        temperature=0.0,
        core_task="ยังไม่เชื่อมต่อ ตารางนี้ไม่ใช่ทีม OpenClaw",
    ),
    "HEARTBEAT_INGEST": AgentModelConfig(
        role="HEARTBEAT_INGEST",
        key="heartbeat_ingest",
        name="Heartbeat Ingest",
        emoji="💓",
        theme="ingest",
        primary_model="deepseek/deepseek-v4.1-flash",
        fallback_models=["minimax/minimax-m3", "google/gemini-3.8-flash"],
        temperature=0.1,
        core_task="ยังไม่เชื่อมต่อ ตารางนี้ไม่ใช่ทีม OpenClaw",
    ),
    "QUANT_ANALYZER": AgentModelConfig(
        role="QUANT_ANALYZER",
        key="quant_analyzer",
        name="Quant Analyzer",
        emoji="📐",
        theme="quant",
        primary_model="deepseek/deepseek-v4-pro",
        fallback_models=["openai/gpt-5.6-luna", "anthropic/claude-sonnet-5"],
        temperature=0.0,
        core_task="ยังไม่เชื่อมต่อ ตารางนี้ไม่ใช่ทีม OpenClaw",
    ),
    "CONTEXT_DIGEST": AgentModelConfig(
        role="CONTEXT_DIGEST",
        key="context_digest",
        name="Context Digest",
        emoji="📰",
        theme="context",
        primary_model="google/gemini-3.8-flash",
        fallback_models=["z-ai/glm-5.3-flash", "deepseek/deepseek-v4.1-flash"],
        temperature=0.3,
        core_task="ยังไม่เชื่อมต่อ ตารางนี้ไม่ใช่ทีม OpenClaw",
    ),
    "PAPER_SANDBOX": AgentModelConfig(
        role="PAPER_SANDBOX",
        key="paper_sandbox",
        name="Paper Sandbox",
        emoji="🧪",
        theme="sandbox",
        primary_model="nvidia/nemotron-3-ultra-550b-a55b:free",
        fallback_models=["nvidia/nemotron-3-super:free", "google/gemini-3.8-flash"],
        temperature=0.1,
        core_task="ยังไม่เชื่อมต่อ ตารางนี้ไม่ใช่ทีม OpenClaw",
    ),
}


def load_model_routing_matrix(config_path: Path | None = None) -> dict[str, Any]:
    """Loads agent model routing from openclaw.json or falls back to defaults."""
    path = config_path or DEFAULT_CONFIG_PATH
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data.get("agents", {}).get("entries", {})
            if entries:
                res: dict[str, Any] = {}
                for role, default_cfg in DEFAULT_AGENT_MATRIX.items():
                    entry = entries.get(default_cfg.key, {})
                    model_obj = entry.get("model", {})
                    primary = model_obj.get("primary", default_cfg.primary_model)
                    fallbacks = model_obj.get("fallbacks", default_cfg.fallback_models)
                    ident = entry.get("identity", {})
                    name = ident.get("name", default_cfg.name)
                    emoji = ident.get("emoji", default_cfg.emoji)
                    theme = ident.get("theme", default_cfg.theme)
                    temp = float(entry.get("temperature", default_cfg.temperature))
                    desc = entry.get("description", default_cfg.core_task)
                    res[role] = {
                        "role": role,
                        "key": default_cfg.key,
                        "name": name,
                        "emoji": emoji,
                        "theme": theme,
                        "primaryModel": primary,
                        "fallbackModels": fallbacks,
                        "temperature": temp,
                        "coreTask": desc,
                        "cacheControl": "ephemeral",
                        "compactorEnabled": True,
                        "compactionThresholdKb": 300,
                    }
                return {
                    "version": "2026-10-16",
                    "agents": res,
                    "totalAgents": len(res),
                    "source": str(path),
                }
        except Exception as exc:
            logger.warning("failed to parse %s: %s", path, exc)

    fallback_dict = {
        k: {
            "role": v.role,
            "key": v.key,
            "name": v.name,
            "emoji": v.emoji,
            "theme": v.theme,
            "primaryModel": v.primary_model,
            "fallbackModels": v.fallback_models,
            "temperature": v.temperature,
            "coreTask": v.core_task,
            "cacheControl": v.cache_control,
            "compactorEnabled": True,
            "compactionThresholdKb": v.compaction_threshold_kb,
        }
        for k, v in DEFAULT_AGENT_MATRIX.items()
    }
    return {
        "version": "2026-10-16",
        "agents": fallback_dict,
        "totalAgents": len(fallback_dict),
        "source": "defaults",
    }


def resolve_model_chain(role: str) -> list[str]:
    """Returns [primary_model, *fallback_models] for an agent role."""
    cfg = DEFAULT_AGENT_MATRIX.get(role)
    if not cfg:
        return ["openrouter/openrouter/auto"]
    return [cfg.primary_model, *cfg.fallback_models]
