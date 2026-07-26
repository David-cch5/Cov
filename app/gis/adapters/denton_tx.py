"""Denton County, TX parcel adapter (Denton CAD's own ArcGIS Online feed,
"Denton_CAD_Parcels"). Field mapping confirmed against the live service
(2026-07-24).

Most structured of this project's adapters: abstractSubdivisionDescription,
block, tract, AND lot are all dedicated columns (Denton's own CAD software
distinguishes "tract" from "lot" explicitly, matching how a Texas-abstract
deed calls out "Tr 47" rather than a plat's "Lot"). A subdivision/abstract
name match plus an exact tract-or-lot attribute filter is as solid here as
Montgomery's Lot/Block match.
"""
from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://services1.arcgis.com/qr14biwnHA6Vis6l/arcgis/rest/services/Denton_CAD_Parcels/FeatureServer/0"
COUNTY_FIPS = "48121"

FIELD_MAPPING = {
    "apn": "pid", "owner_name": "name", "abstract_code": "asCode",
    "abstract_subdivision_description": "abstractSubdivisionDescription",
    "block": "block", "tract": "tract", "lot": "lot",
    "legal_description": "legalDescription", "acreage": "legalAcreage",
    "situs_num": "situsStreetNumb", "situs_street": "situsStreetName",
    "situs_city": "situsCity", "situs_zip": "situsZip",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())


def _situs(attrs: dict) -> str | None:
    parts = [attrs.get("situsStreetNumb"), attrs.get("situsStreetName")]
    joined = " ".join(str(p).strip() for p in parts if p)
    return joined or None


def iter_parcels(max_records: int | None = None, geometry: dict | None = None, where: str = "1=1"):
    for feat in iter_all_features(
        BASE_URL, where=where, out_fields=OUT_FIELDS, return_geometry=True, out_sr=4326,
        max_records=max_records, geometry=geometry,
    ):
        attrs = feat["attributes"]
        geom = feat.get("geometry")
        # a Texas-abstract deed's "tract" and a plat's "lot" are separate
        # columns here -- fall back to whichever one is populated.
        lot_or_tract = attrs.get("lot") or attrs.get("tract")
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": str(attrs.get("pid")),
            "owner_name_raw": attrs.get("name"),
            "situs_address": _situs(attrs),
            "city": attrs.get("situsCity") or None,
            "zip_code": attrs.get("situsZip") or None,
            "lot": lot_or_tract,
            "block": attrs.get("block"),
            "acreage": attrs.get("legalAcreage"),
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": attrs.get("legalDescription"),
        }


def _sql_quote_list(values: list[str]) -> str:
    escaped = [v.replace("'", "''") for v in values]
    return ",".join(f"'{v}'" for v in escaped)


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Match abstractSubdivisionDescription and filter to tract OR lot in the
    wanted list -- exact attribute filter on whichever column the parcel
    actually populates."""
    token = subdivision_name.split(",")[0].strip().replace("'", "''")
    lot_list = _sql_quote_list(lots)
    where = (
        f"UPPER(abstractSubdivisionDescription) LIKE UPPER('%{token}%') "
        f"AND (tract IN ({lot_list}) OR lot IN ({lot_list}))"
    )
    if block:
        where += f" AND block = '{block.replace(chr(39), chr(39)+chr(39))}'"
    return list(iter_parcels(where=where))


def query_by_abstract_code(abstract_code: str, tracts: list[str]):
    """Direct match by abstract code (e.g. "A0336A") -- unique county-wide,
    unlike a survey grantee's name which can vary in spelling/abbreviation
    and repeat across adjoining abstracts (e.g. Denton has separate "WM
    DICKSON", "J.O. DICKSON", "C.C. DICKSON", "J.S. DICKSON" surveys)."""
    where = f"UPPER(asCode) = UPPER('{abstract_code}') AND (tract IN ({_sql_quote_list(tracts)}) OR lot IN ({_sql_quote_list(tracts)}))"
    return list(iter_parcels(where=where))
