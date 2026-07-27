"""create parcel_lineage -- tracks parcel splits/replats/merges over time

Revision ID: 0029
Revises: 0028
Create Date: 2026-07-26

The gap this closes: a fee owed on a bulk/tract-level transfer (e.g. B
sells a 20-acre tract to C, fee unpaid) attaches to the LAND under this
covenant family's own lien language (Section 8: the Lien "relate[s] back
to... recording of the original Reconveyance Fee covenant," binding
"successors... acquiring title by or through Declarant"). If C later
splits that tract, plats it into 100 lots, and one of those lots is sold
years afterward, that specific lot's own parcel_apn never directly
appears on the original unpaid transfer -- there is no way, today, to
walk from a current lot back to an ancestral tract's fee_collection rows
when generating a payoff/resale-certificate demand.

Modeled directly on a real county's own approach: Douglas County, CO's
open-data "Parcel_Lineage" layer is exactly this shape (ACCOUNT_NO ->
PARENT_ACCOUNT_NO + the split's recording number) -- confirmed by
inspecting its schema live (2026-07-26), though that specific layer had
no populated rows for this project's own test parcels.

A plain edge list rather than a strict tree: (county_fips, apn) can
appear as a child of MULTIPLE parents (a merge combining two prior
parcels into one) as well as a parent of multiple children (an ordinary
split) -- both need to be representable without special-casing.

Nothing populates this yet -- that's the Monitor's job (BUILD_SPEC.md
Sec. 10, "periodic spatial diff for new parcels/plats... update
lineage"), not yet built. This migration only adds the place for it to
write to.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE {SCHEMA}.parcel_lineage (
            county_fips              CHAR(5) NOT NULL,
            apn                      TEXT NOT NULL,
            parent_county_fips       CHAR(5) NOT NULL,
            parent_apn               TEXT NOT NULL,
            lineage_type             TEXT NOT NULL DEFAULT 'unknown'
                CHECK (lineage_type IN ('subdivision_split', 'replat', 'merge', 'unknown')),
            split_instrument_number  TEXT,
            effective_date           DATE,
            source_id                BIGINT REFERENCES {SCHEMA}.source(source_id),
            PRIMARY KEY (county_fips, apn, parent_county_fips, parent_apn),
            FOREIGN KEY (county_fips, apn) REFERENCES {SCHEMA}.parcel(county_fips, apn),
            FOREIGN KEY (parent_county_fips, parent_apn) REFERENCES {SCHEMA}.parcel(county_fips, apn),
            CHECK (NOT (county_fips = parent_county_fips AND apn = parent_apn))
        )
    """)
    op.execute(f"""
        CREATE INDEX parcel_lineage_parent_idx ON {SCHEMA}.parcel_lineage (parent_county_fips, parent_apn)
    """)


def downgrade() -> None:
    op.execute(f"DROP TABLE {SCHEMA}.parcel_lineage")
