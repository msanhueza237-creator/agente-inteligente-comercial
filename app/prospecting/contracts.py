from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SourceName(str, Enum):
    google_places = "google_places"
    brave_search = "brave_search"
    official_website = "official_website"


class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    paused = "paused"
    partial = "partial"
    completed = "completed"
    failed = "failed"
    cancel_requested = "cancel_requested"
    cancelled = "cancelled"


class EventLevel(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"


class DedupDisposition(str, Enum):
    unique = "unique"
    exact_match = "exact_match"
    possible_duplicate = "possible_duplicate"


class Territory(BaseModel):
    model_config = ConfigDict(frozen=True)

    country_code: Literal["CL"] = "CL"
    region_code: str = Field(pattern=r"^\d{2}$")
    region_name: str = Field(min_length=1, max_length=120)
    comuna_code: str = Field(pattern=r"^\d{5}$")
    comuna_name: str = Field(min_length=1, max_length=120)

    @field_validator("region_code", "comuna_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def comuna_belongs_to_region(self) -> Territory:
        if not self.comuna_code.startswith(self.region_code):
            raise ValueError("comuna CUT code does not belong to region CUT code")
        return self


class ProspectingCampaign(BaseModel):
    """Reusable CRM-owned definition. Runs always receive an immutable snapshot."""

    model_config = ConfigDict(frozen=True)

    crm_campaign_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    territories: tuple[Territory, ...] = Field(min_length=1, max_length=346)
    keywords: tuple[str, ...] = Field(min_length=1, max_length=50)
    sources: tuple[SourceName, ...] = (
        SourceName.google_places,
        SourceName.brave_search,
        SourceName.official_website,
    )
    sector: Literal["hvac"] = "hvac"
    target_types: tuple[
        Literal[
            "distribuidor",
            "tienda comercial",
            "tecnico",
            "instalador grande",
            "competencia",
            "otro",
        ],
        ...,
    ] = Field(
        default=(
            "distribuidor",
            "tienda comercial",
            "tecnico",
            "instalador grande",
            "competencia",
            "otro",
        ),
        min_length=1,
    )
    max_results_per_task: int = Field(default=20, ge=1, le=20)
    max_candidates: int = Field(default=1000, ge=1, le=1000)

    @field_validator("keywords")
    @classmethod
    def clean_keywords(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(v.strip() for v in values if v.strip()))
        if not cleaned:
            raise ValueError("at least one non-empty keyword is required")
        if any(len(value) > 200 for value in cleaned):
            raise ValueError("keywords cannot exceed 200 characters")
        return cleaned

    @field_validator("sources")
    @classmethod
    def unique_sources(cls, values: tuple[SourceName, ...]) -> tuple[SourceName, ...]:
        return tuple(dict.fromkeys(values))

    @model_validator(mode="after")
    def has_discovery_source(self) -> ProspectingCampaign:
        if not {SourceName.google_places, SourceName.brave_search} & set(self.sources):
            raise ValueError("google_places or brave_search is required for discovery")
        if (
            SourceName.brave_search in self.sources
            and SourceName.official_website not in self.sources
        ):
            raise ValueError(
                "brave_search requires official_website for territorial verification"
            )
        return self


class ProspectingRunSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    crm_run_id: str = Field(min_length=1, max_length=200)
    campaign_version: int = Field(ge=1)
    campaign: ProspectingCampaign
    requested_at: datetime = Field(default_factory=utc_now)
    requested_by: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def campaign_id_present(self) -> ProspectingRunSnapshot:
        if not self.campaign.crm_campaign_id.strip():
            raise ValueError("campaign id is required")
        return self


class ProspectLocation(BaseModel):
    country_code: Literal["CL"] = "CL"
    region_code: str | None = Field(default=None, pattern=r"^\d{2}$")
    region_name: str | None = Field(default=None, max_length=120)
    comuna_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    comuna_name: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def valid_cut_relationship(self) -> ProspectLocation:
        if self.region_code and self.comuna_code and not self.comuna_code.startswith(
            self.region_code
        ):
            raise ValueError("location comuna CUT does not belong to region CUT")
        return self


class DerivedProvenance(BaseModel):
    ruleset: str = Field(min_length=1, max_length=100)
    derived_at: datetime = Field(default_factory=utc_now)
    input_fields: tuple[str, ...] = ()


class SourceEvidence(BaseModel):
    provider: SourceName
    source_url: str | None = Field(default=None, max_length=2048)
    provider_record_id: str | None = Field(default=None, max_length=2048)
    field: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=4000)
    observed_at: datetime = Field(default_factory=utc_now)
    retention_until: datetime | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)

    @model_validator(mode="after")
    def has_traceable_source(self) -> SourceEvidence:
        if not self.source_url and not self.provider_record_id:
            raise ValueError("source_url or provider_record_id is required")
        if self.provider == SourceName.google_places and self.retention_until is None:
            object.__setattr__(self, "retention_until", self.observed_at + timedelta(days=30))
        return self


class ProspectCandidate(BaseModel):
    candidate_id: str | None = Field(default=None, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    trade_name: str | None = Field(default=None, max_length=300)
    rut: str | None = Field(default=None, max_length=32)
    provider_ids: dict[str, str] = Field(default_factory=dict, max_length=10)
    phone: str | None = Field(default=None, max_length=50)
    email: str | None = Field(default=None, max_length=320)
    website: str | None = Field(default=None, max_length=2048)
    location: ProspectLocation
    locations: list[ProspectLocation] = Field(default_factory=list, max_length=50)
    category: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    company_summary: str | None = Field(default=None, max_length=1200)
    social_media: dict[str, str] = Field(default_factory=dict, max_length=10)
    specialties: tuple[str, ...] = Field(default=(), max_length=50)
    brands: tuple[str, ...] = Field(default=(), max_length=50)
    evidence: list[SourceEvidence] = Field(default_factory=list, max_length=100)
    score: float | None = Field(default=None, ge=0, le=100)
    market_score: float | None = Field(default=None, ge=0, le=100)
    market_signals: dict[str, int | float | str | bool] = Field(default_factory=dict, max_length=30)
    derived_provenance: dict[str, DerivedProvenance] = Field(default_factory=dict)
    import_eligible: bool = False
    importable_location_indexes: tuple[int, ...] = ()
    review_flags: tuple[str, ...] = Field(default=(), max_length=50)
    dedup_disposition: DedupDisposition = DedupDisposition.unique
    possible_duplicate_of: str | None = Field(default=None, max_length=200)

    @field_validator("provider_ids")
    @classmethod
    def provider_id_limits(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not key or len(key) > 40 for key in values):
            raise ValueError("provider id keys must contain 1..40 characters")
        if any(not value or len(value) > 2048 for value in values.values()):
            raise ValueError("provider id values must contain 1..2048 characters")
        return values

    @field_validator("review_flags")
    @classmethod
    def review_flag_limits(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value or len(value) > 120 for value in values):
            raise ValueError("review flags must contain 1..120 characters")
        return values

    @model_validator(mode="after")
    def include_canonical_location(self) -> ProspectCandidate:
        def key(location: ProspectLocation) -> tuple[str, ...]:
            return tuple(
                " ".join((value or "").casefold().split())
                for value in (
                    location.country_code,
                    location.region_code or location.region_name,
                    location.comuna_code or location.comuna_name,
                    location.address,
                )
            )

        unique: dict[tuple[str, ...], ProspectLocation] = {}
        for location in (self.location, *self.locations):
            unique.setdefault(key(location), location)
        if len(unique) > 50:
            raise ValueError("a candidate cannot contain more than 50 locations")
        self.locations = list(unique.values())
        return self


class RunEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    task_id: str | None = Field(default=None, max_length=200)
    source: SourceName | None = None
    keyword: str | None = Field(default=None, max_length=200)
    comuna_code: str | None = Field(default=None, max_length=5)
    comuna_name: str | None = Field(default=None, max_length=120)
    task_status: Literal["pending", "running", "completed", "failed", "cancelled"] | None = None
    level: EventLevel = EventLevel.info
    stage: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, max_length=2000)
    metrics: dict[str, int | float | str | bool | None] = Field(
        default_factory=dict, max_length=50
    )
    occurred_at: datetime = Field(default_factory=utc_now)

    @field_validator("metrics")
    @classmethod
    def metric_limits(
        cls, values: dict[str, int | float | str | bool | None]
    ) -> dict[str, int | float | str | bool | None]:
        if any(not key or len(key) > 80 for key in values):
            raise ValueError("metric keys must contain 1..80 characters")
        if any(isinstance(value, str) and len(value) > 1000 for value in values.values()):
            raise ValueError("metric string values cannot exceed 1000 characters")
        return values


class ClaimedTask(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    task_id: str = Field(alias="id", max_length=200)
    run_id: str | None = Field(default=None, max_length=200)
    source: SourceName
    keyword: str = Field(max_length=200)
    region_code: str = Field(pattern=r"^\d{2}$")
    region_name: str | None = Field(default=None, max_length=120)
    comuna_code: str = Field(pattern=r"^\d{5}$")
    comuna_name: str | None = Field(default=None, max_length=120)
    status: Literal["pending", "running", "completed", "failed", "cancelled"] = "pending"
    attempts: int = Field(
        default=0, ge=0, validation_alias=AliasChoices("attempts", "attempt_count")
    )
    candidates_found: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("candidates_found", "results_found"),
    )
    results_discarded: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("results_discarded", "rejected_count"),
    )
    max_results: int = Field(default=20, ge=1, le=20)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def task_cut_relationship(self) -> ClaimedTask:
        if not self.comuna_code.startswith(self.region_code):
            raise ValueError("task comuna CUT does not belong to region CUT")
        if self.attempts > self.max_attempts:
            raise ValueError("task attempts exceed max_attempts")
        return self


class ClaimedRun(BaseModel):
    snapshot: ProspectingRunSnapshot
    lease_token: str
    lease_expires_at: datetime
    candidates_found: int = Field(default=0, ge=0)
    # None means an old/development producer omitted task planning and the
    # agent may expand locally. An explicit empty list means the CRM has no
    # remaining task UUIDs and must never trigger local expansion.
    tasks: tuple[ClaimedTask, ...] | None = None


class ClaimedEnrichment(BaseModel):
    job_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    candidate_relation_id: str = Field(min_length=1, max_length=200)
    candidate: ProspectCandidate
    lease_token: str = Field(min_length=1, max_length=200)
    lease_expires_at: datetime


class CompletionReport(BaseModel):
    status: Literal[RunStatus.completed, RunStatus.partial, RunStatus.cancelled]
    stats: dict[str, Any] = Field(default_factory=dict)


class CandidateBatchAck(BaseModel):
    """Authoritative CRM result for an idempotent candidate batch."""

    accepted: int = Field(
        default=0, ge=0, validation_alias=AliasChoices("accepted", "accepted_count")
    )
    rejected_limit: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("rejected_limit", "rejected_due_to_limit"),
    )
    candidates_found: int | None = Field(default=None, ge=0)
    accepted_candidate_ids: tuple[str, ...] = Field(
        default=(),
        validation_alias=AliasChoices("accepted_candidate_ids", "candidate_ids"),
    )
