"""seed county_gis_registry for Bexar, Harris, Travis, Collin, Nueces, Denton

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-24

Six more counties from the expanded 10-covenant TX sample. Endpoints
discovered via ArcGIS Online catalog search; Bexar/Harris/Travis were found
hosted directly on the county's own domain (maps.bcad.org, gis.hctx.net,
gis.traviscountytx.gov) -- the strongest provenance signal used in this
project so far, stronger even than an ArcGIS Online org account name. Collin
was identified by its "CCAD_Maps" owner account; Nueces/Denton by their
"<County>CADWebService"/"<County>_CAD_Parcels" service naming, matching the
same convention already confirmed official for Llano/Hunt.

Webb County: searched but no public GIS parcel feed could be found (neither
on ArcGIS Online nor a discoverable webbcad.org endpoint) -- not seeded here;
the covenant in that county (covid 2340) is flagged needs_review instead.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "covenant"

ROWS = [
    {
        "county_fips": "48029",
        "base_url": "https://maps.bcad.org/arcgis/rest/services/PAMapSearch/MapServer/6",
        "field_mapping": {
            "apn": 'PAMaps.DBO.ParcelFabric_Parcels.PROP_ID', "owner_name": "PAMaps.dbo.web_map_property.owner_name",
            "legal_desc": "PAMaps.dbo.web_map_property.legal_desc", "situs": "PAMaps.dbo.web_map_property.situs",
        },
        "quirks": {
            "source_discovery": "found directly on the appraisal district's own domain (maps.bcad.org), "
                "not via an ArcGIS Online org account",
            "field_names_fully_qualified": "this service is a SQL view join -- every field name is dotted "
                "(e.g. 'PAMaps.dbo.web_map_property.legal_desc') and must be double-quoted in a where "
                "clause; bracket-quoting does not work (confirmed by testing both)",
            "legal_desc_not_always_a_subdivision_name": "a deed can say 'City of <municipality>' with no "
                "real subdivision to search on (covid 2497) -- situs address matched precisely instead",
        },
    },
    {
        "county_fips": "48201",
        "base_url": "https://www.gis.hctx.net/arcgis/rest/services/HCAD/Parcels/MapServer/0",
        "field_mapping": {
            "apn": "HCAD_NUM", "owner_name": "owner_name_1", "legal_1": "legal_dscr_1",
            "block": "BLK_NUM", "lot": "LOT_NUM", "acreage": "Acreage",
        },
        "quirks": {
            "source_discovery": "hosted directly on Harris County's own domain (gis.hctx.net)",
            "structured_lot_block": "BLK_NUM/LOT_NUM are real, reliably-populated columns (in addition to "
                "a 4-part split legal description, legal_dscr_1..4) -- closer to Montgomery's exact-match "
                "confidence than the text-match-only counties",
        },
    },
    {
        "county_fips": "48453",
        "base_url": "https://gis.traviscountytx.gov/server1/rest/services/Boundaries_and_Jurisdictions/TCAD_public/MapServer/0",
        "field_mapping": {"apn": "PROP_ID", "legal_desc": "legal_desc", "lots": "LOTS", "acreage": "tcad_acres"},
        "quirks": {
            "source_discovery": "hosted directly on Travis County's own domain (gis.traviscountytx.gov)",
            "lots_field_not_single_value": "LOTS can hold amended-plat labels like '2-A' or a comma-joined "
                "list -- matched by token membership, not an exact IN() filter",
            "no_owner_name_field": "not exposed on this public layer",
        },
    },
    {
        "county_fips": "48085",
        "base_url": "https://services2.arcgis.com/uXyoacYrZTPTKD3R/arcgis/rest/services/CCAD_Parcel_Feature_Set/FeatureServer/4",
        "field_mapping": {
            "apn": "PROP_ID", "owner_name": "ownerName", "abs_sub_code": "legalAbsSubCode",
            "abs_sub_name": "legalAbsSubName", "abs_sub_block": "legalAbsSubBlock",
            "abs_sub_lot": "legalAbsSubLot", "acreage": "landSizeAcres",
        },
        "quirks": {
            "source_discovery": "owner account 'CCAD_Maps' -- Collin CAD's own ArcGIS Online org",
            "layer_id_is_4_not_0": "the FeatureServer's parcel layer is id 4 ('Parcels'); id 0 doesn't exist "
                "on this service (it also publishes Abstracts/Subdivisions/City Limits/etc. as separate layers)",
            "covers_both_plat_and_abstract": "legalAbsSubCode/Name/Block/Lot cover both platted subdivisions "
                "(name + lot) and Texas-abstract survey descriptions (abstract code + block + tract) in the "
                "same columns",
        },
    },
    {
        "county_fips": "48355",
        "base_url": "https://services6.arcgis.com/j94FvPaik4etwHFk/ArcGIS/rest/services/NuecesCADWebService/FeatureServer/0",
        "field_mapping": {
            "apn": "prop_id", "owner_name": "file_as_name", "legal_desc": "legal_desc",
            "acreage": "legal_acreage", "abstract_subdivision_code": "abs_subdv_cd",
        },
        "quirks": {
            "source_discovery": "service name 'NuecesCADWebService' matches the same vendor naming "
                "convention already confirmed official for Llano/Hunt",
            "no_reliably_populated_lot_block_field": "same as Llano -- lot parsed from legal_desc via regex",
        },
    },
    {
        "county_fips": "48121",
        "base_url": "https://services1.arcgis.com/qr14biwnHA6Vis6l/arcgis/rest/services/Denton_CAD_Parcels/FeatureServer/0",
        "field_mapping": {
            "apn": "pid", "owner_name": "name", "abstract_subdivision_description": "abstractSubdivisionDescription",
            "block": "block", "tract": "tract", "lot": "lot", "acreage": "legalAcreage",
        },
        "quirks": {
            "source_discovery": "service name 'Denton_CAD_Parcels', owner account is a Denton-specific GIS user",
            "structured_tract_and_lot": "dedicated tract AND lot columns, distinguishing a Texas-abstract "
                "deed's 'Tr 47' from a plat's 'Lot' explicitly -- the most structured schema of this "
                "project's adapters",
        },
    },
]


def upgrade() -> None:
    table = sa.table(
        "county_gis_registry",
        sa.column("county_fips", sa.Text), sa.column("base_url", sa.Text),
        sa.column("service_type", sa.Text), sa.column("field_mapping", sa.Text),
        sa.column("quirks", sa.Text), sa.column("status", sa.Text),
        sa.column("discovered_at", sa.DateTime), sa.column("last_verified_at", sa.DateTime),
        schema=SCHEMA,
    )
    for row in ROWS:
        op.execute(
            table.insert().values(
                county_fips=row["county_fips"], base_url=row["base_url"], service_type="arcgis_rest",
                field_mapping=sa.text("(:fm)::jsonb").bindparams(fm=json.dumps(row["field_mapping"])),
                quirks=sa.text("(:q)::jsonb").bindparams(q=json.dumps(row["quirks"])),
                status="active", discovered_at=sa.func.now(), last_verified_at=sa.func.now(),
            )
        )


def downgrade() -> None:
    op.execute(f"""
        DELETE FROM {SCHEMA}.county_gis_registry WHERE county_fips IN
        ('48029','48201','48453','48085','48355','48121')
    """)
