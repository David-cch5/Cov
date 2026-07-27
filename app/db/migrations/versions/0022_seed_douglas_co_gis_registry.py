"""seed county_gis_registry for Douglas County, CO (disclosure-state test case)

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-26

Endpoint discovered via the ArcGIS Online org search (orgId
seTexOicoRXDvRsJ, "DouglasCountyCO_GISServices"). Unlike most counties in
this project, "Parcels_Enriched" already joins the assessor's owner/
location/legal-description attributes onto the real parcel polygon
geometry in one layer -- no separate attribute-only + geometry-only query
split needed. Field mapping confirmed against the live service (2026-07-26)
for covid 3595's 6 lots (Fairways at Lone Tree Filing No. 2, Lots 9-14,
Block 2) -- all 6 matched with real geometry.

cad_sales_data_url records a second, separate service on the same org
(OpenData/FeatureServer/7, "Property_Sales_Data") that is the actual reason
Douglas County was picked: unlike Bexar's CAD deed history (grantor/
grantee/date/deed type only -- Texas is non-disclosure), Colorado is a
full-disclosure state and this table carries an actual SALE_PRICE per
transaction, joinable by the same ACCOUNT_NO -- exactly the real,
non-fabricated actual-price source the disclosure-state price-extraction
work needs, with no separate recorder-portal build required for this
county.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

FIELD_MAPPING = {
    "apn": "ACCOUNT_NO", "owner_name": "OWNER_NAME", "situs": "LOCATION_ADDRESS",
    "city": "CITY_NAME", "zip_code": "LOCATION_ZIP_CODE", "legal_desc": "GIS_LEGAL_DESC",
    "block": "BLOCK_NO", "acreage": "TOTAL_NET_ACRES", "filing_name": "FILING_DESCR",
}
QUIRKS = {
    "source_discovery": "found via ArcGIS Online org search (arcgis.com/sharing/rest/search, "
        "orgId seTexOicoRXDvRsJ) -- 'Parcels_Enriched' owned by DouglasCountyCO_GISServices, "
        "distinct from the county's separate 'OpenData' FeatureServer (non-spatial attribute "
        "tables only: owner/location/values/sales -- all geometryType None, confirmed by "
        "querying each layer's metadata directly).",
    "no_lot_number_field": "only BLOCK_NO is a structured column; the lot number is embedded "
        "in GIS_LEGAL_DESC (e.g. 'LOT 9 BLK 2 FAIRWAYS AT LONE TREE # 2 AMENDED...') and is "
        "parsed client-side via regex -- same shape as Bexar's adapter.",
    "subdivision_phrasing_drift": "the deed names 'THE FAIRWAYS AT LONE TREE FILING NO. 2' but "
        "GIS_LEGAL_DESC reads 'FAIRWAYS AT LONE TREE # 2 AMENDED' -- a literal substring match "
        "on the deed's own phrase finds nothing; the adapter filters on the deed name's "
        "distinctive keywords instead (dropping short/filler words), the same kind of "
        "phrasing drift already seen in Bexar/Llano.",
    "cad_sales_data_url": "https://services.arcgis.com/seTexOicoRXDvRsJ/arcgis/rest/services/"
        "OpenData/FeatureServer/7",
    "cad_sales_data_vendor": "douglas_co_assessor_arcgis",
    "cad_sales_data_note": "plain unauthenticated GET .../query?where=ACCOUNT_NO IN (...) -- "
        "confirmed via curl. Returns SALE_DATE, SALE_PRICE (actual, not estimated -- Colorado "
        "is a full-disclosure state), DEED_TYPE, GRANTOR, GRANTEE, BOOK, PAGE, RECORDING_NO "
        "per ACCOUNT_NO. This is the primary reason Douglas County was picked as the "
        "disclosure-state price-extraction test case.",
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
            county_fips="08035",
            base_url="https://services.arcgis.com/seTexOicoRXDvRsJ/arcgis/rest/services/Parcels_Enriched/FeatureServer/0",
            service_type="arcgis_rest",
            field_mapping=sa.text("(:fm)::jsonb").bindparams(fm=json.dumps(FIELD_MAPPING)),
            quirks=sa.text("(:q)::jsonb").bindparams(q=json.dumps(QUIRKS)),
            status="active",
            discovered_at=sa.func.now(),
            last_verified_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.county_gis_registry WHERE county_fips = '08035'")
