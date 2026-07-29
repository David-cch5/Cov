"""record Montgomery CAD's own Deed History table in county_gis_registry.quirks

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-29

Found while chain-walking covid 8245 (Montgomery): the covenant's own
declarant name ("ANANTA LLC") doesn't match the actual grantor on the real
conveyances ("ANANTA PARTNERS, LLC", a related-but-distinct entity), which
broke the recorder-portal name-walk entirely. Montgomery CAD's own website
(mcad-tx.org) exposes a per-property "Deed History" table indexed by APN/
account number -- confirmed live to give a complete 14-hop chain of title
back to 1996 for a real parcel (APN 41116) that the name-walk couldn't
reconstruct at all, and to self-correct: one instrument had been mistakenly
associated with the wrong parcel and is marked "DELETED" in MCAD's own
records rather than left silently wrong (app/title/mcad_deed_history.py
excludes these entirely).

Unlike Bexar's Harris Govern PACS deed history (migration 0019) and Douglas
County CO's assessor sales-data (migration 0022), this is NOT a plain
unauthenticated JSON GET -- no public REST API was found after directly
testing for one (intercepting both window.fetch and
XMLHttpRequest.prototype.open around the real search/detail-page load), so
this is fetched via a Playwright-rendered page (app/title/
mcad_deed_history.py), the same as the recorder-portal adapters. Stored
under quirks rather than a new column for the same reason as migration
0019: a different vendor/shape than the ArcGIS service this table
otherwise describes, and only confirmed for this one county so far. Texas
is a non-disclosure state: no consideration amount here either, same as
Bexar's CAD deed history.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

NOTE = {
    "mcad_deed_history_url": "https://mcad-tx.org",
    "mcad_deed_history_vendor": "mcad_ag_grid_spa",
    "mcad_deed_history_note": "Playwright-rendered React SPA (AG-Grid), not a REST API -- "
        "app/title/mcad_deed_history.py's fetch_deed_history(context, base_url, apn) navigates "
        "to /property-search, searches by APN/account number, and scrapes the property detail "
        "page's own Deed History table (deed_date, deed_type, description, grantor, grantee, "
        "book, volume, page, instrument). A 'DELETED' deed_type is MCAD's own record of an "
        "instrument it has determined does NOT actually apply to this parcel -- excluded "
        "entirely by the fetch/walk, not surfaced as ambiguous. Confirmed live: a far more "
        "complete and reliable chain-of-title source than the county clerk's recorder portal "
        "(app/recorder/adapters/publicsearch.py) for a covenant whose declarant name doesn't "
        "match its actual grantor name on record.",
}


def upgrade() -> None:
    op.execute(
        sa.text(f"""
            UPDATE {SCHEMA}.county_gis_registry
            SET quirks = COALESCE(quirks, '{{}}'::jsonb) || (:note)::jsonb
            WHERE county_fips = '48339'
        """).bindparams(note=json.dumps(NOTE))
    )


def downgrade() -> None:
    op.execute(f"""
        UPDATE {SCHEMA}.county_gis_registry
        SET quirks = quirks - 'mcad_deed_history_url' - 'mcad_deed_history_vendor' - 'mcad_deed_history_note'
        WHERE county_fips = '48339'
    """)
