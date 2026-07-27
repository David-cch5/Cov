"""widen fee_payoff_statement to match fee_collection's new per-parcel key

Revision ID: 0028
Revises: 0027
Create Date: 2026-07-26

fee_payoff_statement is empty (0 rows). Cascades the same recording_date +
parcel_apn widening down from fee_collection (0024), since a payoff
statement is generated for one specific parcel's fee_collection row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.add_column(
        "fee_payoff_statement", sa.Column("recording_date", sa.Date(), nullable=False), schema=SCHEMA,
    )
    op.add_column(
        "fee_payoff_statement", sa.Column("parcel_apn", sa.Text(), nullable=False), schema=SCHEMA,
    )
    op.execute(f"ALTER TABLE {SCHEMA}.fee_payoff_statement DROP CONSTRAINT fee_payoff_statement_pkey CASCADE")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.fee_payoff_statement
        ADD PRIMARY KEY (county_fips, instrument_number, recording_date, parcel_apn, collection_seq, statement_seq)
    """)
    op.execute(f"""
        ALTER TABLE {SCHEMA}.fee_payoff_statement
        ADD CONSTRAINT fee_payoff_statement_fee_collection_fkey
        FOREIGN KEY (county_fips, instrument_number, recording_date, parcel_apn, collection_seq)
        REFERENCES {SCHEMA}.fee_collection (county_fips, instrument_number, recording_date, parcel_apn, collection_seq)
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.fee_payoff_statement DROP CONSTRAINT fee_payoff_statement_fee_collection_fkey")
    op.execute(f"ALTER TABLE {SCHEMA}.fee_payoff_statement DROP CONSTRAINT fee_payoff_statement_pkey")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.fee_payoff_statement
        ADD PRIMARY KEY (county_fips, instrument_number, collection_seq, statement_seq)
    """)
    op.drop_column("fee_payoff_statement", "parcel_apn", schema=SCHEMA)
    op.drop_column("fee_payoff_statement", "recording_date", schema=SCHEMA)
