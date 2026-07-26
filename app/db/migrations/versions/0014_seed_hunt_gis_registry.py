"""seed county_gis_registry for Hunt County TX

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-24

Endpoint discovered during Webb/Hunt outlier research: HuntCADWebService via
ArcGIS Online, owner account "bis_huntcad" -- the same naming convention
(bis_<county>cad + <County>CADWebService) already confirmed official for
Llano and Nueces, all evidently the same underlying CAD software vendor.

This closes out covid 5346: the covenant's Exhibit A legal description was
initially misread as having no subdivision name at all -- the cached OCR text
had dropped it -- but reading the source page image directly showed "Being
all of Lot 8, Lowe's Home Centers Addition, Greenville, Texas." Matched
cleanly: prop_id 205334, owner "KMS RETAIL WAXAHACHIE LP" (the covenant's own
declarant), 1.3078 acres.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

FIELD_MAPPING = {
    "apn": "prop_id", "owner_name": "file_as_name", "legal_desc": "legal_desc",
    "legal_desc2": "legal_desc2", "legal_desc3": "legal_desc3",
    "acreage": "legal_acreage", "abstract_subdivision_code": "abs_subdv_cd", "block": "Block",
}
QUIRKS = {
    "source_discovery": "service name 'HuntCADWebService', owner account 'bis_huntcad' -- same "
        "vendor naming convention already confirmed official for Llano/Nueces",
    "no_reliably_populated_lot_block_field": "same as Llano/Nueces -- lot parsed from legal_desc via regex",
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
            county_fips="48231",
            base_url="https://services3.arcgis.com/GIIiqmeq0npieHV9/arcgis/rest/services/HuntCADWebService/FeatureServer/0",
            service_type="arcgis_rest",
            field_mapping=sa.text("(:fm)::jsonb").bindparams(fm=json.dumps(FIELD_MAPPING)),
            quirks=sa.text("(:q)::jsonb").bindparams(q=json.dumps(QUIRKS)),
            status="active", discovered_at=sa.func.now(), last_verified_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.county_gis_registry WHERE county_fips = '48231'")
