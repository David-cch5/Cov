"""confirm covenant_template fee/interest percentages for V11 (spousal family)

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-26

Cross-checked directly against covid 2497's actual document text (Bexar,
0.452 ac, single-lot assemblage -- the small-acreage TX test case picked for
chain-of-title walking) while confirming which transfers owe the
Reconveyance Fee vs. which are exempt under V11:

  - Sec. 5 AMOUNT DUE: 1% of Gross Sales Price, due contemporaneous with
    Transfer of Title (a condition precedent -- no separate "days after"
    grace period, hence covenant.fee_due_days is correctly NULL here).
  - Sec. 8.t: unpaid fees bear interest at the lesser of the legal maximum
    or 18% per year -- identical to V01.
  - Sec. 8.k: Trustee's foreclosure-sale commission is 3% of the bid --
    identical to V01's trustee_fee_percent.
  - Sec. 12.b: Trustee retains 5% of collected fees in a separate escrow
    account -- identical to V01's escrow_reserve_percent.
  - Sec. 13.e: Closing Agent may withhold the greater of $100 or 2% of the
    fee collected -- identical to V01's closing_agent_fee_percent/minimum.

The exemption clauses themselves (covenant_template_exemption, already
seeded in 0001) were independently verified word-for-word against this same
document's own Section 6 -- exact match, no corrections needed. The one
real difference from the V01/V04 family: V11 has no affiliate_transaction
(Controlling Interest) carve-out at all -- it exempts spousal transfers
instead. A commonly-owned-entity transfer that would be exempt under V01
still owes the fee under V11 unless it independently qualifies under one of
V11's 10 listed exemptions.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
        UPDATE {SCHEMA}.covenant_template
        SET unpaid_interest_percent = 18.00,
            unpaid_interest_source = 'Section 8.t LIEN AND PRIORITY; LIABILITY; COLLECTION',
            standard_fee_percent = 1.00,
            trustee_fee_percent = 3.00,
            escrow_reserve_percent = 5.00,
            closing_agent_fee_percent = 2.00,
            closing_agent_fee_minimum = 100.00
        WHERE template_version_id = 'V11'
    """)


def downgrade() -> None:
    op.execute(f"""
        UPDATE {SCHEMA}.covenant_template
        SET unpaid_interest_percent = NULL, unpaid_interest_source = NULL,
            standard_fee_percent = NULL, trustee_fee_percent = NULL,
            escrow_reserve_percent = NULL, closing_agent_fee_percent = NULL,
            closing_agent_fee_minimum = NULL
        WHERE template_version_id = 'V11'
    """)
