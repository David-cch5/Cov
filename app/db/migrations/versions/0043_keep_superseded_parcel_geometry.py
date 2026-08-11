"""a superseded parcel boundary is evidence, not garbage -- keep it

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-11

covenant.parcel_history has existed since 0001_initial_schema with exactly the
right shape for this -- captured_at, geom, acreage, owner_name_raw,
change_reason, superseded_by_apn -- and has never held a single row. Same story
as job_queue: designed, then never written to.

WHY IT MATTERS HERE, from covid 4956. Dallas's Tax_Parcels_2019 layer put 6,001
sq ft of land in one parcel that current Dallas CAD assigns to its neighbour:

    parcel                      2019 AREA_FEET    current    delta
    24123500010140000 (SSM)             37,154     43,155   +6,001
    24049800010010100 (CONLON)          45,401     39,400   -6,001

Read as a data-quality problem, the 2019 geometry is simply wrong and gets
replaced. Read properly, the DIFFERENCE IS THE RECORD of a 2017 conveyance
(INT201700012130) working its way into the parcel fabric -- which is precisely
what a system that tracks land through time wants to keep. A covenant runs with
the land; establishing which land was encumbered WHEN is the whole job, and a
boundary that moved is part of that history. Overwriting it destroys the only
evidence that it moved at all.

So parcel geometry is now versioned on change rather than replaced in place, and
the superseded layer stays registered and queryable rather than being swapped
out. Two columns support that:

  parcel.geometry_vintage        which layer the CURRENT geometry came from, so a
                                 consumer never has to guess whether it is looking
                                 at current or archival data. app/gis/adapters/
                                 dallas_tx.py already reports this per row; before
                                 now nothing persisted it.

  county_gis_registry.superseded_layers
                                 the layers a county USED to be read from, kept
                                 addressable. Not a comment in `quirks` -- a
                                 superseded layer is a source you may legitimately
                                 need to query again (it is still published), so it
                                 belongs in a field a query can reach.

Nothing is deleted by this migration, and nothing is backfilled by it either:
history only exists from the first change observed after it, plus whatever a
deliberate backfill script writes.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {SCHEMA}.parcel
          ADD COLUMN geometry_vintage TEXT
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.parcel.geometry_vintage IS
        'Which published layer this geometry came from -- ''current'' when read from the '
        'county''s live parcel service, otherwise a label for the archival layer (e.g. '
        '''2019''). Provenance, per CLAUDE.md''s rule that every datum carries its source: a '
        'consumer comparing acreage against a deed needs to know whether it is holding current '
        'or historical geometry. See migration 0043.'
    """)
    op.execute(f"""
        ALTER TABLE {SCHEMA}.county_gis_registry
          ADD COLUMN superseded_layers JSONB NOT NULL DEFAULT '[]'::jsonb
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.county_gis_registry.superseded_layers IS
        'Layers this county was previously read from, kept addressable rather than discarded: '
        '[{{"base_url": ..., "label": ..., "vintage": ..., "retired_at": ..., "why": ...}}]. '
        'A superseded layer is still published and still queryable, and the difference between '
        'it and the current one records how a boundary moved -- see migration 0043.'
    """)
    # captured_at is part of the natural key: one snapshot per parcel per observed
    # change. Without it a second change to the same parcel would either collide or
    # duplicate silently, and this table is meant to be an append-only record.
    op.execute(f"""
        CREATE UNIQUE INDEX parcel_history_snapshot_uniq
          ON {SCHEMA}.parcel_history (county_fips, apn, captured_at)
    """)
    op.execute(f"""
        CREATE INDEX parcel_history_apn_idx
          ON {SCHEMA}.parcel_history (county_fips, apn, captured_at DESC)
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.parcel_history_snapshot_uniq")
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.parcel_history_apn_idx")
    op.execute(f"ALTER TABLE {SCHEMA}.county_gis_registry DROP COLUMN IF EXISTS superseded_layers")
    op.execute(f"ALTER TABLE {SCHEMA}.parcel DROP COLUMN IF EXISTS geometry_vintage")
