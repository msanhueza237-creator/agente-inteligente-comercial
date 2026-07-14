"""Add authoritative CRM candidate count and bounded outbox retries.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-13
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # IF NOT EXISTS also converges developer databases that briefly ran a
    # pre-release 0003 containing either column.
    op.execute(
        "ALTER TABLE prospecting_runs "
        "ADD COLUMN IF NOT EXISTS remote_candidates_baseline INTEGER NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE crm_outbox_messages "
        "ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 10"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE crm_outbox_messages DROP COLUMN IF EXISTS max_attempts")
    op.execute("ALTER TABLE prospecting_runs DROP COLUMN IF EXISTS remote_candidates_baseline")
