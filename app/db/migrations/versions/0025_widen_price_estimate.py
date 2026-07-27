"""widen price_estimate to match transfer's new per-parcel key

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-26

Same treatment as fee_collection (0024): price_estimate is empty (0 rows),
add recording_date + parcel_apn (NOT NULL), widen the PK alongside the
existing `method` column, re-point the FK at transfer's new 4-column key.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.add_column(
        "price_estimate", sa.Column("recording_date", sa.Date(), nullable=False), schema=SCHEMA,
    )
    op.add_column(
        "price_estimate", sa.Column("parcel_apn", sa.Text(), nullable=False), schema=SCHEMA,
    )
    op.execute(f"ALTER TABLE {SCHEMA}.price_estimate DROP CONSTRAINT price_estimate_pkey CASCADE")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.price_estimate
        ADD PRIMARY KEY (county_fips, instrument_number, recording_date, parcel_apn, method)
    """)
    op.execute(f"""
        ALTER TABLE {SCHEMA}.price_estimate
        ADD CONSTRAINT price_estimate_transfer_fkey
        FOREIGN KEY (county_fips, instrument_number, recording_date, parcel_apn)
        REFERENCES {SCHEMA}.transfer (county_fips, instrument_number, recording_date, parcel_apn)
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.price_estimate DROP CONSTRAINT price_estimate_transfer_fkey")
    op.execute(f"ALTER TABLE {SCHEMA}.price_estimate DROP CONSTRAINT price_estimate_pkey")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.price_estimate
        ADD PRIMARY KEY (county_fips, instrument_number, method)
    """)
    op.drop_column("price_estimate", "parcel_apn", schema=SCHEMA)
    op.drop_column("price_estimate", "recording_date", schema=SCHEMA)
