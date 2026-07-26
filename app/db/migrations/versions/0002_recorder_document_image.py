"""recorder_document_image -- deed images and estoppel certificate scans

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-23
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE {SCHEMA}.recorder_document_image (
      document_id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      document_type               TEXT NOT NULL CHECK (document_type IN
                                    ('deed_image','estoppel_certificate_image','other_recorder_document')),
      transfer_county_fips         CHAR(5),
      transfer_instrument_number   TEXT,
      estoppel_county_fips         CHAR(5),
      estoppel_instrument_number   TEXT,
      relpath                      TEXT NOT NULL UNIQUE,
      mime_type                    TEXT,
      pages                        INTEGER,
      ocr_engine                   TEXT CHECK (ocr_engine IN ('tesseract','fable5_vision','opus_vision','human')),
      confidence                   NUMERIC(4,3),
      source_id                    BIGINT REFERENCES {SCHEMA}.source(source_id),
      retrieved_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
      FOREIGN KEY (transfer_county_fips, transfer_instrument_number)
        REFERENCES {SCHEMA}.transfer(county_fips, instrument_number),
      FOREIGN KEY (estoppel_county_fips, estoppel_instrument_number)
        REFERENCES {SCHEMA}.estoppel_certificate(county_fips, instrument_number),
      CHECK (
        (transfer_county_fips IS NOT NULL AND estoppel_county_fips IS NULL) OR
        (estoppel_county_fips IS NOT NULL AND transfer_county_fips IS NULL)
      )
    )
    """)
    op.execute(f"""CREATE INDEX recorder_document_image_transfer_idx
                   ON {SCHEMA}.recorder_document_image(transfer_county_fips, transfer_instrument_number)""")
    op.execute(f"""CREATE INDEX recorder_document_image_estoppel_idx
                   ON {SCHEMA}.recorder_document_image(estoppel_county_fips, estoppel_instrument_number)""")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.recorder_document_image")
