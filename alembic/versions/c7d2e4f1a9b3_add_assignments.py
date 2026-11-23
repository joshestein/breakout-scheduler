"""add assignments

Saves where the solver seated each person in a published round, so the
participant view shows that room instead of a guess from their stars.

Revision ID: c7d2e4f1a9b3
Revises: b1afe033c5d9
Create Date: 2026-11-23 23:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d2e4f1a9b3'
down_revision: Union[str, Sequence[str], None] = 'b1afe033c5d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('assignments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('timeslot_id', sa.Integer(), nullable=False),
    sa.Column('participant_id', sa.Integer(), nullable=False),
    sa.Column('proposal_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['participant_id'], ['participants.id'], ),
    sa.ForeignKeyConstraint(['proposal_id'], ['proposals.id'], ),
    sa.ForeignKeyConstraint(['timeslot_id'], ['timeslots.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('timeslot_id', 'participant_id')
    )
    with op.batch_alter_table('assignments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_assignments_participant_id'), ['participant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_assignments_proposal_id'), ['proposal_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_assignments_timeslot_id'), ['timeslot_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('assignments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_assignments_timeslot_id'))
        batch_op.drop_index(batch_op.f('ix_assignments_proposal_id'))
        batch_op.drop_index(batch_op.f('ix_assignments_participant_id'))

    op.drop_table('assignments')
