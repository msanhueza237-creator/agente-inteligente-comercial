"""CRM-controlled prospecting worker persistence

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def upgrade() -> None:
    op.execute("ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'brave_search'")
    op.execute("ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'official_website'")

    op.create_table(
        "prospecting_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("crm_run_id", sa.Text(), nullable=False, unique=True),
        sa.Column("crm_campaign_id", sa.Text(), nullable=False),
        sa.Column("campaign_version", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("crm_lease_token", sa.Text()),
        sa.Column("crm_lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("stats", postgresql.JSONB()),
        sa.Column("error_log", sa.Text()),
        *_timestamps(),
    )
    op.create_index("ix_prospecting_runs_status", "prospecting_runs", ["status"])

    op.create_table(
        "prospecting_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("keyword", sa.Text(), nullable=False),
        sa.Column("region_code", sa.String(10), nullable=False),
        sa.Column("region_name", sa.Text(), nullable=False),
        sa.Column("comuna_code", sa.String(10), nullable=False),
        sa.Column("comuna_name", sa.Text(), nullable=False),
        sa.Column("max_results", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("results_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_log", sa.Text()),
        *_timestamps(),
        sa.ForeignKeyConstraint(["run_id"], ["prospecting_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "run_id", "source", "keyword", "comuna_code", name="uq_prospecting_task_scope"
        ),
    )
    op.create_index(
        "ix_prospecting_tasks_claim",
        "prospecting_tasks",
        ["run_id", "status", "available_at", "lease_expires_at"],
    )

    op.create_table(
        "prospecting_candidate_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_key", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("dedup_disposition", sa.String(32), nullable=False, server_default="unique"),
        sa.Column("possible_duplicate_of", postgresql.UUID(as_uuid=True)),
        *_timestamps(),
        sa.ForeignKeyConstraint(["run_id"], ["prospecting_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["possible_duplicate_of"], ["prospecting_candidate_records.id"]),
        sa.UniqueConstraint("run_id", "candidate_key", name="uq_run_candidate_key"),
    )

    op.create_table(
        "prospect_evidence_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("provider_record_id", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("field", sa.String(80), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_until", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["prospecting_candidate_records.id"], ondelete="CASCADE"
        ),
    )

    op.create_table(
        "prospecting_event_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False, unique=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True)),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(80), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("metrics", postgresql.JSONB()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["run_id"], ["prospecting_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["prospecting_tasks.id"], ondelete="SET NULL"),
    )

    op.create_table(
        "crm_outbox_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_error", sa.Text()),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.ForeignKeyConstraint(["run_id"], ["prospecting_runs.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_crm_outbox_delivery",
        "crm_outbox_messages",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_table("crm_outbox_messages")
    op.drop_table("prospecting_event_records")
    op.drop_table("prospect_evidence_records")
    op.drop_table("prospecting_candidate_records")
    op.drop_table("prospecting_tasks")
    op.drop_table("prospecting_runs")
