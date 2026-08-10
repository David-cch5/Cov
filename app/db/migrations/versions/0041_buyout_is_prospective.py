"""a buyout stops FUTURE collection -- it cannot be void ab initio

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-10

Per direction: a buyout is negotiated to stop future collections. After its
effective date the parcels no longer collect fees. That is the whole point of the
instrument, and it makes a buyout inherently PROSPECTIVE.

0038 gave both release types the same two effects, which let a buyout be recorded
as void_ab_initio. That is not a shape a buyout has. Reaching back to inception
belongs to a TERMINATION, where the covenant is declared never to have been a
lawful restriction at all -- the Transylvania County instrument's "null and void,
in the same manner as if it had never been recorded." Nobody negotiates and pays
for that; they pay to stop what would otherwise keep accruing.

    termination   may be prospective OR void ab initio
    buyout        prospective only

WHAT settles_prior_fees IS, AND IS NOT
It is NOT how a buyout stops collection -- that is automatic, prospective and needs
no flag. It records the separate, optional payment term that the negotiated
consideration also covered a specific fee already outstanding. A buyout with no
such term still stops future collection exactly the same way; it simply leaves any
existing unpaid balance where it was. 0039's column and settle_prior_fees() keep
that narrower meaning, now stated as such.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_release
          ADD CONSTRAINT covenant_release_buyout_is_prospective
          CHECK (release_type <> 'buyout' OR effect = 'prospective')
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.covenant_release.effect IS
        'prospective: ends the obligation from the effective date forward. A BUYOUT is '
        'always this -- it is negotiated to stop future collection. void_ab_initio: the '
        'covenant is void as if never recorded, reaching back to inception; a TERMINATION '
        'shape only. See migrations 0038 and 0041.'
    """)
    op.execute(f"""
        COMMENT ON COLUMN {SCHEMA}.covenant_release.settles_prior_fees IS
        'A separate, optional payment term: the negotiated consideration also covered a fee '
        'already outstanding. NOT how a buyout stops collection -- that is automatic and '
        'prospective. A buyout without this term still stops future fees; it just leaves any '
        'existing unpaid balance in place. Never true for a termination. See migration 0041.'
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.covenant_release "
               f"DROP CONSTRAINT IF EXISTS covenant_release_buyout_is_prospective")
