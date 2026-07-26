"""seed state/county reference data for the Montgomery + pilot-county probe scope

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-23

Deliberately scoped to just the counties needed for the sanctioned cost probe
(Montgomery County TX + the 4-covenant multi-county pilot), not the full ~336
county/state pairs in the portfolio -- that full reference build-out is future
work once the probe results support scaling beyond this.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    state_table = sa.table(
        "state",
        sa.column("state_code", sa.Text),
        sa.column("state_name", sa.Text),
        sa.column("is_disclosure_state", sa.Boolean),
        schema=SCHEMA,
    )
    op.bulk_insert(state_table, [
        {"state_code": "TX", "state_name": "TEXAS", "is_disclosure_state": False},
    ])

    county_table = sa.table(
        "county",
        sa.column("county_fips", sa.Text),
        sa.column("state_code", sa.Text),
        sa.column("county_name", sa.Text),
        schema=SCHEMA,
    )
    op.bulk_insert(county_table, [
        {"county_fips": "48339", "state_code": "TX", "county_name": "MONTGOMERY"},
        {"county_fips": "48439", "state_code": "TX", "county_name": "TARRANT"},
        {"county_fips": "48113", "state_code": "TX", "county_name": "DALLAS"},
        {"county_fips": "48355", "state_code": "TX", "county_name": "NUECES"},
        {"county_fips": "48299", "state_code": "TX", "county_name": "LLANO"},
    ])


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.county WHERE state_code = 'TX'")
    op.execute(f"DELETE FROM {SCHEMA}.state WHERE state_code = 'TX'")
