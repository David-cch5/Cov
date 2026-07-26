"""seed county reference data for the expanded 10-covenant, 10-county TX sample

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-24

Adds the 9 Texas counties (Nueces was already seeded in 0003) needed to run
the next small multi-county cost-probe sample per CLAUDE.md's scope guardrail:
one covenant each from Bexar, Denton, Collin, Harris, Kerr, Travis, Webb,
Ellis, and Hunt -- chosen as the highest-vocab_score (cleanest OCR) covenant
in each county from _pilot/covid_index.csv, picked to diversify the sample
beyond the DFW-heavy counties already covered (Montgomery, Tarrant, Dallas)
plus rural Llano.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    county_table = sa.table(
        "county",
        sa.column("county_fips", sa.Text),
        sa.column("state_code", sa.Text),
        sa.column("county_name", sa.Text),
        schema=SCHEMA,
    )
    op.bulk_insert(county_table, [
        {"county_fips": "48029", "state_code": "TX", "county_name": "BEXAR"},
        {"county_fips": "48121", "state_code": "TX", "county_name": "DENTON"},
        {"county_fips": "48085", "state_code": "TX", "county_name": "COLLIN"},
        {"county_fips": "48201", "state_code": "TX", "county_name": "HARRIS"},
        {"county_fips": "48265", "state_code": "TX", "county_name": "KERR"},
        {"county_fips": "48453", "state_code": "TX", "county_name": "TRAVIS"},
        {"county_fips": "48479", "state_code": "TX", "county_name": "WEBB"},
        {"county_fips": "48139", "state_code": "TX", "county_name": "ELLIS"},
        {"county_fips": "48231", "state_code": "TX", "county_name": "HUNT"},
    ])


def downgrade() -> None:
    op.execute(f"""
        DELETE FROM {SCHEMA}.county WHERE county_fips IN
        ('48029','48121','48085','48201','48265','48453','48479','48139','48231')
    """)
