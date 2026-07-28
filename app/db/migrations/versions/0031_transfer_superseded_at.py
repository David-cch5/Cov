"""add superseded_at to transfer

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-27

Confirmed real (covid 3297, parcel 93070, multiple times this session): when
chain.py's walker is re-run after a classification fix (a newly-recognized doc
type, a corrected anchor match, ...) and finds a DIFFERENT chain for the same
parcel, the previous walk's transfer row was never cleaned up -- walk_chain_
of_title/_finalize only ever upserted the CURRENT walk's real_links, so a
superseded row just sat there indefinitely, indistinguishable from a current
one to any query.

Never simply deleted: a transfer row can carry real fee_collection history
(a fee already invoiced or collected against it), and CLAUDE.md's own "never
fabricate title data" cuts the other way here too -- silently discarding a
prior conveyance a human may have already acted on is its own kind of data
loss. superseded_at is nullable and NULL for every transfer today (backfilled
as such); chain.py's _finalize now sets it when a re-walk's real_links no
longer include a previously-recorded (instrument_number, recording_date) key
for that parcel, and clears it back to NULL if a later walk re-confirms the
same key. fee_compute.py's compute_fees_for_covid only iterates non-
superseded rows -- a superseded transfer's own fee_collection row (if any)
is left exactly as it was, not touched by this migration or by chain.py;
reconciling that is real, separate future work (deliberately out of scope
here, same as this migration not touching fee_payoff_statement).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.transfer ADD COLUMN superseded_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.transfer DROP COLUMN superseded_at")
