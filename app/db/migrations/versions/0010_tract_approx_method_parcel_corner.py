"""add 'matched_parcel_corner' as a tract.approximate_geom_method value

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-23

Distinct from 'geocoded_point_of_beginning' (a free-text place-name geocode,
multi-mile uncertainty): this is for when a metes-and-bounds tract's Point of
Beginning ties to a corner of an adjoining, already-platted parcel that a
real GIS query confirms still exists today (e.g. covid 4781's POB is stated
as the "Northwesterly corner of Restricted Reserve B, Palm Beach Estates" --
a specific reserve tract found by exact name/reserve match in Montgomery's
live parcel data, not a text-similarity guess). Meaningfully tighter
uncertainty (roughly the matched parcel's own extent) than a place-name
geocode, but still a heuristic (centroid-of-parcel proxy for the exact
corner, and no correction for the source document's local/plat-relative
bearing basis vs. true north) -- not a confirmed survey retracement, so it
stays in approximate_geom, never geom.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.tract DROP CONSTRAINT tract_approximate_geom_method_check")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.tract ADD CONSTRAINT tract_approximate_geom_method_check
          CHECK (approximate_geom_method IN
            ('geocoded_point_of_beginning', 'matched_parcel_corner', 'other'))
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.tract DROP CONSTRAINT tract_approximate_geom_method_check")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.tract ADD CONSTRAINT tract_approximate_geom_method_check
          CHECK (approximate_geom_method IN ('geocoded_point_of_beginning', 'other'))
    """)
