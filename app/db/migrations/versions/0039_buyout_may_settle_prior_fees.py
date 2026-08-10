"""a buyout's consideration may settle fees already accrued; a termination cannot

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-10

0038 treated the two release types as differing only in WHY the obligation ended.
They also differ in what happens to fees already accrued, and the difference is
not symmetric:

  TERMINATION   a validly terminated covenant had no prior sales -- which is
                exactly what the Transylvania County instrument swears to
                ("neither the Released Property ... has been sold, conveyed or
                assigned since the date of filing of the Declaration"). If there
                were no conveyances there are no accrued fees, so there is nothing
                for a termination to settle. A termination found sitting over
                real prior fees is therefore a CONTRADICTION -- either the
                termination is not valid as to that land or the fee record is
                wrong -- and belongs in front of a human, not silently applied.

  BUYOUT        the consideration MAY include fees accrued before it, depending on
                the agreement. When it does, those fees are satisfied by the
                buyout rather than separately; when it does not, they remain owed.
                Only the agreement says which, so this cannot be inferred.

settles_prior_fees records that choice on the release. fee_collection.
settled_by_release_id records which rows it actually satisfied -- a link, not a
deletion, so the history the user needs stays intact: the row still shows the fee
was owed, what it was owed on, and now also how it was discharged.

WHY NOT JUST MARK THEM 'waived'
'waived' already exists in fee_collection.status and means someone chose to forgo
a fee that remained owed. A fee discharged by a buyout was not forgone -- it was
paid, as part of a larger consideration. Recording it as waived would understate
what was actually collected and lose the connection to the instrument that
collected it.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          ADD COLUMN settles_prior_fees BOOLEAN NOT NULL DEFAULT false,
          ADD COLUMN settlement_note TEXT
    """)
    # Only a buyout pays for anything. A termination settling prior fees is a
    # category error, and one the database itself should refuse rather than rely
    # on a caller remembering.
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          ADD CONSTRAINT covenant_release_only_buyout_settles
          CHECK (NOT settles_prior_fees OR release_type = 'buyout')
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.covenant_release.settles_prior_fees IS
        'The buyout consideration includes fees accrued before it, so those fees are '
        'discharged by this instrument. Depends entirely on the agreement and cannot be '
        'inferred. Never true for a termination -- see migration 0039.'
    """)

    op.execute(f"""
        ALTER TABLE {SCHEMA}.fee_collection
          ADD COLUMN settled_by_release_id INTEGER
              REFERENCES {SCHEMA}.covenant_release(release_id)
    """)
    op.execute(f"""
        CREATE INDEX fee_collection_settled_by_release_idx
        ON {SCHEMA}.fee_collection (settled_by_release_id)
        WHERE settled_by_release_id IS NOT NULL
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.fee_collection.settled_by_release_id IS
        'The buyout that discharged this fee. A LINK, never a deletion: the row still '
        'records that the fee was owed and on what, and now also how it was discharged. '
        'Distinct from status=waived, which means a still-owed fee was forgone.'
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.fee_collection_settled_by_release_idx")
    op.execute(f"ALTER TABLE {SCHEMA}.fee_collection DROP COLUMN IF EXISTS settled_by_release_id")
    op.execute(f"ALTER TABLE {SCHEMA}.covenant_release "
               f"DROP CONSTRAINT IF EXISTS covenant_release_only_buyout_settles")
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          DROP COLUMN IF EXISTS settles_prior_fees,
          DROP COLUMN IF EXISTS settlement_note
    """)
