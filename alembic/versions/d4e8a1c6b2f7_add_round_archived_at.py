"""add round archived_at

An archived round is hidden from participants and attendees, and it no longer
blocks later rounds from opening.

Revision ID: d4e8a1c6b2f7
Revises: c7d2e4f1a9b3
Create Date: 2026-11-25 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e8a1c6b2f7'
down_revision: Union[str, Sequence[str], None] = 'c7d2e4f1a9b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('rounds', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archived_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('rounds', schema=None) as batch_op:
        batch_op.drop_column('archived_at')
