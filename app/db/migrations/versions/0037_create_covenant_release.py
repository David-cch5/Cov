"""create covenant_release -- a covenant can END for some land, without erasing its past

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-09

Nothing in this system could say a covenant had STOPPED applying to land. There
was no table, no column, and covenant.status has no such value. Two real
situations need it, and they end the obligation for different reasons:

  termination  an instrument terminates the covenant as to some or all of the
               land (including the declarant's own right to terminate, which
               these deeds reserve in their paragraph 25)
  buyout       the fee obligation is bought out, after which the covenant is no
               longer enforced against that land

WHY THIS IS NOT parcel_covenant_exclusion
An exclusion says "this parcel was never part of this tract" -- geometric,
retroactive, and it has no date column at all. A release says "this parcel WAS
encumbered, and stopped being so on a date." Recording a release as an exclusion
would erase the period the covenant genuinely ran, and with it the basis for any
fee already collected. The two cannot share a mechanism, and the parcel stays in
the census here rather than disappearing from it -- the covenant-to-lot lineage
CLAUDE.md requires stays intact either way.

HISTORY IS PRESERVED BY CONSTRUCTION
A release carries an effective_date and changes nothing before it. Transfers
recorded earlier keep their fee_collection rows; a fee already collected remains
collected, correctly, because it was owed when it was taken. Only transfers on or
after the effective date become exempt. That is the same shape the system already
has at the other end of a covenant's life -- the pre_effective_date exemption --
so the two new exemption categories added here slot into machinery that exists.

SCOPE
scope='covenant' releases every parcel of the covenant; scope='partial' releases
only the parcels named in covenant_release_parcel. A partial release with no
parcels named is meaningless and is rejected, rather than silently releasing
nothing or everything.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
    CREATE TABLE {SCHEMA}.covenant_release (
      release_id            SERIAL PRIMARY KEY,
      covid                 INTEGER NOT NULL REFERENCES {SCHEMA}.covenant(covid),
      release_type          TEXT NOT NULL CHECK (release_type IN ('termination', 'buyout')),
      scope                 TEXT NOT NULL CHECK (scope IN ('covenant', 'partial')),
      -- The date the release takes effect, which is what fee liability turns on.
      -- Kept separate from recording_date on purpose: an instrument recorded in
      -- March can be effective from January, and it is the effective date that
      -- decides whether a February transfer still owed a fee.
      effective_date        DATE NOT NULL,
      recording_instrument  TEXT,
      recording_date        DATE,
      -- What was paid to buy the obligation out. Null for a termination, which
      -- ends the covenant without consideration passing.
      consideration_amount  NUMERIC(14,2),
      notes                 TEXT,
      source_id             INTEGER REFERENCES {SCHEMA}.source(source_id),
      created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """)
    op.execute(f"""
        CREATE INDEX covenant_release_covid_idx
        ON {SCHEMA}.covenant_release (covid, effective_date)
    """)
    op.execute(f"""
    CREATE TABLE {SCHEMA}.covenant_release_parcel (
      release_id   INTEGER NOT NULL REFERENCES {SCHEMA}.covenant_release(release_id) ON DELETE CASCADE,
      county_fips  CHAR(5) NOT NULL,
      apn          TEXT NOT NULL,
      PRIMARY KEY (release_id, county_fips, apn),
      FOREIGN KEY (county_fips, apn) REFERENCES {SCHEMA}.parcel(county_fips, apn)
    )
    """)
    op.execute(f"""
        CREATE INDEX covenant_release_parcel_apn_idx
        ON {SCHEMA}.covenant_release_parcel (county_fips, apn)
    """)

    # Two categories, not one: which reason ended the obligation is exactly what
    # an estoppel or payoff statement has to say, and collapsing them would make
    # a bought-out parcel indistinguishable from a terminated one on the record.
    op.execute(f"""
        INSERT INTO {SCHEMA}.exemption_category (category_code, label, description)
        VALUES
          ('post_termination', 'Post-termination',
           'Transfer recorded on or after the effective date of an instrument terminating this '
           'covenant as to the transferred land. Fees owed on earlier transfers are unaffected.'),
          ('post_buyout', 'Post-buyout',
           'Transfer recorded on or after the effective date of a buyout of the fee obligation '
           'for the transferred land. Fees owed on earlier transfers are unaffected.')
        ON CONFLICT (category_code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute(f"""
        DELETE FROM {SCHEMA}.exemption_category
        WHERE category_code IN ('post_termination', 'post_buyout')
          AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.transfer
                          WHERE exemption_category = exemption_category.category_code)
    """)
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.covenant_release_parcel")
    op.execute(f"DROP TABLE IF EXISTS {SCHEMA}.covenant_release")
