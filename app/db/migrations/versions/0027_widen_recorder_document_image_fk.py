"""widen recorder_document_image's optional transfer reference to match transfer's new per-parcel key

Revision ID: 0027
Revises: 0026
Create Date: 2026-07-26

Same treatment as event (0026): transfer_county_fips/transfer_instrument_number
were already nullable (a document image can instead reference an
estoppel_certificate -- see the existing mutual-exclusion check
constraint, untouched here). recorder_document_image is empty (0 rows).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.add_column("recorder_document_image", sa.Column("transfer_recording_date", sa.Date(), nullable=True), schema=SCHEMA)
    op.add_column("recorder_document_image", sa.Column("transfer_parcel_apn", sa.Text(), nullable=True), schema=SCHEMA)
    op.execute(f"""
        ALTER TABLE {SCHEMA}.recorder_document_image
        ADD CONSTRAINT recorder_document_image_transfer_fkey
        FOREIGN KEY (transfer_county_fips, transfer_instrument_number, transfer_recording_date, transfer_parcel_apn)
        REFERENCES {SCHEMA}.transfer (county_fips, instrument_number, recording_date, parcel_apn)
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.recorder_document_image DROP CONSTRAINT recorder_document_image_transfer_fkey")
    op.drop_column("recorder_document_image", "transfer_parcel_apn", schema=SCHEMA)
    op.drop_column("recorder_document_image", "transfer_recording_date", schema=SCHEMA)
