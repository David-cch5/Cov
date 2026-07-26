"""seed county_gis_registry for Kerr County TX

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-24

Endpoint discovered while investigating why covid 7768's Exhibit A was never
attached: Kerr County Clerk's own AVA/Fidlar record index (ava.fidlar.com/
TXKerr) showed the same document (09-7803, Book 1765 Page 243) as 17 pages
long -- our local copy has only 15 -- with the legal description given
directly in the index: "LT 1 BLK 1 GALLERY PROJECT (0.428 ACS)". Matched
cleanly against KerrCADWebService (services6.arcgis.com/j94FvPaik4etwHFk,
owner "bisconsulting" -- the same organization ID already confirmed official
for Nueces, evidently the same hosting vendor).
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

FIELD_MAPPING = {
    "apn": "prop_id", "owner_name": "file_as_name", "legal_desc": "legal_desc",
    "legal_desc2": "legal_desc2", "legal_desc3": "legal_desc3",
    "acreage": "legal_acreage", "abstract_subdivision_code": "abs_subdv_cd", "block": "block",
}
QUIRKS = {
    "source_discovery": "service name 'KerrCADWebService', owner account 'bisconsulting', same "
        "organization ID (services6.arcgis.com/j94FvPaik4etwHFk) already confirmed official for Nueces",
    "block_field_reliably_populated": "unlike Llano/Nueces/Hunt, this county's 'block' column was "
        "populated in the one match tested -- still text-matched via legal_desc for the lot itself, "
        "since tract_or_lot is not reliably populated the same way",
    "resolved_missing_exhibit_case": "covid 7768's Exhibit A was never attached to the recorded "
        "instrument (verified directly against page images) -- the legal description came instead from "
        "the Kerr County Clerk's own AVA/Fidlar record index for the same document, which also revealed "
        "our local PDF copy is missing 2 of the recording's 17 pages.",
}


def upgrade() -> None:
    table = sa.table(
        "county_gis_registry",
        sa.column("county_fips", sa.Text), sa.column("base_url", sa.Text),
        sa.column("service_type", sa.Text), sa.column("field_mapping", sa.Text),
        sa.column("quirks", sa.Text), sa.column("status", sa.Text),
        sa.column("discovered_at", sa.DateTime), sa.column("last_verified_at", sa.DateTime),
        schema=SCHEMA,
    )
    op.execute(
        table.insert().values(
            county_fips="48265",
            base_url="https://services6.arcgis.com/j94FvPaik4etwHFk/arcgis/rest/services/KerrCADWebService/FeatureServer/0",
            service_type="arcgis_rest",
            field_mapping=sa.text("(:fm)::jsonb").bindparams(fm=json.dumps(FIELD_MAPPING)),
            quirks=sa.text("(:q)::jsonb").bindparams(q=json.dumps(QUIRKS)),
            status="active", discovered_at=sa.func.now(), last_verified_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.county_gis_registry WHERE county_fips = '48265'")
