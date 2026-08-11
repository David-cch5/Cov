"""when each parcel was FORMED, and what event moved a boundary

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-11

0043 started keeping superseded parcel geometry. It keyed each snapshot on
captured_at -- when this project OBSERVED a change -- which is honest about
provenance and nearly useless as land history. Dallas's 6,001 sq ft boundary
move carries captured_at 2020-07-01, the date the GIS layer was last edited. The
event that actually moved the line was a conveyance recorded 2017-01-09,
instrument INT201700012130. Three years apart, and only one of them is a fact
about the land.

The existing covid 4440 platting map already does this right and is the model
here: every lot flips from raw to platted at its own real recorded plat date --
Harrington Trails Section 1 at 2020-03-25, instrument 2020029214 -- and a parcel
with no real date NEVER flips, whatever the time slider says. That rule is the
whole point, and it needs columns to hang on.

PARCEL FORMATION
A parcel comes into existence when an instrument creates it: a plat that
subdivides raw acreage into lots, or a deed that splits a tract. So:

  formed_date            when the parcel came into existence
  formed_by_instrument   the recorded instrument that created it
  formation_source       'plat' | 'deed' | NULL -- WHICH KIND of evidence, so a
                         consumer can tell a plat-derived date from a deed-derived
                         one without re-deriving it

Derivable today for the 4,713 parcels that already carry plat_id: their plat's
own recording_date and recording_instrument. The remaining ~3,000 are raw
abstract-survey tracts and unresolved references, and their formation stays NULL.
Never inferred from first-seen date, appraisal year, or anything else this
project happened to notice -- an unplatted tract has no formation event to read,
and inventing one would put a fabricated date under a fee calculation.

HISTORY EVENTS
parcel_history gains the same distinction:

  effective_date         when the change happened on the ground
  instrument             the recorded instrument that caused it

captured_at stays, and stays the natural key -- it is when we saw the change, and
two different observations are two different rows. effective_date is nullable on
purpose: a snapshot taken because a county republished its fabric genuinely has
no known instrument until someone reads the deed, and NULL says that honestly
where a copied captured_at would be a quiet lie.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {SCHEMA}.parcel
          ADD COLUMN formed_date DATE,
          ADD COLUMN formed_by_instrument TEXT,
          ADD COLUMN formation_source TEXT
              CHECK (formation_source IS NULL OR formation_source IN ('plat', 'deed'))
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.parcel.formed_date IS
        'When this parcel came into existence, from the instrument that created it -- a plat '
        'that subdivided raw acreage, or a deed that split a tract. NULL for raw '
        'abstract-survey tracts with no formation event to read. NEVER inferred from a '
        'first-seen date or an appraisal year: a fabricated formation date under a fee '
        'calculation is worse than none. See migration 0044.'
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.parcel.formation_source IS
        'plat: read from the recorded subdivision plat this parcel belongs to. deed: read from '
        'a recorded conveyance that split the parent tract. NULL: not established. Says WHICH '
        'KIND of evidence stands behind formed_date without re-deriving it.'
    """)
    # A formation date must have an instrument behind it, and vice versa -- half a
    # citation is how a date ends up being trusted with nothing supporting it.
    op.execute(f"""
        ALTER TABLE {SCHEMA}.parcel
          ADD CONSTRAINT parcel_formation_is_cited
          CHECK ((formed_date IS NULL AND formed_by_instrument IS NULL AND formation_source IS NULL)
                 OR (formed_date IS NOT NULL AND formed_by_instrument IS NOT NULL
                     AND formation_source IS NOT NULL))
    """)

    op.execute(f"""
        ALTER TABLE {SCHEMA}.parcel_history
          ADD COLUMN effective_date DATE,
          ADD COLUMN instrument TEXT
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.parcel_history.effective_date IS
        'When the change happened ON THE GROUND, from the instrument that caused it -- as '
        'opposed to captured_at, which is when this project observed it. Dallas''s 6,001 sq ft '
        'boundary move was captured at 2020-07-01 (the GIS layer''s edit date) and effective '
        '2017-01-09 (the conveyance). Nullable: a snapshot taken because a county republished '
        'its fabric has no known instrument until someone reads the deed, and NULL says so '
        'where a copied captured_at would be a quiet lie. See migration 0044.'
    """)
    op.execute(f"""
        CREATE INDEX parcel_history_effective_idx
          ON {SCHEMA}.parcel_history (county_fips, apn, effective_date)
          WHERE effective_date IS NOT NULL
    """)
    op.execute(f"""
        CREATE INDEX parcel_formed_idx ON {SCHEMA}.parcel (county_fips, formed_date)
          WHERE formed_date IS NOT NULL
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.parcel_formed_idx")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.parcel_history_effective_idx")
    op.execute(f"ALTER TABLE {SCHEMA}.parcel_history "
               f"DROP COLUMN IF EXISTS effective_date, DROP COLUMN IF EXISTS instrument")
    op.execute(f"ALTER TABLE {SCHEMA}.parcel "
               f"DROP CONSTRAINT IF EXISTS parcel_formation_is_cited")
    op.execute(f"ALTER TABLE {SCHEMA}.parcel DROP COLUMN IF EXISTS formed_date, "
               f"DROP COLUMN IF EXISTS formed_by_instrument, DROP COLUMN IF EXISTS formation_source")
