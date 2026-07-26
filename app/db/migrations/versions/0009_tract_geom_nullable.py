"""allow tract.geom to be null when only an approximate placement exists

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-23

geom was NOT NULL from the original schema, written when every tract came
from a confirmed parcel union (subdivision_plat path always produces one).
The metes-and-bounds path can now validate a boundary SHAPE (closure +
acreage match) without a confirmed real-world anchor -- see approximate_geom
(migration 0008). Forcing a value into geom in that case would fabricate a
location, which CLAUDE.md's never-fabricate rule rules out. A tract row must
still carry at least one of the two geometries -- never neither -- enforced
below rather than left as an unstated assumption.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.tract ALTER COLUMN geom DROP NOT NULL")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.tract ADD CONSTRAINT tract_geom_or_approximate_geom_present
          CHECK (geom IS NOT NULL OR approximate_geom IS NOT NULL)
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.tract DROP CONSTRAINT tract_geom_or_approximate_geom_present")
    op.execute(f"ALTER TABLE {SCHEMA}.tract ALTER COLUMN geom SET NOT NULL")
