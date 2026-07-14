"""add paginas_amarillas to source_type enum

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-12

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE source_type ADD VALUE IF NOT EXISTS 'paginas_amarillas'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE; removing an enum value
    # requires rebuilding the type. Not needed for a straightforward
    # additive change, so downgrade is a no-op here.
    pass
