"""seed county_recorder_registry for Montgomery County

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-26

Montgomery is the primary target of the Section 7 cost probe (BUILD_SPEC.md)
but had no county_recorder_registry entry -- a gap found during a build
status review, since GIS classification for Montgomery was already wired up
(county_gis_registry, migration 0006-ish) but deed/chain-of-title access
was not.

BUILD_SPEC.md's own pipeline section already documented this portal in
detail (montgomery.tx.publicsearch.us -- a GovOS/Kofile "PublicSearch"
product, results assembled client-side with no replayable JSON endpoint,
Playwright-required). Confirmed directly against the live portal:
app.recorder.adapters.publicsearch works completely unmodified -- same
#basicSearchInputBox / #withOcr / dynamic-header results table as
Denton/Nueces/Collin/Bexar. Verified with two live queries: a name search
("AVALON HARBOR", 50 rows) and an exact document-number lookup
(2009089679 -- the DECLARATION for covid 4780, AVALON HARBOR II LP,
19.872 AC, JOHN CORNER A8) that returned the correct single row.

Montgomery's results table includes HIGH LOT/LOW LOT/BLOCK/SUBDIVISION/
ACREAGE/COMMENT columns that Denton/Nueces/Collin don't expose (closer to
Bexar's shape) -- already handled by the adapter's per-county dynamic
header mapping, no code change needed.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


def upgrade() -> None:
    table = sa.table(
        "county_recorder_registry",
        sa.column("county_fips", sa.Text), sa.column("access_tier", sa.Text),
        sa.column("base_url", sa.Text), sa.column("auth_notes", sa.Text),
        sa.column("workers_allowed", sa.SmallInteger), sa.column("quirks", sa.Text),
        sa.column("status", sa.Text),
        sa.column("discovered_at", sa.DateTime), sa.column("last_verified_at", sa.DateTime),
        schema=SCHEMA,
    )

    quirks = {
        "vendor": "govos_publicsearch",
        "adapter": "app.recorder.adapters.publicsearch",
        "confirmed_live": "search_by_name('AVALON HARBOR') returned 50 rows; "
            "search_by_document_number('2009089679') returned the exact "
            "DECLARATION doc for covid 4780 (AVALON HARBOR II LP, 19.872 AC, "
            "JOHN CORNER A8) -- adapter required no changes.",
        "results_columns": "includes HIGH LOT/LOW LOT/BLOCK/SUBDIVISION/ACREAGE/"
            "COMMENT (closer to Bexar's column set than Denton/Nueces/Collin's) "
            "-- already handled by the adapter's dynamic header mapping.",
        "note": "this is the primary county for the BUILD_SPEC.md Section 7 "
            "cost probe -- see the pipeline section's detailed Montgomery notes "
            "(no replayable JSON endpoint; Playwright rendering required).",
    }

    op.execute(
        table.insert().values(
            county_fips="48339", access_tier="portal_playwright",
            base_url="https://montgomery.tx.publicsearch.us",
            auth_notes="Guest access to search, no login required. Full-text OCR "
                       "search available via the 'Search Index & Full Text (OCR)' checkbox.",
            workers_allowed=1,
            quirks=sa.text("(:q)::jsonb").bindparams(q=json.dumps(quirks)),
            status="active", discovered_at=sa.func.now(), last_verified_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.county_recorder_registry WHERE county_fips = '48339'")
