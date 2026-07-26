"""Collin County, TX parcel adapter (Collin CAD's own ArcGIS Online org,
"CCAD_Maps" -- confirmed official by the owner account name). Field mapping
confirmed against the live service (2026-07-24).

Richly structured: legalAbsSubCode/legalAbsSubName/legalAbsSubBlock/
legalAbsSubLot are dedicated columns, covering both platted subdivisions
(name + lot) AND Texas-abstract survey descriptions (abstract code + block +
tract, e.g. "ABSTRACT A0166 CHERRY, DAVID, BLOCK 4, TRACT 163-9") -- the same
match-by-name-then-filter-by-token approach used elsewhere in this project
works for both, since a deed's "tract" and a plat's "lot" land in the same
legalAbsSubLot column here.
"""
from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://services2.arcgis.com/uXyoacYrZTPTKD3R/arcgis/rest/services/CCAD_Parcel_Feature_Set/FeatureServer/4"
COUNTY_FIPS = "48085"

FIELD_MAPPING = {
    "apn": "PROP_ID", "owner_name": "ownerName",
    "abs_sub_code": "legalAbsSubCode", "abs_sub_name": "legalAbsSubName",
    "abs_sub_block": "legalAbsSubBlock", "abs_sub_lot": "legalAbsSubLot",
    "legal_description": "legalDescription", "acreage": "landSizeAcres",
    "situs_num": "situsBldgNum", "situs_street": "situsStreetName",
    "situs_city": "situsCity", "situs_zip": "situsZip",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())


def _situs(attrs: dict) -> str | None:
    parts = [attrs.get("situsBldgNum"), attrs.get("situsStreetName")]
    joined = " ".join(str(p).strip() for p in parts if p)
    return joined or None


def iter_parcels(max_records: int | None = None, geometry: dict | None = None, where: str = "1=1"):
    for feat in iter_all_features(
        BASE_URL, where=where, out_fields=OUT_FIELDS, return_geometry=True, out_sr=4326,
        max_records=max_records, geometry=geometry,
    ):
        attrs = feat["attributes"]
        geom = feat.get("geometry")
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": str(attrs.get("PROP_ID")),
            "owner_name_raw": attrs.get("ownerName"),
            "situs_address": _situs(attrs),
            "city": attrs.get("situsCity") or None,
            "zip_code": attrs.get("situsZip") or None,
            "lot": attrs.get("legalAbsSubLot"),
            "block": attrs.get("legalAbsSubBlock"),
            "abstract_code": attrs.get("legalAbsSubCode"),
            "acreage": attrs.get("landSizeAcres"),
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": attrs.get("legalDescription"),
        }


def _sql_quote_list(values: list[str]) -> str:
    escaped = [v.replace("'", "''") for v in values]
    return ",".join(f"'{v}'" for v in escaped)


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Match legalAbsSubName (subdivision name OR abstract survey name, e.g.
    "CHERRY, DAVID") and filter to legalAbsSubLot in the wanted tract/lot list
    -- a real structured attribute filter once the name narrows candidates."""
    token = subdivision_name.split(",")[0].strip().replace("'", "''")
    where = f"UPPER(legalAbsSubName) LIKE UPPER('%{token}%') AND legalAbsSubLot IN ({_sql_quote_list(lots)})"
    if block:
        where += f" AND legalAbsSubBlock = '{block.replace(chr(39), chr(39)+chr(39))}'"
    return list(iter_parcels(where=where))


def query_by_abstract_code(abstract_code: str, tracts: list[str]):
    """Direct match by abstract code (e.g. "A0166") when the deed gives it
    explicitly -- more precise than a name-text search since abstract codes
    are unique county-wide, unlike survey names which can repeat."""
    where = f"UPPER(legalAbsSubCode) = UPPER('{abstract_code}') AND legalAbsSubLot IN ({_sql_quote_list(tracts)})"
    return list(iter_parcels(where=where))
