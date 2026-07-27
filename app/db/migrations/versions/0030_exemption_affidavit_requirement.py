"""add requires_grantor_affidavit to covenant_template_exemption

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-26

Found during a pre-commit review of real covenant text across template
families (not previously read this closely for V02/V03/V12): those three
templates' own Section 6 EXEMPTIONS clause ends with a sentence none of
the other families checked (V01, V06, V08, V11, V13, V18) have --
confirmed directly in each sample covenant's own text (covids 7990, 9147,
8071 respectively, identical wording in all three):

  "Exemptions pursuant to section 6(c), 6(d), 6(f) or 6(h) shall be
  supported by Grantor's written affidavit under oath that the foregoing
  exemption(s) apply, which shall be filed in the OPR in connection with
  the Conveyance."

That is: for these three templates specifically, death_probate (c),
foreclosure (d), affiliate_transaction (f), and trustee_unidentified (h)
are not self-executing exemptions the way they are elsewhere -- they only
actually apply if a supporting affidavit was filed. A recorder/CAD index
alone (what app/title/chain.py's classifiers work from) has no way to
confirm an affidavit was filed, so a transfer that otherwise looks
foreclosure-shaped under one of these three templates cannot be
auto-confirmed exempt with the same confidence as elsewhere -- it needs a
human (or a future document-image read) to confirm the affidavit exists.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

AFFIDAVIT_GATED_TEMPLATES = ("V02", "V03", "V12")
AFFIDAVIT_GATED_CATEGORIES = ("death_probate", "foreclosure", "affiliate_transaction", "trustee_unidentified")


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {SCHEMA}.covenant_template_exemption
        ADD COLUMN requires_grantor_affidavit BOOLEAN NOT NULL DEFAULT false
    """)
    op.execute(f"""
        UPDATE {SCHEMA}.covenant_template_exemption
        SET requires_grantor_affidavit = true
        WHERE template_version_id IN {AFFIDAVIT_GATED_TEMPLATES}
          AND category_code IN {AFFIDAVIT_GATED_CATEGORIES}
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.covenant_template_exemption DROP COLUMN requires_grantor_affidavit")
