"""Travis County, TX parcel adapter (Travis County's own GIS server,
gis.traviscountytx.gov -- TCAD's public parcel layer hosted directly on the
county's domain). Field mapping confirmed against the live service (2026-07-24).

Has a dedicated LOTS text field, but it's null on most parcels sampled (e.g.
the whole "NORTH PLAINS" subdivision) -- the lot has to be parsed out of
legal_desc instead (e.g. "LOT 2-A NORTH PLAINS AMENDED PLAT OF LTS 2-4 BLK A"),
same approach as the text-match-only counties.
"""
import re

from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://gis.traviscountytx.gov/server1/rest/services/Boundaries_and_Jurisdictions/TCAD_public/MapServer/0"
COUNTY_FIPS = "48453"

FIELD_MAPPING = {
    "apn": "PROP_ID", "legal_desc": "legal_desc", "lots": "LOTS",
    "acreage": "tcad_acres", "geo_id": "geo_id",
    "situs_num": "situs_num", "situs_street": "situs_street",
    "situs_city": "situs_city", "situs_zip": "situs_zip",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())

_LOT_RE = re.compile(r"\bLOTS?\s+([A-Z0-9]+(?:-[A-Z0-9]+)?)\b", re.IGNORECASE)
_LOT_TOKEN_RE = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)?", re.IGNORECASE)


def _parse_lot(legal_desc: str | None, lots_field: str | None) -> str | None:
    if lots_field:
        return lots_field
    if not legal_desc:
        return None
    m = _LOT_RE.search(legal_desc)
    return m.group(1).upper() if m else None


def _lot_tokens(lots_field: str | None, legal_desc: str | None) -> set[str]:
    tokens = set()
    if lots_field:
        tokens |= {t.upper() for t in _LOT_TOKEN_RE.findall(lots_field)}
    parsed = _parse_lot(legal_desc, lots_field)
    if parsed:
        tokens.add(parsed.upper())
    return tokens


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
        lot_tokens = _lot_tokens(attrs.get("LOTS"), legal_desc)
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": str(attrs.get("PROP_ID")),
            "owner_name_raw": None,  # not exposed on this public layer
            "situs_address": _situs(attrs),
            "city": attrs.get("situs_city") or None,
            "zip_code": attrs.get("situs_zip") or None,
            "lot": _parse_lot(legal_desc, attrs.get("LOTS")),
            "_lot_tokens": lot_tokens,
            "block": None,
            "acreage": attrs.get("tcad_acres"),
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": legal_desc,
        }


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Text match against legal_desc for the subdivision name, then filter to
    parcels whose lot (from LOTS if populated, else parsed from legal_desc)
    matches one of the wanted lot tokens -- amended-plat labels like "2-A"
    make this a token-membership check, not an exact string match."""
    token = subdivision_name.split(",")[0].strip().replace("'", "''")
    where = f"UPPER(legal_desc) LIKE UPPER('%{token}%')"
    if block:
        where += f" AND UPPER(legal_desc) LIKE UPPER('%BLK {block.replace(chr(39), chr(39)+chr(39))}%')"
    results = list(iter_parcels(where=where))
    wanted = {l.upper() for l in lots}
    return [r for r in results if r["_lot_tokens"] & wanted]
