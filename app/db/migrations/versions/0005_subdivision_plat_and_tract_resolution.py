"""add subdivision_plat legal-description type + tract.boundary_resolution_method

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-23

Real extraction results surfaced this gap directly: covid 3428 ("Lot 121-A...
Escondido Subdivision") and 5340 ("Lots 14/15, Block A/5, University Hill") are
both lot/plat references, not metes-and-bounds or PLSS -- but the schema only
offered those two plus texas_abstract/unknown, so the extractor was forced into
the closest-available (wrong) category for both. subdivision_plat is also the
operationally distinct case discussed for tract-boundary construction: it
resolves by unioning existing county parcel geometry, not by a COGO traverse.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.covenant DROP CONSTRAINT covenant_legal_description_type_check")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant ADD CONSTRAINT covenant_legal_description_type_check
        CHECK (legal_description_type IN ('texas_abstract','plss','metes_bounds','subdivision_plat','unknown'))
    """)
    op.execute(f"""
        ALTER TABLE {SCHEMA}.tract ADD COLUMN boundary_resolution_method TEXT CHECK (boundary_resolution_method IN
          ('current_parcel_match','plat_archive_lookup','metes_and_bounds_traverse','manual'))
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.tract DROP COLUMN boundary_resolution_method")
    op.execute(f"ALTER TABLE {SCHEMA}.covenant DROP CONSTRAINT covenant_legal_description_type_check")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant ADD CONSTRAINT covenant_legal_description_type_check
        CHECK (legal_description_type IN ('texas_abstract','plss','metes_bounds','unknown'))
    """)
