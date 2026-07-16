"""Persist Brave query usage for hard monthly budget enforcement."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brave_search_query_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("crm_run_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("query_kind", sa.String(30), nullable=False, server_default="discovery"),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("results_count", sa.Integer()),
        sa.Column("cost_estimate_usd", sa.Numeric(8, 4), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_brave_search_query_log_crm_run_id", "brave_search_query_log", ["crm_run_id"])
    op.create_index("ix_brave_search_query_log_created_at", "brave_search_query_log", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_brave_search_query_log_created_at", table_name="brave_search_query_log")
    op.drop_index("ix_brave_search_query_log_crm_run_id", table_name="brave_search_query_log")
    op.drop_table("brave_search_query_log")
