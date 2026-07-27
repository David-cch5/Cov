"""add transfer.consideration_source_id -- actual vs. estimated price provenance

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-26

Stakeholder decision: build actual-price extraction for disclosure states
(the deed's own stated consideration, or an assessor's sales-history page
when it lists one) since that's a real, calculable number -- but not
non-disclosure-state estimation (deed-of-trust/assessor-market-value
inference, or a future Zillow/PropStream/PropertyRadar integration), which
the stakeholder is pursuing as its own separate project.

This needed a way to tell "actual" from "estimated" per the stakeholder's
own request, without duplicating what source.is_estimated + source_type
already express (assessor_api/recorder_api/recorder_portal + is_estimated
=false for an actual disclosed price; estimate_derivation + is_estimated
=true for the existing price_estimate table's non-disclosure path) --
transfer just had no column linking consideration_amount to ITS OWN
source, distinct from recorder_source_id (the provenance of the transfer
record itself: which recorder/CAD index found this grantor/grantee/date).
A disclosure-state deed's stated price can come from a different source
than the one that found the conveyance (e.g. the index finds the deed, a
separate OCR/vision-read of that deed's image is what actually yields the
dollar figure) -- hence a dedicated FK rather than reusing
recorder_source_id.

No code populates this yet: no disclosure-state covenant is in the
current TX-only probe scope (Montgomery + a small TX sample per
CLAUDE.md's guardrail) to build and test the extraction pipeline against.
This just keeps the schema ready, per BUILD_SPEC.md's extensibility rule.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE {SCHEMA}.transfer
        ADD COLUMN consideration_source_id BIGINT REFERENCES {SCHEMA}.source(source_id)
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.transfer DROP COLUMN consideration_source_id")
