"""a discovered termination is a FOUND DOCUMENT, not a release, until adjudicated

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-10

Per direction: every covenant that will be ingested is VALID AS OF TODAY. A
termination turning up in the public records does not change that by itself --
some of them are invalid, and an invalid termination is answered by recording a
rescission that voids it, not by treating the covenant as over.

So finding a termination is a document-acquisition event, not a release event. The
work splits into three stages, and only the last one has any fee effect:

  1. FIND      download the instrument and record that it exists
  2. ADJUDICATE decide whether it is valid -- a separate process, human-led
  3. ACT        if valid, the covenant is released; if invalid, generate and record
                a rescission voiding it

0037-0039 modelled stage 3 only, and modelled it as the default: calling
record_release asserted the release had happened, and everything downstream --
fee exemption, is_fully_released, the skip-research rule -- believed it
immediately. That is the wrong default now. A found termination that is later held
invalid would have silently stopped fee collection on a live covenant in the
meantime, which is the expensive direction to be wrong in.

validity_status makes the stage explicit and defaults to 'pending_review'. NOTHING
releases on a pending or invalid release: not fee exemption, not settlement, not
the historic/skip-research rule. They all require 'valid'. Recording a discovered
termination is therefore safe by construction -- it captures the fact without
asserting its consequence.

rescission_instrument / rescission_recording_date record the answer to an invalid
one. Kept on this row rather than in a separate table because a rescission exists
only in reference to the termination it voids, and reading one without the other
tells you nothing.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          ADD COLUMN validity_status TEXT NOT NULL DEFAULT 'pending_review'
              CHECK (validity_status IN ('pending_review', 'valid', 'invalid')),
          ADD COLUMN validity_note TEXT,
          ADD COLUMN adjudicated_at TIMESTAMPTZ,
          ADD COLUMN rescission_instrument TEXT,
          ADD COLUMN rescission_recording_date DATE
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.covenant_release.validity_status IS
        'pending_review (default): the instrument was found and downloaded, and nothing '
        'follows from it yet. valid: adjudicated as effective -- only then does it release '
        'anything. invalid: held ineffective, and answered by a rescission. Every consumer '
        'requires valid; see migration 0040.'
    """)
    # A rescission answers an INVALID termination. Recording one against a release
    # held valid, or still pending, is a contradiction the database should catch.
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          ADD CONSTRAINT covenant_release_rescission_only_when_invalid
          CHECK (rescission_instrument IS NULL OR validity_status = 'invalid')
    """)
    # An adjudicated release should say when. Left permissive for 'pending_review',
    # which by definition has not been adjudicated yet.
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          ADD CONSTRAINT covenant_release_adjudicated_has_timestamp
          CHECK (validity_status = 'pending_review' OR adjudicated_at IS NOT NULL)
    """)

    # Existing rows were written before this distinction existed, under the old
    # assumption that recording a release meant it applied. They are the two real
    # instruments read into the model, both adjudicated by reading them, so they
    # are marked valid rather than silently demoted to pending -- but only because
    # there are exactly two and both were examined. A larger backfill would go to
    # pending_review instead.
    op.execute(f"""
        UPDATE {SCHEMA}.covenant_release
        SET validity_status = 'valid', adjudicated_at = now(),
            validity_note = 'pre-0040 row: recorded when the model had no validity stage'
        WHERE validity_status = 'pending_review'
    """)


def downgrade() -> None:
    for constraint in ("covenant_release_rescission_only_when_invalid",
                       "covenant_release_adjudicated_has_timestamp"):
        op.execute(f"ALTER TABLE {SCHEMA}.covenant_release DROP CONSTRAINT IF EXISTS {constraint}")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          DROP COLUMN IF EXISTS validity_status,
          DROP COLUMN IF EXISTS validity_note,
          DROP COLUMN IF EXISTS adjudicated_at,
          DROP COLUMN IF EXISTS rescission_instrument,
          DROP COLUMN IF EXISTS rescission_recording_date
    """)
