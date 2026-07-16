"""Reconcile local Brave ledger with provider rate-limit usage."""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brave_usage_reconciliation",
        sa.Column("month_key", sa.String(7), primary_key=True),
        sa.Column("provider_queries", sa.Integer(), nullable=False),
        sa.Column("provider_spend_usd", sa.Numeric(8, 4), nullable=False),
        sa.Column("provider_limit_queries", sa.Integer()),
        sa.Column("provider_remaining_queries", sa.Integer()),
        sa.Column("reset_seconds", sa.Integer()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("brave_usage_reconciliation")
