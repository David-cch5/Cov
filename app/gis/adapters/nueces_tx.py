"""Nueces County, TX parcel adapter (NuecesCADWebService via ArcGIS Online).
Field mapping confirmed against the live service (2026-07-24).

Same field-naming convention as Llano (prop_id, file_as_name, legal_desc,
tract_or_lot, abs_subdv_cd, lowercase `block`) -- same underlying CAD software
vendor as Llano/Bexar/Hunt, evidently used across many Texas appraisal
districts. Same caveat as Llano: tract_or_lot/block exist but aren't reliably
populated, so this is a text match against legal_desc, not an exact filter.
"""
import re

from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://services6.arcgis.com/j94FvPaik4etwHFk/ArcGIS/rest/services/NuecesCADWebService/FeatureServer/0"
COUNTY_FIPS = "48355"

FIELD_MAPPING = {
    "apn": "prop_id", "owner_name": "file_as_name", "legal_desc": "legal_desc",
    "legal_desc2": "legal_desc2", "legal_desc3": "legal_desc3",
    "acreage": "legal_acreage", "abstract_subdivision_code": "abs_subdv_cd",
    "situs_num": "situs_num", "situs_street": "situs_street",
    "situs_city": "situs_city", "situs_zip": "situs_zip",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())

_LOT_RE = re.compile(r"\bLT\s+([A-Z0-9]+(?:-[A-Z0-9]+)?)\b", re.IGNORECASE)


def _parse_lot(legal_desc: str | None) -> str | None:
    if not legal_desc:
        return None
    m = _LOT_RE.search(legal_desc)
    return m.group(1).upper() if m else None


def _situs(attrs: dict) -> str | None:
    parts = [attrs.get("situs_num"), attrs.get("situs_street")]
    joined = " ".join(str(p).strip() for p in parts if p)
    return joined or None


def iter_parcels(max_records: int | None = None, geometry: dict | None = None, where: str = "1=1"):
    for feat in iter_all_features(
        BASE_URL, where=where, out_fields=OUT_FIELDS, return_geometry=True, out_sr=4326,
        max_records=max_records, geometry=geometry,
    ):
        attrs = feat["attributes"]
        geom = feat.get("geometry")
        legal_desc = attrs.get("legal_desc")
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": str(attrs.get("prop_id")),
            "owner_name_raw": attrs.get("file_as_name"),
            "situs_address": _situs(attrs),
            "city": attrs.get("situs_city") or None,
            "zip_code": attrs.get("situs_zip") or None,
            "lot": _parse_lot(legal_desc),
            "block": None,
            "acreage": attrs.get("legal_acreage"),
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": " ".join(
                filter(None, [legal_desc, attrs.get("legal_desc2"), attrs.get("legal_desc3")])
            ),
        }


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Unlike Llano's Escondido case (where different phases share one root
    name and matching just the first word is deliberate), Nueces subdivision
    names like "GLEN ARBOR UNIT 5" should be matched close to whole -- a
    first-word-only truncation would pull in every Glen Arbor unit/section."""
    token = subdivision_name.split(",")[0].strip().replace("'", "''")
    where = f"UPPER(legal_desc) LIKE UPPER('%{token}%')"
    results = list(iter_parcels(where=where))
    wanted = {l.upper() for l in lots}
    return [r for r in results if r["lot"] and r["lot"].upper() in wanted]
