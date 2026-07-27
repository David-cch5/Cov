"""widen transfer's primary key to (county_fips, instrument_number, recording_date, parcel_apn)

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-26

Two real problems this fixes, both found while chain-walking real data:

1. One recorded instrument routinely conveys MULTIPLE parcels at once (a
   developer selling a group of platted lots in a single deed -- confirmed
   directly against covid 3595's 6 lots: both of its real historical
   sales, recording numbers 9220140 and 2021070554, cover all 6 lots
   identically). The old PK (county_fips, instrument_number) allows only
   one row per instrument, so it can't represent "this instrument, this
   specific parcel" -- exactly what's needed so a later fetch for a NEW
   parcel that turns out to share the same bulk instrument doesn't need
   special-casing to avoid a duplicate, and so each parcel's own
   transfer/fee history can be tracked independently.

2. Stakeholder-confirmed: some counties reuse instrument numbers on a
   yearly reset, so (county_fips, instrument_number) alone is not always
   truly unique across time either -- recording_date disambiguates that.
   (grantor/grantee were considered too, but rejected: OCR/index name
   spelling variance -- confirmed hands-on on the Bexar case, "ABRAMOFF
   EFRAIM" vs "EFRAIM ABRAMOFF, AN INDIVIDUAL..." -- makes names unsafe
   key material; a hard date value doesn't have that problem.)

Uses CASCADE to drop the four dependent FK constraints (fee_collection,
price_estimate, event, recorder_document_image) along with the old PK --
each of those tables is widened with its own new FK in the migrations that
follow. Safe against current data: transfer has 3 rows (covid 2497), all
with non-null recording_date and parcel_apn already (verified before
writing this).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.transfer DROP CONSTRAINT transfer_pkey CASCADE")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.transfer
        ADD PRIMARY KEY (county_fips, instrument_number, recording_date, parcel_apn)
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.transfer DROP CONSTRAINT transfer_pkey")
    op.execute(f"ALTER TABLE {SCHEMA}.transfer ADD PRIMARY KEY (county_fips, instrument_number)")
