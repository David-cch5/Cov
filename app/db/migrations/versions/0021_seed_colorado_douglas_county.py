"""seed Colorado state + Douglas County reference data (disclosure-state test case)

Revision ID: 0021
Revises: 0020
Create Date: 2026-07-26

Colorado is a full-disclosure state (actual sale consideration is a matter
of public record via the deed and/or the county's own Declaration of Value/
sales data) -- picked per stakeholder direction to build and test actual
(not estimated) sale-price extraction, distinct from and prerequisite to
the deferred non-disclosure-state estimation work.

Douglas County FIPS (08035) verified directly against the Census Bureau's
own reference file (national_county2020.txt: "CO|08|035|...|Douglas
County"), not assumed from memory.

Test covenant: covid 3595 (Douglas County, recorded 2009-09-18, Reception
#2009073674) -- already classified as template V01 in Covenant_Matrix
(confidence 1.0), and its own Section 5/6 text (1% fee, same 9-clause
exemption structure, same 01/01/2013 fixed cutoff) matches the existing
V01 template row/exemptions verbatim -- no new template work needed, only
the state/county/GIS/recorder plumbing this migration and the following
ones build. Encumbers 6 lots (Lots 9-14, Block 2, The Fairways at Lone
Tree Filing No. 2) -- under the 10-parcel test cap, no truncation needed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
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
        {"state_code": "CO", "state_name": "COLORADO", "is_disclosure_state": True},
    ])

    county_table = sa.table(
        "county",
        sa.column("county_fips", sa.Text),
        sa.column("state_code", sa.Text),
        sa.column("county_name", sa.Text),
        schema=SCHEMA,
    )
    op.bulk_insert(county_table, [
        {"county_fips": "08035", "state_code": "CO", "county_name": "DOUGLAS"},
    ])


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.county WHERE county_fips = '08035'")
    op.execute(f"DELETE FROM {SCHEMA}.state WHERE state_code = 'CO'")
