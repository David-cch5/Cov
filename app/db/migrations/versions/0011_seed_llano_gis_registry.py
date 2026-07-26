"""seed county_gis_registry for Llano County TX

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-24

Endpoint discovered via the ArcGIS Online catalog search (BUILD_SPEC's fallback
path); owner account "bis_llanocad" and service description "Llano CAD Web
Service MXD" identify it as the county appraisal district's own feed, not a
third-party copy (a "Parcels_Llano_2016 - Copy" hit from the same search was
passed over for exactly that reason). Field mapping confirmed against the live
service (2026-07-24).

Honest limitation: tract_or_lot and Block exist as columns but are
inconsistently populated (frequently null even when legal_desc plainly shows
the lot) -- confirmed by sampling the Escondido Subdivision roster. So despite
looking structured, this is a text-match-against-legal_desc county like
Tarrant/Dallas, not an exact-attribute-filter county like Montgomery.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

FIELD_MAPPING = {
    "apn": "prop_id", "owner_name": "file_as_name", "legal_desc": "legal_desc",
    "legal_desc2": "legal_desc2", "legal_desc3": "legal_desc3", "acreage": "legal_acreage",
    "abstract_subdivision_code": "abs_subdv_cd", "situs_num": "situs_num",
    "situs_street": "situs_street", "situs_city": "situs_city", "situs_zip": "situs_zip",
    "deed_volume": "Volume", "deed_page": "Page", "deed_date": "Deed_Date",
}
QUIRKS = {
    "max_record_count": 2000, "native_spatial_reference_wkid": 2277,
    "requires_out_sr_4326_for_classification": True,
    "no_reliably_populated_lot_block_field": True,
    "lot_parsed_from": "legal_desc via regex (e.g. 'ESCONDIDO LT 121-B  0.770 AC') -- "
        "tract_or_lot/Block columns exist but are null on most records even when "
        "legal_desc clearly states the lot.",
    "subdivision_phase_note": "a single abs_subdv_cd can cover multiple differently-labeled "
        "phases of one master development (e.g. 'ESCONDIDO', 'ESCONDIDO II', 'ESCONDIDO "
        "PHASE 3' all shared code 10422 as observed) -- lot numbers are unique per phase, "
        "not per code, and a deed referencing just 'Escondido Subdivision' without a phase "
        "requires searching across all of them, not guessing one.",
    "source_discovery": "found via ArcGIS Online catalog search (arcgis.com/sharing/rest/search); "
        "owner account 'bis_llanocad' and service description 'Llano CAD Web Service MXD' "
        "identify it as the county appraisal district's own feed (a same-search hit "
        "'Parcels_Llano_2016 - Copy' under an unrelated personal account was passed over).",
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
            county_fips="48299",
            base_url="https://services.arcgis.com/3fXpNNO2cx0O3RtY/arcgis/rest/services/LlanoCADWebService/FeatureServer/0",
            service_type="arcgis_rest",
            field_mapping=sa.text("(:fm)::jsonb").bindparams(fm=json.dumps(FIELD_MAPPING)),
            quirks=sa.text("(:q)::jsonb").bindparams(q=json.dumps(QUIRKS)),
            status="active",
            discovered_at=sa.func.now(),
            last_verified_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {SCHEMA}.county_gis_registry WHERE county_fips = '48299'")
