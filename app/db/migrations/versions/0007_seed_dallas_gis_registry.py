"""seed county_gis_registry for Dallas County TX

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-23

Endpoint discovered via the ArcGIS Online catalog search (BUILD_SPEC's fallback
path for when the appraisal-district hostname doesn't yield a direct hit); field
mapping confirmed against the live service (2026-07-23).

Honest limitation: this service's editingInfo reports dataLastEditDate ~mid-2020
-- the publicly available Dallas parcel data is roughly 5-6 years stale as of
this writing. Stated here rather than papered over, per CLAUDE.md's non-negotiable
on known hard limits.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

FIELD_MAPPING = {
    "apn": "ACCT", "owner_name": "TAXPANAME1", "owner_address": "TAXPAADD1",
    "owner_city": "TAXPACITY", "owner_state": "TAXPASTA", "owner_zip": "TAXPAZIP",
    "city": "CITY", "legal_1": "LEGAL_1", "legal_2": "LEGAL_2", "legal_3": "LEGAL_3",
    "street_number": "ST_NUM", "street_name": "ST_NAME", "street_type": "ST_TYPE",
    "street_dir": "ST_DIR", "area_sqft": "AREA_FEET",
}
QUIRKS = {
    "max_record_count": 2000, "native_spatial_reference_wkid": 4326,
    "requires_out_sr_4326_for_classification": True,
    "no_structured_lot_block_field": True,
    "legal_description_split_across_fields": "subdivision/installment name in LEGAL_1, "
        "'BLK X LT Y' embedded in LEGAL_2 with inconsistent spacing and LT/LOT abbreviation "
        "-- parsed via regex, not an exact attribute filter",
    "situs_address_is_composite": "no single situs field -- built from ST_NUM + ST_DIR + ST_NAME + ST_TYPE",
    "data_freshness_warning": "service editingInfo.dataLastEditDate is approximately mid-2020 as of "
        "this writing (2026-07-23) -- publicly available data is roughly 5-6 years stale. Ownership, "
        "replats, and new construction since then will not be reflected. A licensed/current DCAD feed "
        "would be needed to close this gap.",
    "source_discovery": "found via ArcGIS Online catalog search (arcgis.com/sharing/rest/search), not a "
        "BUILD_SPEC-confirmed endpoint like Montgomery/Tarrant",
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
            county_fips="48113",
            base_url="https://services2.arcgis.com/rwnOSbfKSwyTBcwN/arcgis/rest/services/Tax_Parcels_2019/FeatureServer/0",
            service_type="arcgis_rest",
            field_mapping=sa.text("(:fm)::jsonb").bindparams(fm=json.dumps(FIELD_MAPPING)),
            quirks=sa.text("(:q)::jsonb").bindparams(q=json.dumps(QUIRKS)),
            status="active",
            discovered_at=sa.func.now(),
            last_verified_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.county_gis_registry WHERE county_fips = '48113'")
