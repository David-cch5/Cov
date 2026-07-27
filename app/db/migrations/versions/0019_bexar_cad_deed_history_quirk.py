"""record Bexar's CAD deed-history API in county_gis_registry.quirks

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-26

Found while chain-walking covid 2497 (Bexar): a recorder-portal name-walk
(app/title/chain.py) found the declarant's 2017 sale but not what happened
next, leaving the walk's last known holder mismatched against the parcel's
current owner of record. Checking BCAD's own website (not the ArcGIS REST
layer this registry otherwise points at) surfaced a completely different,
better system: a "Harris Govern" CAD front-end
(hgo.harrisgovern.com/bexar/...) with its own deed-history table per
property, exposed as a plain unauthenticated JSON GET -- confirmed via curl
(see app/title/cad_deed_history.py). It revealed a foreclosure and a
subsequent resale that no recorder-portal search (address text, or a
per-grantee name walk) ever surfaced.

Stored under quirks rather than a new column: this is a different *vendor*
than the ArcGIS service this table otherwise describes, and only confirmed
for this one county so far -- not worth a schema commitment until it's
seen elsewhere. "Harris Govern" is a CAD software vendor (unrelated to
Harris County, TX, despite the name) used by multiple Texas CADs, so other
counties may expose the same shape -- unverified.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

NOTE = {
    "cad_deed_history_url": "https://hgo.harrisgovern.com/bexar",
    "cad_deed_history_vendor": "harris_govern_pacs",
    "cad_deed_history_note": "plain unauthenticated GET .../api/property/property-details/"
        "property-deed-history?propertyId=<apn> -- confirmed via curl; propertyId is this "
        "county's own APN, no separate lookup needed. Returns every recorded deed for the "
        "property (deed_dt, deed_type_cd, deed_type_desc, grantor, grantee, deed_num) -- a "
        "far more complete and reliable chain-of-title source than reconstructing it from "
        "the county clerk's recorder portal (app/recorder/adapters/publicsearch.py), which "
        "missed a foreclosure and a subsequent resale for covid 2497 that this API surfaced "
        "directly.",
}


def upgrade() -> None:
    op.execute(
        sa.text(f"""
            UPDATE {SCHEMA}.county_gis_registry
            SET quirks = COALESCE(quirks, '{{}}'::jsonb) || (:note)::jsonb
            WHERE county_fips = '48029'
        """).bindparams(note=json.dumps(NOTE))
    )


def downgrade() -> None:
    op.execute(f"""
        UPDATE {SCHEMA}.county_gis_registry
        SET quirks = quirks - 'cad_deed_history_url' - 'cad_deed_history_vendor' - 'cad_deed_history_note'
        WHERE county_fips = '48029'
    """)
