import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --- Enums -------------------------------------------------------------


class ProspectCategory(str, enum.Enum):
    distributor = "distributor"
    retailer = "retailer"
    installer_large = "installer_large"
    installer_independent = "installer_independent"
    maintenance = "maintenance"
    refrigeration = "refrigeration"
    competitor = "competitor"
    other = "other"


class CommercialPotentialLevel(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    very_high = "very_high"


class ProspectStatus(str, enum.Enum):
    new = "new"
    enriched = "enriched"
    reviewed = "reviewed"
    approved = "approved"
    rejected = "rejected"
    synced = "synced"


class DedupStatus(str, enum.Enum):
    unique = "unique"
    needs_review = "needs_review"
    merged = "merged"


class CRMSyncStatus(str, enum.Enum):
    not_synced = "not_synced"
    pending = "pending"
    synced = "synced"
    error = "error"


class SourceType(str, enum.Enum):
    google_places = "google_places"
    brave_search = "brave_search"
    official_website = "official_website"
    paginas_amarillas = "paginas_amarillas"
    website_scrape = "website_scrape"
    social_media = "social_media"
    excel_import = "excel_import"
    manual_edit = "manual_edit"
    llm_enrichment = "llm_enrichment"
    crm = "crm"


class JobType(str, enum.Enum):
    region_category_search = "region_category_search"
    excel_import = "excel_import"
    manual_search = "manual_search"
    enrichment_refresh = "enrichment_refresh"
    dedup_scan = "dedup_scan"
    crm_sync_batch = "crm_sync_batch"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    partial = "partial"


class DedupCandidateStatus(str, enum.Enum):
    pending = "pending"
    merged = "merged"
    rejected_not_duplicate = "rejected_not_duplicate"


class ImportBatchStatus(str, enum.Enum):
    uploaded = "uploaded"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class CRMSyncDirection(str, enum.Enum):
    search = "search"
    upsert = "upsert"


class PlacesQueryType(str, enum.Enum):
    text_search = "text_search"
    nearby_search = "nearby_search"
    place_details = "place_details"


class PlacesFieldTier(str, enum.Enum):
    pro = "pro"
    enterprise = "enterprise"


class UserRole(str, enum.Enum):
    admin = "admin"
    reviewer = "reviewer"


# --- Tables --------------------------------------------------------------


class Prospect(Base):
    __tablename__ = "prospects"

    id: Mapped[uuid.UUID] = _uuid_pk()

    name: Mapped[str] = mapped_column(Text, nullable=False)
    trade_name: Mapped[str | None] = mapped_column(Text)
    legal_form: Mapped[str | None] = mapped_column(String(20))
    rut: Mapped[str | None] = mapped_column(String(12), unique=True)

    category: Mapped[ProspectCategory | None] = mapped_column(
        Enum(ProspectCategory, name="prospect_category")
    )
    specialties: Mapped[list[str] | None] = mapped_column(ARRAY(Text))

    region: Mapped[str | None] = mapped_column(Text)
    comuna: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    address_normalized: Mapped[str | None] = mapped_column(Text)

    phone: Mapped[str | None] = mapped_column(String(20))
    phone_raw: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    social_media: Mapped[dict | None] = mapped_column(JSONB)

    google_place_id: Mapped[str | None] = mapped_column(Text, unique=True)
    google_rating: Mapped[float | None] = mapped_column(Float)
    google_ratings_total: Mapped[int | None] = mapped_column(Integer)
    google_maps_url: Mapped[str | None] = mapped_column(Text)

    employee_count_estimate: Mapped[str | None] = mapped_column(Text)
    number_of_locations: Mapped[int | None] = mapped_column(Integer)

    commercial_potential_score: Mapped[float | None] = mapped_column(Numeric(5, 2))
    commercial_potential_level: Mapped[CommercialPotentialLevel | None] = mapped_column(
        Enum(CommercialPotentialLevel, name="commercial_potential_level")
    )
    scoring_breakdown: Mapped[dict | None] = mapped_column(JSONB)

    status: Mapped[ProspectStatus] = mapped_column(
        Enum(ProspectStatus, name="prospect_status"), default=ProspectStatus.new, nullable=False
    )
    dedup_status: Mapped[DedupStatus] = mapped_column(
        Enum(DedupStatus, name="dedup_status"), default=DedupStatus.unique, nullable=False
    )
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospects.id")
    )

    crm_id: Mapped[str | None] = mapped_column(Text)
    crm_sync_status: Mapped[CRMSyncStatus] = mapped_column(
        Enum(CRMSyncStatus, name="crm_sync_status"),
        default=CRMSyncStatus.not_synced,
        nullable=False,
    )
    crm_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    crm_sync_error: Mapped[str | None] = mapped_column(Text)

    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
    created_by: Mapped[str | None] = mapped_column(Text)

    sources: Mapped[list["ProspectSource"]] = relationship(back_populates="prospect")


class ProspectSource(Base):
    __tablename__ = "prospect_sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    prospect_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospects.id"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("research_jobs.id")
    )
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type"), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    raw_data: Mapped[dict | None] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    fetched_by: Mapped[str | None] = mapped_column(Text)

    prospect: Mapped["Prospect"] = relationship(back_populates="sources")


class ResearchJob(Base):
    __tablename__ = "research_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_type: Mapped[JobType] = mapped_column(Enum(JobType, name="job_type"), nullable=False)
    parameters: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), default=JobStatus.queued, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triggered_by: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict | None] = mapped_column(JSONB)
    error_log: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class DedupCandidate(Base):
    __tablename__ = "dedup_candidates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    prospect_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospects.id"), nullable=False
    )
    prospect_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospects.id"), nullable=False
    )
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    match_reasons: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[DedupCandidateStatus] = mapped_column(
        Enum(DedupCandidateStatus, name="dedup_candidate_status"),
        default=DedupCandidateStatus.pending,
        nullable=False,
    )
    reviewed_by: Mapped[str | None] = mapped_column(Text)

    prospect_a: Mapped["Prospect"] = relationship(foreign_keys=[prospect_a_id])
    prospect_b: Mapped["Prospect"] = relationship(foreign_keys=[prospect_b_id])
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (UniqueConstraint("prospect_a_id", "prospect_b_id", name="uq_dedup_pair"),)


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_by: Mapped[str | None] = mapped_column(Text)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    column_mapping: Mapped[dict | None] = mapped_column(JSONB)
    row_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[ImportBatchStatus] = mapped_column(
        Enum(ImportBatchStatus, name="import_batch_status"),
        default=ImportBatchStatus.uploaded,
        nullable=False,
    )
    storage_path: Mapped[str | None] = mapped_column(Text)


class CRMSyncLog(Base):
    __tablename__ = "crm_sync_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    prospect_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospects.id"), nullable=False
    )
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    direction: Mapped[CRMSyncDirection] = mapped_column(
        Enum(CRMSyncDirection, name="crm_sync_direction"), nullable=False
    )
    request_payload: Mapped[dict | None] = mapped_column(JSONB)
    response_payload: Mapped[dict | None] = mapped_column(JSONB)
    http_status: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error_message: Mapped[str | None] = mapped_column(Text)


class GoogleMapsQueryLog(Base):
    __tablename__ = "google_maps_query_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    query_type: Mapped[PlacesQueryType] = mapped_column(
        Enum(PlacesQueryType, name="places_query_type"), nullable=False
    )
    query_params: Mapped[dict | None] = mapped_column(JSONB)
    region: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    results_count: Mapped[int | None] = mapped_column(Integer)
    field_mask_tier: Mapped[PlacesFieldTier | None] = mapped_column(
        Enum(PlacesFieldTier, name="places_field_tier")
    )
    cost_estimate_usd: Mapped[float | None] = mapped_column(Numeric(8, 4))
    created_at: Mapped[datetime] = _created_at()


class BraveSearchQueryLog(Base):
    __tablename__ = "brave_search_query_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    crm_run_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    query_kind: Mapped[str] = mapped_column(String(30), nullable=False, default="discovery")
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    results_count: Mapped[int | None] = mapped_column(Integer)
    cost_estimate_usd: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class BraveUsageReconciliation(Base):
    __tablename__ = "brave_usage_reconciliation"

    month_key: Mapped[str] = mapped_column(String(7), primary_key=True)
    provider_queries: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_spend_usd: Mapped[float] = mapped_column(Numeric(8, 4), nullable=False)
    provider_limit_queries: Mapped[int | None] = mapped_column(Integer)
    provider_remaining_queries: Mapped[int | None] = mapped_column(Integer)
    reset_seconds: Mapped[int | None] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = _updated_at()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.reviewer, nullable=False
    )
    created_at: Mapped[datetime] = _created_at()


class RegionComuna(Base):
    __tablename__ = "regions_comunas"

    id: Mapped[uuid.UUID] = _uuid_pk()
    region: Mapped[str] = mapped_column(Text, nullable=False)
    comuna: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("region", "comuna", name="uq_region_comuna"),)


# --- CRM-controlled prospecting worker ---------------------------------


class ProspectingRun(Base):
    """Local durable mirror of an immutable run claimed from the CRM."""

    __tablename__ = "prospecting_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    crm_run_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    crm_campaign_id: Mapped[str] = mapped_column(Text, nullable=False)
    campaign_version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    remote_candidates_baseline: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    crm_worker_id: Mapped[str | None] = mapped_column(Text)
    crm_lease_token: Mapped[str | None] = mapped_column(Text)
    crm_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict | None] = mapped_column(JSONB)
    error_log: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class ProspectingTask(Base):
    __tablename__ = "prospecting_tasks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospecting_runs.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    keyword: Mapped[str] = mapped_column(Text, nullable=False)
    region_code: Mapped[str] = mapped_column(String(10), nullable=False)
    region_name: Mapped[str] = mapped_column(Text, nullable=False)
    comuna_code: Mapped[str] = mapped_column(String(10), nullable=False)
    comuna_name: Mapped[str] = mapped_column(Text, nullable=False)
    max_results: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    lease_owner: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    results_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_log: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "source",
            "keyword",
            "comuna_code",
            name="uq_prospecting_task_scope",
        ),
    )


class ProspectingCandidateRecord(Base):
    __tablename__ = "prospecting_candidate_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospecting_runs.id", ondelete="CASCADE"), nullable=False
    )
    candidate_key: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    dedup_disposition: Mapped[str] = mapped_column(String(32), nullable=False, default="unique")
    possible_duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospecting_candidate_records.id")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (UniqueConstraint("run_id", "candidate_key", name="uq_run_candidate_key"),)


class ProspectEvidenceRecord(Base):
    __tablename__ = "prospect_evidence_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("prospecting_candidate_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_record_id: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    field: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class ProspectingEventRecord(Base):
    __tablename__ = "prospecting_event_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospecting_runs.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospecting_tasks.id", ondelete="SET NULL")
    )
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    stage: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class CRMOutboxMessage(Base):
    __tablename__ = "crm_outbox_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prospecting_runs.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
