"""seed county_gis_registry for Tarrant County TX

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-23

Endpoint from BUILD_SPEC (Fort Worth ArcGIS Online org); field mapping
confirmed directly against the live service (queried 2026-07-23). No
structured Lot/Block field here (unlike Montgomery) -- subdivision/lot
matching goes against PARCEL_LEGAL_DESCRIPTION as free text.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

FIELD_MAPPING = {
    "apn": "TAXPIN", "owner_name": "OWNER_NAME", "owner_address": "OWNER_ADDRESS",
    "owner_city_state": "OWNER_CITY_ST", "owner_zip": "OWNER_ZIP_CODE", "situs": "SITUS_ADDR",
    "city": "CITYNAME", "zip_code": "Situs_ZipCode", "legal_description": "PARCEL_LEGAL_DESCRIPTION",
    "recited_acreage": "LAND_ACRE", "computed_acreage": "CalcAcres",
    "deed_date": "DEED_DATE", "deed_book": "DEED_BOOK", "deed_page": "DEED_PAGE",
}
QUIRKS = {
    "max_record_count": 2000, "native_spatial_reference_wkid": 3857,
    "requires_out_sr_4326_for_classification": True,
    "no_structured_lot_block_field": True,
    "subdivision_matching": "text match against PARCEL_LEGAL_DESCRIPTION only -- treat as candidates, "
                             "not confirmed, unless cross-checked another way",
    "apn_format": "composite PLAT-BLOCK-LOT style, e.g. 22348-B-5, not a plain numeric PIN",
    "bonus_fields": "carries the latest DEED_DATE/DEED_BOOK/DEED_PAGE directly on the parcel record -- "
                     "useful chain-of-title bootstrap/cross-check, not a substitute for the full recorder index",
}


def upgrade() -> None:
    table = sa.table(
        "county_gis_registry",
        sa.column("county_fips", sa.Text),
        sa.column("base_url", sa.Text),
        sa.column("service_type", sa.Text),
        sa.column("field_mapping", sa.Text),
        sa.column("quirks", sa.Text),
        sa.column("status", sa.Text),
        sa.column("discovered_at", sa.DateTime),
        sa.column("last_verified_at", sa.DateTime),
        schema=SCHEMA,
    )
    op.execute(
        table.insert().values(
            county_fips="48439",
            base_url="https://services5.arcgis.com/3ddLCBXe1bRt7mzj/arcgis/rest/services/Parcels_Public_Vview/FeatureServer/0",
            service_type="arcgis_rest",
            field_mapping=sa.text("(:fm)::jsonb").bindparams(fm=json.dumps(FIELD_MAPPING)),
            quirks=sa.text("(:q)::jsonb").bindparams(q=json.dumps(QUIRKS)),
            status="active",
            discovered_at=sa.func.now(),
            last_verified_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.county_gis_registry WHERE county_fips = '48439'")
