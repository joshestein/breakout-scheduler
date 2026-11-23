"""drop cut features

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-23 12:00:00.000000

Removes the columns behind features cut in the simplification pass: repeat and
multi-slot sessions, "elsewhere" (roomless) sessions, force-run, anonymous
proposals, and per-slot room capacities. See simplifications.md.

Data loss on upgrade: roomless ("elsewhere") placements are deleted, because
placements.room_id becomes NOT NULL again. Back up first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Names for the constraints Alembic must re-create when SQLite batch-rebuilds a
# table; without these the reflected (unnamed) UNIQUE/FK raise "Constraint must
# have a name" in batch mode.
_NAMING = {
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

_PROPOSAL_COLS = ('slots_needed', 'allow_repeat', 'min_minutes', 'anonymous',
                  'needs_space', 'must_run')


def upgrade() -> None:
    op.execute("DELETE FROM placements WHERE room_id IS NULL")
    with op.batch_alter_table('placements', schema=None, naming_convention=_NAMING) as batch_op:
        batch_op.alter_column('room_id', existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column('span')
    with op.batch_alter_table('timeslot_rooms', schema=None, naming_convention=_NAMING) as batch_op:
        batch_op.drop_column('capacity')
    with op.batch_alter_table('proposals', schema=None, naming_convention=_NAMING) as batch_op:
        for col in _PROPOSAL_COLS:
            batch_op.drop_column(col)


def downgrade() -> None:
    with op.batch_alter_table('proposals', schema=None, naming_convention=_NAMING) as batch_op:
        batch_op.add_column(sa.Column('slots_needed', sa.Integer(), nullable=False,
                                      server_default='1'))
        batch_op.add_column(sa.Column('allow_repeat', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.add_column(sa.Column('min_minutes', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('anonymous', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.add_column(sa.Column('needs_space', sa.Boolean(), nullable=False,
                                      server_default=sa.true()))
        batch_op.add_column(sa.Column('must_run', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
    with op.batch_alter_table('timeslot_rooms', schema=None, naming_convention=_NAMING) as batch_op:
        batch_op.add_column(sa.Column('capacity', sa.Integer(), nullable=True))
    with op.batch_alter_table('placements', schema=None, naming_convention=_NAMING) as batch_op:
        batch_op.add_column(sa.Column('span', sa.Integer(), nullable=False, server_default='1'))
        batch_op.alter_column('room_id', existing_type=sa.Integer(), nullable=True)
