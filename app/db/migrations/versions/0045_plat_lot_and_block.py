"""a plat that created ONE lot, identified by the lot it created

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-11

Not every plat is a phase. A recorder's plat index carries two different kinds of
filing, and this project could only key on one of them:

  a filing that platted a PHASE      "PALMILLA BEACH P U D Unit: 6A", 2022-11-09
  a filing that replatted ONE LOT    "PALMILLA BEACH Lot: 14C Block: 3", 2018-06-12

The second kind is exactly as real a formation event as the first -- it is the
instrument that brought lot 14C of block 3 into existence -- but plat's identity
was (county_fips, subdivision_name, section), so a single-lot replat had no
section to be keyed on and nowhere to record WHICH lot it created. Nueces alone
publishes a dozen of them for one subdivision, several on dates years apart, and
they were being collapsed into one sectionless row or dropped.

So: lot and block, and they join the uniqueness key. '' rather than NULL, the same
convention `section` already uses here, because a unique constraint treats two
NULLs as distinct and would happily store the same plat twice.

This also makes the parcel side answerable. A Nueces parcel recites its own lot
and block plainly -- "PALMILLA BEACH P.U.D. UNIT 7 BLK 2 LOT 9" -- so a parcel can
now be matched to the filing that names its exact lot, which is the only way to
date a lot in a subdivision whose parcels recite no phase at all.

Not applied to plat_history or anything downstream: nothing else keys on a plat's
lot, and a column nobody reads is a liability.
"""
from alembic import op

from app.config import DB_SCHEMA as SCHEMA

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {SCHEMA}.plat
            ADD COLUMN lot   text NOT NULL DEFAULT '',
            ADD COLUMN block text NOT NULL DEFAULT ''
    """)
    # Widen the identity. Dropped and recreated rather than added alongside: two
    # unique constraints would both have to be satisfied, and the narrow one would
    # reject the second single-lot replat of the same subdivision -- the exact case
    # this migration exists to allow.
    op.execute(f"ALTER TABLE {SCHEMA}.plat DROP CONSTRAINT plat_county_fips_subdivision_name_section_key")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.plat ADD CONSTRAINT plat_county_fips_subdivision_name_section_lot_block_key
            UNIQUE (county_fips, subdivision_name, section, lot, block)
    """)
    # A lot without the block it sits in cannot be matched against a parcel: block
    # numbering restarts per subdivision, so "LOT 9" alone names several parcels.
    op.execute(f"""
        ALTER TABLE {SCHEMA}.plat ADD CONSTRAINT plat_lot_needs_block
            CHECK (lot = '' OR block <> '')
    """)
    op.execute(f"CREATE INDEX plat_lot_block_idx ON {SCHEMA}.plat (county_fips, lot, block) WHERE lot <> ''")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.plat_lot_block_idx")
    op.execute(f"ALTER TABLE {SCHEMA}.plat DROP CONSTRAINT IF EXISTS plat_lot_needs_block")
    op.execute(f"ALTER TABLE {SCHEMA}.plat DROP CONSTRAINT IF EXISTS "
               "plat_county_fips_subdivision_name_section_lot_block_key")
    # Deduplicate first or the narrow constraint cannot be restored: rows that
    # differ only by lot/block become identical once those columns are gone.
    op.execute(f"""
        DELETE FROM {SCHEMA}.plat p USING {SCHEMA}.plat q
         WHERE p.plat_id > q.plat_id
           AND p.county_fips = q.county_fips
           AND p.subdivision_name = q.subdivision_name
           AND p.section = q.section
    """)
    op.execute(f"ALTER TABLE {SCHEMA}.plat DROP COLUMN lot, DROP COLUMN block")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.plat ADD CONSTRAINT plat_county_fips_subdivision_name_section_key
            UNIQUE (county_fips, subdivision_name, section)
    """)
