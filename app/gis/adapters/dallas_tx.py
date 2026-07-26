"""Dallas County, TX parcel adapter (Dallas Central Appraisal District data via
ArcGIS Online). Endpoint discovered through the ArcGIS Online catalog search
(BUILD_SPEC's fallback path); field mapping confirmed against the live service
(2026-07-23).

Known limitation, stated honestly rather than papered over: this service's
editingInfo reports dataLastEditDate ~mid-2020 -- the publicly available Dallas
parcel GIS data is roughly 5-6 years stale as of this writing. Ownership,
replats, and new construction since then will not be reflected. Use with that
caveat; a licensed/current DCAD feed would be needed to close this gap.

Quirk vs. Montgomery/Tarrant: legal description is split across LEGAL_1..LEGAL_5
fields (subdivision/installment name in LEGAL_1, "BLK X LT Y" in LEGAL_2 with
inconsistent spacing and LT/LOT abbreviation) rather than one text field or
structured Lot/Block columns.
"""
import re

from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://services2.arcgis.com/rwnOSbfKSwyTBcwN/arcgis/rest/services/Tax_Parcels_2019/FeatureServer/0"
COUNTY_FIPS = "48113"

FIELD_MAPPING = {
    "apn": "ACCT",
    "owner_name": "TAXPANAME1",
    "owner_address": "TAXPAADD1",
    "owner_city": "TAXPACITY",
    "owner_state": "TAXPASTA",
    "owner_zip": "TAXPAZIP",
    "city": "CITY",
    "legal_1": "LEGAL_1",
    "legal_2": "LEGAL_2",
    "legal_3": "LEGAL_3",
    "street_number": "ST_NUM",
    "street_name": "ST_NAME",
    "street_type": "ST_TYPE",
    "street_dir": "ST_DIR",
    "area_sqft": "AREA_FEET",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())

_BLOCK_LOT_RE = re.compile(r"BLK\s+([A-Z0-9]+)\s+L(?:T|OT)\s+([A-Z0-9\-]+)", re.IGNORECASE)


def _parse_block_lot(legal_2: str | None) -> tuple[str | None, str | None]:
    if not legal_2:
        return None, None
    m = _BLOCK_LOT_RE.search(legal_2)
    return (m.group(1), m.group(2)) if m else (None, None)


def _situs(attrs: dict) -> str | None:
    parts = [attrs.get("ST_NUM"), attrs.get("ST_DIR"), attrs.get("ST_NAME"), attrs.get("ST_TYPE")]
    joined = " ".join(str(p).strip() for p in parts if p)
    return joined or None


def iter_parcels(max_records: int | None = None, geometry: dict | None = None, where: str = "1=1"):
    for feat in iter_all_features(
        BASE_URL, where=where, out_fields=OUT_FIELDS, return_geometry=True, out_sr=4326,
        max_records=max_records, geometry=geometry,
    ):
        attrs = feat["attributes"]
        geom = feat.get("geometry")
        block, lot = _parse_block_lot(attrs.get("LEGAL_2"))
        area_sqft = attrs.get("AREA_FEET")
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": str(attrs.get("ACCT")),
            "owner_name_raw": attrs.get("TAXPANAME1"),
            "situs_address": _situs(attrs),
            "city": attrs.get("CITY") or None,
            "block": block,
            "lot": lot,
            "acreage": (area_sqft / 43560.0) if area_sqft else None,
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": " ".join(
                filter(None, [attrs.get("LEGAL_1"), attrs.get("LEGAL_2"), attrs.get("LEGAL_3")])
            ),
        }


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Text match against LEGAL_1 (subdivision/installment name) -- no structured
    Lot/Block field here either, same caveat as Tarrant: treat results as
    candidates, not confirmed, since this is a text match plus a lot-number match.
    `block` is accepted for interface parity but not used -- no structured Block field."""
    token = subdivision_name.split(",")[0].strip().replace("'", "''")
    where = f"UPPER(LEGAL_1) LIKE UPPER('%{token}%')"
    results = list(iter_parcels(where=where))
    wanted = {l.upper() for l in lots}
    return [r for r in results if r["lot"] and r["lot"].upper() in wanted]
