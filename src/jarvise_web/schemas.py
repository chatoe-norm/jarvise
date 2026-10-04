"""Pydantic response models for the control API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ApprovalRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    status: str | None = None
    symbol: str | None = None
    action: str | None = None
    invalidation_price: float | None = None


class ApprovalsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool = True
    rows: list[dict[str, Any]] = Field(default_factory=list)


class DashboardResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool = True
    status: dict[str, Any]
    pending_count: int = 0
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    paper: dict[str, Any]
    metrics: dict[str, Any]
