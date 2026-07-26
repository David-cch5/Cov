"""seed county_gis_registry for Montgomery County TX

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-23

Endpoint from BUILD_SPEC; field mapping confirmed directly against the live
service (queried 2026-07-23): PIN/pid both work as APN (identical values in
sampled records), ownerName/ownerAddress/situs/legalDescription confirmed present,
recited acreage is embedded as text in legalDescription (e.g. "ACRES 0.573").
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"


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
    op.execute(f"""
        INSERT INTO {SCHEMA}.county_gis_registry
          (county_fips, base_url, service_type, field_mapping, quirks, status, discovered_at, last_verified_at)
        VALUES (
          '48339',
          'https://services1.arcgis.com/PRoAPGnMSUqvTrzq/arcgis/rest/services/Tax_Parcel_view/FeatureServer/0',
          'arcgis_rest',
          '{json.dumps({
              "apn": "PIN", "owner_name": "ownerName", "owner_address": "ownerAddress",
              "situs": "situs", "legal_description": "legalDescription", "area_sqft": "Shape__Area",
          })}'::jsonb,
          '{json.dumps({
              "max_record_count": 2000, "native_spatial_reference_wkid": 2277,
              "requires_out_sr_4326_for_classification": True,
              "acreage_source": "computed from Shape__Area (sq ft, native SRID); legalDescription also carries recited acreage as text",
          })}'::jsonb,
          'active', now(), now()
        )
    """)


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.county_gis_registry WHERE county_fips = '48339'")
