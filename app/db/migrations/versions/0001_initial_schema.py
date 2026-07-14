"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-07-09

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prospects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("trade_name", sa.Text(), nullable=True),
        sa.Column("legal_form", sa.String(length=20), nullable=True),
        sa.Column("rut", sa.String(length=12), nullable=True),
        sa.Column(
            "category",
            sa.Enum(
                "distributor",
                "retailer",
                "installer_large",
                "installer_independent",
                "maintenance",
                "refrigeration",
                "competitor",
                "other",
                name="prospect_category",
            ),
            nullable=True,
        ),
        sa.Column("specialties", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("comuna", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("address_normalized", sa.Text(), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("phone_raw", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("social_media", postgresql.JSONB(), nullable=True),
        sa.Column("google_place_id", sa.Text(), nullable=True),
        sa.Column("google_rating", sa.Float(), nullable=True),
        sa.Column("google_ratings_total", sa.Integer(), nullable=True),
        sa.Column("google_maps_url", sa.Text(), nullable=True),
        sa.Column("employee_count_estimate", sa.Text(), nullable=True),
        sa.Column("number_of_locations", sa.Integer(), nullable=True),
        sa.Column("commercial_potential_score", sa.Numeric(5, 2), nullable=True),
        sa.Column(
            "commercial_potential_level",
            sa.Enum("low", "medium", "high", "very_high", name="commercial_potential_level"),
            nullable=True,
        ),
        sa.Column("scoring_breakdown", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "new", "enriched", "reviewed", "approved", "rejected", "synced",
                name="prospect_status",
            ),
            nullable=False,
            server_default="new",
        ),
        sa.Column(
            "dedup_status",
            sa.Enum("unique", "needs_review", "merged", name="dedup_status"),
            nullable=False,
            server_default="unique",
        ),
        sa.Column("merged_into_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("crm_id", sa.Text(), nullable=True),
        sa.Column(
            "crm_sync_status",
            sa.Enum("not_synced", "pending", "synced", "error", name="crm_sync_status"),
            nullable=False,
            server_default="not_synced",
        ),
        sa.Column("crm_last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("crm_sync_error", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["merged_into_id"], ["prospects.id"]),
        sa.UniqueConstraint("rut"),
        sa.UniqueConstraint("google_place_id"),
    )
    op.create_index("ix_prospects_region", "prospects", ["region"])
    op.create_index("ix_prospects_comuna", "prospects", ["comuna"])
    op.create_index("ix_prospects_category", "prospects", ["category"])
    op.create_index("ix_prospects_status", "prospects", ["status"])
    op.create_index("ix_prospects_dedup_status", "prospects", ["dedup_status"])
    op.create_index("ix_prospects_crm_sync_status", "prospects", ["crm_sync_status"])
    op.create_index(
        "ix_prospects_potential_score", "prospects", ["commercial_potential_score"]
    )

    op.create_table(
        "research_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_type",
            sa.Enum(
                "region_category_search",
                "excel_import",
                "manual_search",
                "enrichment_refresh",
                "dedup_scan",
                "crm_sync_batch",
                name="job_type",
            ),
            nullable=False,
        ),
        sa.Column("parameters", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("queued", "running", "completed", "failed", "partial", name="job_status"),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_by", sa.Text(), nullable=True),
        sa.Column("stats", postgresql.JSONB(), nullable=True),
        sa.Column("error_log", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_research_jobs_status", "research_jobs", ["status"])
    op.create_index("ix_research_jobs_job_type", "research_jobs", ["job_type"])

    op.create_table(
        "prospect_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("prospect_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "source_type",
            sa.Enum(
                "google_places",
                "website_scrape",
                "social_media",
                "excel_import",
                "manual_edit",
                "llm_enrichment",
                "crm",
                name="source_type",
            ),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("raw_data", postgresql.JSONB(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("fetched_by", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["prospect_id"], ["prospects.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["research_jobs.id"]),
    )
    op.create_index("ix_prospect_sources_prospect_id", "prospect_sources", ["prospect_id"])
    op.create_index("ix_prospect_sources_job_id", "prospect_sources", ["job_id"])

    op.create_table(
        "dedup_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("prospect_a_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("prospect_b_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=False),
        sa.Column("match_reasons", postgresql.JSONB(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending", "merged", "rejected_not_duplicate", name="dedup_candidate_status"
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["prospect_a_id"], ["prospects.id"]),
        sa.ForeignKeyConstraint(["prospect_b_id"], ["prospects.id"]),
        sa.UniqueConstraint("prospect_a_id", "prospect_b_id", name="uq_dedup_pair"),
    )
    op.create_index("ix_dedup_candidates_status", "dedup_candidates", ["status"])

    op.create_table(
        "import_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("uploaded_by", sa.Text(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("column_mapping", postgresql.JSONB(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "uploaded", "processing", "completed", "failed", name="import_batch_status"
            ),
            nullable=False,
            server_default="uploaded",
        ),
        sa.Column("storage_path", sa.Text(), nullable=True),
    )

    op.create_table(
        "crm_sync_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("prospect_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "direction", sa.Enum("search", "upsert", name="crm_sync_direction"), nullable=False
        ),
        sa.Column("request_payload", postgresql.JSONB(), nullable=True),
        sa.Column("response_payload", postgresql.JSONB(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["prospect_id"], ["prospects.id"]),
    )
    op.create_index("ix_crm_sync_log_prospect_id", "crm_sync_log", ["prospect_id"])

    op.create_table(
        "google_maps_query_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "query_type",
            sa.Enum(
                "text_search", "nearby_search", "place_details", name="places_query_type"
            ),
            nullable=False,
        ),
        sa.Column("query_params", postgresql.JSONB(), nullable=True),
        sa.Column("region", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("results_count", sa.Integer(), nullable=True),
        sa.Column(
            "field_mask_tier", sa.Enum("pro", "enterprise", name="places_field_tier"), nullable=True
        ),
        sa.Column("cost_estimate_usd", sa.Numeric(8, 4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_google_maps_query_log_created_at", "google_maps_query_log", ["created_at"])

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "role", sa.Enum("admin", "reviewer", name="user_role"), nullable=False,
            server_default="reviewer",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("email"),
    )

    op.create_table(
        "regions_comunas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("comuna", sa.Text(), nullable=False),
        sa.UniqueConstraint("region", "comuna", name="uq_region_comuna"),
    )


def downgrade() -> None:
    op.drop_table("regions_comunas")
    op.drop_table("users")
    op.drop_table("google_maps_query_log")
    op.drop_table("crm_sync_log")
    op.drop_table("import_batches")
    op.drop_table("dedup_candidates")
    op.drop_table("prospect_sources")
    op.drop_table("research_jobs")
    op.drop_table("prospects")

    for enum_name in (
        "prospect_category",
        "commercial_potential_level",
        "prospect_status",
        "dedup_status",
        "crm_sync_status",
        "job_type",
        "job_status",
        "source_type",
        "dedup_candidate_status",
        "import_batch_status",
        "crm_sync_direction",
        "places_query_type",
        "places_field_tier",
        "user_role",
    ):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
