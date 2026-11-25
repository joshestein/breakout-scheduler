"""round status archived

A round's lifecycle is now one field: collecting -> published -> archived. This
replaces the archived_at column.

Participants now see only the oldest round that is not archived. Before, they saw
the newest published round. So the upgrade also archives every round older than an
event's newest published round, and each event keeps showing the same schedule.

Revision ID: e9b3f5a2c8d1
Revises: d4e8a1c6b2f7
Create Date: 2026-11-25 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9b3f5a2c8d1'
down_revision: Union[str, Sequence[str], None] = 'd4e8a1c6b2f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema and data."""
    op.execute("UPDATE rounds SET status = 'archived' WHERE archived_at IS NOT NULL")
    # the newest published round is the one attendees saw: archive the rounds before it
    op.execute("""
        UPDATE rounds SET status = 'archived'
        WHERE status != 'archived'
          AND id < (SELECT MAX(r2.id) FROM rounds AS r2
                    WHERE r2.event_id = rounds.event_id AND r2.status = 'published')
    """)
    with op.batch_alter_table('rounds', schema=None) as batch_op:
        batch_op.drop_column('archived_at')


def downgrade() -> None:
    """Downgrade schema. Superseded rounds stay archived."""
    with op.batch_alter_table('rounds', schema=None) as batch_op:
        batch_op.add_column(sa.Column('archived_at', sa.DateTime(), nullable=True))
    op.execute("""
        UPDATE rounds SET archived_at = CURRENT_TIMESTAMP,
            status = CASE WHEN published_at IS NULL THEN 'collecting' ELSE 'published' END
        WHERE status = 'archived'
    """)
