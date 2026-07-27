"""widen event's optional transfer reference to match transfer's new per-parcel key

Revision ID: 0026
Revises: 0025
Create Date: 2026-07-26

event.transfer_county_fips/transfer_instrument_number were already nullable
(an event doesn't always relate to a specific transfer) -- adds the two new
columns as nullable too, same optional-all-or-nothing shape as before
(Postgres's default MATCH SIMPLE skips FK validation if any referencing
column is null, unchanged behavior from the original 2-column version of
this same optional reference). event is empty (0 rows).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.add_column("event", sa.Column("transfer_recording_date", sa.Date(), nullable=True), schema=SCHEMA)
    op.add_column("event", sa.Column("transfer_parcel_apn", sa.Text(), nullable=True), schema=SCHEMA)
    op.execute(f"""
        ALTER TABLE {SCHEMA}.event
        ADD CONSTRAINT event_transfer_fkey
        FOREIGN KEY (transfer_county_fips, transfer_instrument_number, transfer_recording_date, transfer_parcel_apn)
        REFERENCES {SCHEMA}.transfer (county_fips, instrument_number, recording_date, parcel_apn)
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.event DROP CONSTRAINT event_transfer_fkey")
    op.drop_column("event", "transfer_parcel_apn", schema=SCHEMA)
    op.drop_column("event", "transfer_recording_date", schema=SCHEMA)
