"""a plat splits a tract into MANY lots, not two

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-12

0046 modelled a split as a deed making two children -- the piece conveyed and the
piece retained -- which is right for a conveyance and wrong for the event that
actually created most of the lots this project holds.

A PLAT CONVEYS NOTHING. It subdivides. One instrument turns raw acreage into N lots
at once (PALMILLA BEACH unit 7's plat created 143 of them in a single filing), and
forcing those children into 'conveyed' would assert 143 conveyances that never
happened -- and the unique index on (parent, instrument, disposition) would reject
all but the first anyway.

So 'platted' joins the dispositions, and the split index widens to include the
child's own label. The two-halves rule survives intact and is what makes the event
honest: the platted lots PLUS a retained remainder for the acreage the plat left
raw. That remainder is the same quantity app/gis/monitor.py watches for new plats,
now expressed as a node in the spine rather than only as a number on the tract.
"""
from alembic import op

from app.config import DB_SCHEMA as SCHEMA

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.tract_node DROP CONSTRAINT tract_node_disposition_check")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.tract_node ADD CONSTRAINT tract_node_disposition_check
            CHECK (disposition IN ('root', 'conveyed', 'retained', 'platted'))
    """)
    # N children may share one instrument and disposition -- they are the lots one
    # plat created. Their labels still have to differ, so the same lot cannot be
    # recorded twice under the same filing.
    op.execute(f"DROP INDEX {SCHEMA}.tract_node_one_split_per_instrument")
    op.execute(f"""
        CREATE UNIQUE INDEX tract_node_one_split_per_instrument
            ON {SCHEMA}.tract_node (parent_node_id, split_instrument_number, disposition,
                                     node_label)
         WHERE parent_node_id IS NOT NULL
    """)
    # A platted lot is identified by the parcel it became, so the same parcel must
    # not hang off the same tract twice however many times the back-fill runs.
    op.execute(f"""
        CREATE UNIQUE INDEX tract_node_one_node_per_parcel_per_tract
            ON {SCHEMA}.tract_node (covid, tract_no, county_fips, apn)
         WHERE apn IS NOT NULL
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.tract_node_one_node_per_parcel_per_tract")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.tract_node_one_split_per_instrument")
    op.execute(f"DELETE FROM {SCHEMA}.tract_node WHERE disposition = 'platted'")
    op.execute(f"ALTER TABLE {SCHEMA}.tract_node DROP CONSTRAINT tract_node_disposition_check")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.tract_node ADD CONSTRAINT tract_node_disposition_check
            CHECK (disposition IN ('root', 'conveyed', 'retained'))
    """)
    op.execute(f"""
        CREATE UNIQUE INDEX tract_node_one_split_per_instrument
            ON {SCHEMA}.tract_node (parent_node_id, split_instrument_number, disposition)
         WHERE parent_node_id IS NOT NULL
    """)
