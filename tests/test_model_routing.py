"""Tests for OpenRouter Multi-Tier Model Routing Matrix."""

from __future__ import annotations

from pathlib import Path

from jarvise.model_routing import (
    DEFAULT_AGENT_MATRIX,
    load_model_routing_matrix,
    resolve_model_chain,
)


def test_default_agent_matrix_has_5_roles() -> None:
    expected_roles = {
        "DOCTRINE_JUDGE",
        "HEARTBEAT_INGEST",
        "QUANT_ANALYZER",
        "CONTEXT_DIGEST",
        "PAPER_SANDBOX",
    }
    assert set(DEFAULT_AGENT_MATRIX.keys()) == expected_roles

    # Check primary models per spec
    assert DEFAULT_AGENT_MATRIX["DOCTRINE_JUDGE"].primary_model == "anthropic/claude-sonnet-5"
    assert DEFAULT_AGENT_MATRIX["HEARTBEAT_INGEST"].primary_model == "deepseek/deepseek-v4.1-flash"
    assert DEFAULT_AGENT_MATRIX["QUANT_ANALYZER"].primary_model == "deepseek/deepseek-v4-pro"
    assert DEFAULT_AGENT_MATRIX["CONTEXT_DIGEST"].primary_model == "google/gemini-3.8-flash"
    assert DEFAULT_AGENT_MATRIX["PAPER_SANDBOX"].primary_model == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_resolve_model_chain() -> None:
    chain = resolve_model_chain("DOCTRINE_JUDGE")
    assert chain == [
        "anthropic/claude-sonnet-5",
        "anthropic/claude-sonnet-4.6",
        "anthropic/claude-opus-5.5",
    ]

    fallback_chain = resolve_model_chain("UNKNOWN_ROLE")
    assert fallback_chain == ["openrouter/openrouter/auto"]


def test_load_model_routing_matrix_from_file() -> None:
    cfg_path = Path("config/openclaw/openclaw.json")
    matrix = load_model_routing_matrix(cfg_path)
    assert matrix["totalAgents"] == 5
    assert "DOCTRINE_JUDGE" in matrix["agents"]
    assert matrix["agents"]["DOCTRINE_JUDGE"]["primaryModel"] == "anthropic/claude-sonnet-5"
    assert matrix["agents"]["DOCTRINE_JUDGE"]["cacheControl"] == "ephemeral"
    assert matrix["agents"]["DOCTRINE_JUDGE"]["compactorEnabled"] is True
