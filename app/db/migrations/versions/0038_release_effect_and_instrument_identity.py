"""a release can be prospective OR void ab initio -- read from two real instruments

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-10

0037 and 330fa86 modelled a release as necessarily PROSPECTIVE, on the covenant's
own rule that a termination takes effect after it is recorded, and treated an
earlier effective date as probably a transcription error. Two real recorded
instruments show that is only half of it.

PROSPECTIVE -- Williamson County TX, 2019003560, recorded 2019-01-15
    "Larry A. Richardson, Trustee ... hereby terminates that one certain
     Declaration of Covenant signed on November 12, 2009 and recorded in
     Document No. 2009082853"
No retroactive language. Ends the covenant going forward.

VOID AB INITIO -- Transylvania County NC, 2010004621, recorded 2010-09-16
    "The Instrument shall be terminated and declared to be null and void, IN THE
     SAME MANNER AS IF IT HAD NEVER BEEN RECORDED ... The Instrument was not
     authorized by FBP to be executed or recorded, and has never constituted a
     lawful restriction upon the property"
This reaches back to inception. It is not an error and not rare enough to treat
as one -- it is a drafted form with its own name for the land ("Released
Property").

WHAT MAKES VOID AB INITIO SAFE, AND IS ITSELF RECORDED
The same NC instrument carries a sworn statement that there is nothing to claw
back: the Managers "swear and affirm upon personal knowledge that neither the
Released Property, nor Declarant's Beneficial Interest, nor a Controlling
Interest in Declarant, has been sold, conveyed or assigned since the date of
filing of the Declaration." No intervening conveyance means no accrued fee to
void. That affidavit is the precondition for reaching back, so it is captured as
a column rather than buried in notes -- a void-ab-initio release WITHOUT it, over
a period that does contain transfers, is a conflict for a human, not something to
apply silently.

OTHER FACTS BOTH INSTRUMENTS CARRY THAT HAD NOWHERE TO GO
  execution_date          Williamson: executed Jan 3, recorded Jan 15. NC: made
                          as of Sept 10, notarised Sept 13, recorded Sept 16.
                          Three distinct dates, and only one was stored.
  terminates_instrument   the covenant is identified by ITS OWN recording
                          (Doc. 2009082853; Book 529 Page 410), not by covid
  referenced_instruments  Williamson names three later documents the covenant was
                          "referenced in" (2012005120, 2015005262, 2018004487) --
                          amendments or assignments that a chain walk needs
  acknowledged_date       the Williamson termination is only "fully effective"
                          once the Trustee acknowledges it, and in the copy on
                          hand that acknowledgement is UNEXECUTED -- blank day,
                          no signature. A pending acknowledgement is a real state
                          and must not read as an effective termination.
  terminated_under        both cite Paragraph 25 as the declarant's own means of
                          terminating, which is the same paragraph these deeds
                          carve out of their SAVE AND EXCEPT clause
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          ADD COLUMN effect TEXT NOT NULL DEFAULT 'prospective'
              CHECK (effect IN ('prospective', 'void_ab_initio')),
          ADD COLUMN execution_date DATE,
          ADD COLUMN acknowledged_date DATE,
          ADD COLUMN acknowledgement_required BOOLEAN NOT NULL DEFAULT false,
          ADD COLUMN no_intervening_conveyance_affidavit BOOLEAN NOT NULL DEFAULT false,
          ADD COLUMN terminates_instrument TEXT,
          ADD COLUMN referenced_instruments TEXT[],
          ADD COLUMN terminated_under TEXT
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.covenant_release.effect IS
        'prospective: ends the obligation from the effective date forward. '
        'void_ab_initio: the covenant is void as if never recorded, reaching back to '
        'its inception -- real, recorded language, not an error. See migration 0038.'
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.covenant_release.no_intervening_conveyance_affidavit IS
        'The instrument swears no conveyance occurred since the covenant was filed, so '
        'no accrued fee exists to void. This is what licenses effect=void_ab_initio; '
        'without it, a retroactive release over a period containing transfers is a '
        'conflict for human review, never applied silently.'
    """)
    # A void-ab-initio release needs no effective_date reasoning at all -- it
    # reaches everything -- but the column stays NOT NULL, so the constraint below
    # keeps the two shapes honest rather than letting a retroactive release carry a
    # date that means nothing.
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          ADD CONSTRAINT covenant_release_prospective_not_before_recording
          CHECK (effect = 'void_ab_initio'
                 OR recording_date IS NULL
                 OR effective_date >= recording_date)
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.covenant_release "
               f"DROP CONSTRAINT IF EXISTS covenant_release_prospective_not_before_recording")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          DROP COLUMN IF EXISTS effect,
          DROP COLUMN IF EXISTS execution_date,
          DROP COLUMN IF EXISTS acknowledged_date,
          DROP COLUMN IF EXISTS acknowledgement_required,
          DROP COLUMN IF EXISTS no_intervening_conveyance_affidavit,
          DROP COLUMN IF EXISTS terminates_instrument,
          DROP COLUMN IF EXISTS referenced_instruments,
          DROP COLUMN IF EXISTS terminated_under
    """)
