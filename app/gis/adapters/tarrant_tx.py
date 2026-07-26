"""Tarrant County, TX parcel adapter (Fort Worth ArcGIS Online org). Endpoint
confirmed in BUILD_SPEC; field mapping confirmed directly against the live
service (2026-07-23).

Quirk vs. Montgomery: no structured Lot/Block fields -- subdivision/lot matching
here has to go against PARCEL_LEGAL_DESCRIPTION as free text, not an exact
attribute filter.
"""
import re

from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://services5.arcgis.com/3ddLCBXe1bRt7mzj/arcgis/rest/services/Parcels_Public_Vview/FeatureServer/0"
COUNTY_FIPS = "48439"

FIELD_MAPPING = {
    "apn": "TAXPIN",
    "owner_name": "OWNER_NAME",
    "owner_address": "OWNER_ADDRESS",
    "owner_city_state": "OWNER_CITY_ST",
    "owner_zip": "OWNER_ZIP_CODE",
    "situs": "SITUS_ADDR",
    "city": "CITYNAME",
    "zip_code": "Situs_ZipCode",
    "legal_description": "PARCEL_LEGAL_DESCRIPTION",
    "recited_acreage": "LAND_ACRE",
    "computed_acreage": "CalcAcres",
    "deed_date": "DEED_DATE",
    "deed_book": "DEED_BOOK",
    "deed_page": "DEED_PAGE",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())

_LOT_RE = re.compile(r"\bLOT\s+([A-Z0-9\-]+)", re.IGNORECASE)


def _parse_lot(legal_description: str | None) -> str | None:
    if not legal_description:
        return None
    m = _LOT_RE.search(legal_description)
    return m.group(1) if m else None


def iter_parcels(max_records: int | None = None, geometry: dict | None = None, where: str = "1=1"):
    for feat in iter_all_features(
        BASE_URL, where=where, out_fields=OUT_FIELDS, return_geometry=True, out_sr=4326,
        max_records=max_records, geometry=geometry,
    ):
        attrs = feat["attributes"]
        geom = feat.get("geometry")
        legal = attrs.get("PARCEL_LEGAL_DESCRIPTION")
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": str(attrs.get("TAXPIN")),
            "owner_name_raw": attrs.get("OWNER_NAME"),
            "situs_address": attrs.get("SITUS_ADDR") or None,
            "city": attrs.get("CITYNAME") or None,
            "zip_code": attrs.get("Situs_ZipCode") or None,
            "lot": _parse_lot(legal),
            "acreage": attrs.get("LAND_ACRE"),
            "computed_acreage": attrs.get("CalcAcres"),
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": legal,
            "deed_date": attrs.get("DEED_DATE"),
            "deed_book": attrs.get("DEED_BOOK"),
            "deed_page": attrs.get("DEED_PAGE"),
        }


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Text match against PARCEL_LEGAL_DESCRIPTION -- no structured Lot/Block field
    here, so this is inherently fuzzier than Montgomery's exact attribute filter.
    Caller should treat results as candidates, not a confirmed match, since a text
    match plus a lot-number match is not as certain as an exact attribute filter.
    `block` is accepted for interface parity with the other county adapters but not
    used for filtering -- there's no structured Block field to filter on here."""
    token = subdivision_name.split(",")[0].strip().replace("'", "''")
    where = f"UPPER(PARCEL_LEGAL_DESCRIPTION) LIKE UPPER('%{token}%')"
    results = list(iter_parcels(where=where))
    wanted = {l.upper() for l in lots}
    return [r for r in results if r["lot"] and r["lot"].upper() in wanted]
