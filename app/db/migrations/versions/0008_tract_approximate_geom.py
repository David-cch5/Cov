"""add tract.approximate_geom -- a clearly-separate, low-confidence placement

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-23

Deliberately a distinct column from tract.geom, never the same field. geom means
"this is where the tract actually is" (survey-anchored or GIS-parcel-confirmed);
approximate_geom means "a rough, flagged estimate" -- e.g. a geocoded Point of
Beginning reference -- for metes-and-bounds tracts whose shape is validated
(closure + acreage match) but not yet anchored to a real surveyed position.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.tract ADD COLUMN approximate_geom geometry(MultiPolygon, 4326)")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.tract ADD COLUMN approximate_geom_method TEXT CHECK (approximate_geom_method IN
          ('geocoded_point_of_beginning', 'other'))
    """)
    op.execute(f"ALTER TABLE {SCHEMA}.tract ADD COLUMN approximate_geom_confidence NUMERIC(4,3)")
    op.execute(f"ALTER TABLE {SCHEMA}.tract ADD COLUMN approximate_geom_notes TEXT")
    op.execute(f"CREATE INDEX tract_approximate_geom_gix ON {SCHEMA}.tract USING GIST (approximate_geom)")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.tract_approximate_geom_gix")
    op.execute(f"ALTER TABLE {SCHEMA}.tract DROP COLUMN approximate_geom_notes")
    op.execute(f"ALTER TABLE {SCHEMA}.tract DROP COLUMN approximate_geom_confidence")
    op.execute(f"ALTER TABLE {SCHEMA}.tract DROP COLUMN approximate_geom_method")
    op.execute(f"ALTER TABLE {SCHEMA}.tract DROP COLUMN approximate_geom")
