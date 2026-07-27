"""widen fee_collection to match transfer's new per-parcel key

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-26

fee_collection is empty (0 rows) so this is a pure schema change: add
recording_date + parcel_apn (both NOT NULL, matching transfer's widened
key -- see 0023), widen the PK to include them alongside the existing
collection_seq (multiple fee events can still occur against the same
instrument+parcel, e.g. a corrected re-invoice), and re-point the FK at
transfer's new 4-column key.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.add_column(
        "fee_collection", sa.Column("recording_date", sa.Date(), nullable=False), schema=SCHEMA,
    )
    op.add_column(
        "fee_collection", sa.Column("parcel_apn", sa.Text(), nullable=False), schema=SCHEMA,
    )
    op.execute(f"ALTER TABLE {SCHEMA}.fee_collection DROP CONSTRAINT fee_collection_pkey CASCADE")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.fee_collection
        ADD PRIMARY KEY (county_fips, instrument_number, recording_date, parcel_apn, collection_seq)
    """)
    op.execute(f"""
        ALTER TABLE {SCHEMA}.fee_collection
        ADD CONSTRAINT fee_collection_transfer_fkey
        FOREIGN KEY (county_fips, instrument_number, recording_date, parcel_apn)
        REFERENCES {SCHEMA}.transfer (county_fips, instrument_number, recording_date, parcel_apn)
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.fee_collection DROP CONSTRAINT fee_collection_transfer_fkey")
    op.execute(f"ALTER TABLE {SCHEMA}.fee_collection DROP CONSTRAINT fee_collection_pkey")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.fee_collection
        ADD PRIMARY KEY (county_fips, instrument_number, collection_seq)
    """)
    op.drop_column("fee_collection", "parcel_apn", schema=SCHEMA)
    op.drop_column("fee_collection", "recording_date", schema=SCHEMA)
