"""create parcel_covenant_exclusion -- make a reviewed exclusion durable

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-06

Confirmed real and reproduced, not hypothetical: exclude_non_tract_parcels only
ever DELETED parcel_covenant rows, and both classify_metes_and_bounds_tract and
monitor_tract_for_new_plats rebuild that census from geometry alone. Neither had
any memory that a human had already reviewed a parcel and ruled it out, so the
next classification or monitoring pass silently put every excluded parcel back.

Demonstrated by monitor_tract_for_new_plats on covid 8245: after removing three
square-centimetre artifacts (0.01-0.06 m2 overlap, residential lots in a
different subdivision), a single monitoring run reported 4 "new" parcels -- the
one deliberately removed by the test fixture, plus all three exclusions coming
straight back. The same silently applied to covid 8534's 40 non-tract lots and
covid 4440's 22 deed-verified adjoiners: every exclusion this project has ever
made was one scheduled monitor run away from being undone.

That matters beyond a wrong count. parcel_covenant is what marks land as
encumbered, so a resurrected parcel is a property owner put back on the hook for
a 1% transfer fee -- two of covid 8245's three had already accumulated real,
non-exempt recorded transfers on that basis.

This table is the durable record of the human judgment itself, deliberately kept
separate from parcel_covenant (which is derived, rebuilt from geometry every run,
and correctly so). A row here means "reviewed, and this parcel is NOT part of the
encumbered land"; classification and monitoring both consult it before writing.
Removing a row is the explicit un-exclude path -- previously there was none, and
restoring a wrongly-excluded parcel (covid 4440, 6 of them, after deed-history
verification) was done by re-running classification, i.e. by relying on the very
bug this fixes.

reason is NOT NULL on purpose: an exclusion without a recorded justification is
exactly the un-auditable "guessed value" CLAUDE.md's never-fabricate rule exists
to prevent.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE {SCHEMA}.parcel_covenant_exclusion (
      county_fips   CHAR(5) NOT NULL,
      apn           TEXT NOT NULL,
      covid         INTEGER NOT NULL,
      tract_no      SMALLINT NOT NULL,
      reason        TEXT NOT NULL,
      excluded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY (county_fips, apn, covid, tract_no),
      FOREIGN KEY (county_fips, apn) REFERENCES {SCHEMA}.parcel(county_fips, apn),
      FOREIGN KEY (covid, tract_no)  REFERENCES {SCHEMA}.tract(covid, tract_no)
    )
    """)
    # Classification and monitoring both filter by (covid, tract_no) on every run.
    op.execute(f"""
        CREATE INDEX parcel_covenant_exclusion_tract_idx
        ON {SCHEMA}.parcel_covenant_exclusion (covid, tract_no)
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.parcel_covenant_exclusion_tract_idx")
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.parcel_covenant_exclusion")
