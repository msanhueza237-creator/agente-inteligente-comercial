from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentType(StrEnum):
    commercial = "commercial"
    marketing = "marketing"
    finance = "finance"
    collections = "collections"
    foreign_trade = "foreign_trade"
    executive = "executive"


class TaskStatus(StrEnum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ProposalKind(StrEnum):
    campaign_draft = "campaign_draft"
    collection_reminder = "collection_reminder"
    purchase_order = "purchase_order"
    commercial_follow_up = "commercial_follow_up"
    executive_alert = "executive_alert"


class HubTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    agent_type: AgentType
    action: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    requested_by: str | None = None
    created_at: datetime | None = None


class ClaimedHubTask(HubTask):
    lease_token: str
    lease_expires_at: datetime


class ActionProposal(BaseModel):
    kind: ProposalKind
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4000)
    payload: dict[str, Any] = Field(default_factory=dict)
    risk_level: str = "medium"
    requires_approval: bool = True


class AgentResult(BaseModel):
    summary: str
    metrics: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    proposals: list[ActionProposal] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
