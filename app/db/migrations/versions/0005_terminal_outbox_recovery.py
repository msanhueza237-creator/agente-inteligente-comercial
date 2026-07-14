"""Persist the worker identity used by terminal idempotent replays.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-13
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE prospecting_runs "
        "ADD COLUMN IF NOT EXISTS crm_worker_id TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE prospecting_runs DROP COLUMN IF EXISTS crm_worker_id")
