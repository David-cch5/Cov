"""add tract.stated_acreage -- a tract's own acreage, not its covenant's total

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-08

Confirmed real on covid 5838, not hypothetical. reconcile_tract reconciles a
'current_parcel_match' tract by comparing tract.classified_acreage against
covenant.stated_acreage. That is correct for a single-tract covenant and wrong
for every other one, because stated_acreage describes the WHOLE covenant.

covid 5838 tract 2 is the Gulfside Estates land -- the deed's own 31.140 and
2.454 acre tracts, 33.594 ac together, and 33.518 ac as classified. It was being
measured against the covenant's 318.779 ac, which is tract 1's figure, and
reported 285.261 ac "unaccounted": a gap 850% of the tract's own size, on a tract
that is fully accounted for.

Six covenants in this corpus carry more than one tract (3194, 3346, 4123, 4440,
4780, 5838). Most escaped the bug only because their tracts are
metes_and_bounds_traverse, which reconciles against residual geometry and never
reads stated_acreage at all.

Nullable on purpose. A tract whose own acreage has not been read from its deed
must NOT silently fall back to the covenant total -- reconcile_tract now reports
such a tract as not-checkable instead, which is the honest answer and follows
CLAUDE.md's rule against filling a gap with a guessed value.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.tract ADD COLUMN stated_acreage NUMERIC(12,3)")
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.tract.stated_acreage IS
        'Acreage this tract''s own deed states for itself. NULL when not yet read. '
        'Never defaults to covenant.stated_acreage, which covers all tracts together.'
    """)
    # A single-tract covenant's stated acreage IS that tract's acreage, so those
    # rows can be backfilled safely and without reading anything. Multi-tract
    # covenants are deliberately left NULL: their per-tract figures have to come
    # from each deed, and a split of the total would be invention.
    op.execute(f"""
        UPDATE {SCHEMA}.tract t
        SET stated_acreage = c.stated_acreage
        FROM {SCHEMA}.covenant c
        WHERE c.covid = t.covid
          AND c.stated_acreage IS NOT NULL
          AND (SELECT count(*) FROM {SCHEMA}.tract t2 WHERE t2.covid = t.covid) = 1
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.tract DROP COLUMN stated_acreage")
