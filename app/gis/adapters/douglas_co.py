"""Douglas County, CO parcel adapter -- "Parcels_Enriched", a single ArcGIS
Online-hosted FeatureServer layer that (unusually) already joins the
assessor's owner/location/values/legal-description attribute tables onto
the real parcel polygon geometry (most counties in this project split those
across separate services) -- discovered via the ArcGIS Online org search
(orgId seTexOicoRXDvRsJ, "DouglasCountyCO_GISServices" / "_OpenData"), not
the county's own domain. Field mapping confirmed against the live service
(2026-07-26) for covid 3595's 6 lots.

Quirk: there is no separate LOT number field -- only BLOCK_NO is structured;
the lot number lives embedded in GIS_LEGAL_DESC (e.g. "LOT 9 BLK 2 FAIRWAYS
AT LONE TREE # 2 AMENDED LIEBERMAN HOMES..."). Same shape as Bexar's
adapter: filter server-side on subdivision keywords + block, then parse and
filter the exact lot client-side.

Quirk: the GIS's own subdivision phrasing doesn't literally match the
deed's -- this covenant names "THE FAIRWAYS AT LONE TREE FILING NO. 2" but
the GIS text reads "FAIRWAYS AT LONE TREE # 2 AMENDED" ("FILING NO." vs
"#"). A literal substring match on the full deed name would find nothing.
So the subdivision filter uses the deed name's own distinctive words
(dropping short/filler tokens), not the phrase verbatim -- this project's
existing text-match-only counties (Bexar, Llano, ...) hit the same kind of
phrasing drift, not unique to Colorado.
"""
import re

from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://services.arcgis.com/seTexOicoRXDvRsJ/arcgis/rest/services/Parcels_Enriched/FeatureServer/0"
COUNTY_FIPS = "08035"

FIELD_MAPPING = {
    "apn": "ACCOUNT_NO",
    "owner_name": "OWNER_NAME",
    "situs": "LOCATION_ADDRESS",
    "city": "CITY_NAME",
    "zip_code": "LOCATION_ZIP_CODE",
    "legal_desc": "GIS_LEGAL_DESC",
    "block": "BLOCK_NO",
    "acreage": "TOTAL_NET_ACRES",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())

_STOPWORDS = {"THE", "AT", "OF", "AND", "FILING", "NO", "SECTION", "SEC", "AMENDMENT"}
_LOT_RE = re.compile(r"\bLOT\s+([0-9A-Z\-]+)\s+BLK\b", re.IGNORECASE)


def _subdivision_tokens(subdivision_name: str) -> list[str]:
    words = re.split(r"[^A-Za-z0-9]+", subdivision_name.upper())
    return [w for w in words if w and w not in _STOPWORDS and not w.isdigit() and len(w) > 1]


def _parse_lot(legal_desc: str | None) -> str | None:
    if not legal_desc:
        return None
    m = _LOT_RE.search(legal_desc)
    return m.group(1) if m else None


def iter_parcels(max_records: int | None = None, geometry: dict | None = None, where: str = "1=1"):
    for feat in iter_all_features(
        BASE_URL, where=where, out_fields=OUT_FIELDS, return_geometry=True, out_sr=4326,
        max_records=max_records, geometry=geometry,
    ):
        attrs = feat["attributes"]
        geom = feat.get("geometry")
        legal_desc = attrs.get("GIS_LEGAL_DESC")
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": attrs.get("ACCOUNT_NO"),
            "owner_name_raw": attrs.get("OWNER_NAME"),
            "situs_address": (attrs.get("LOCATION_ADDRESS") or "").strip() or None,
            "city": attrs.get("CITY_NAME") or None,
            "zip_code": attrs.get("LOCATION_ZIP_CODE") or None,
            "lot": _parse_lot(legal_desc),
            "block": attrs.get("BLOCK_NO"),
            "acreage": attrs.get("TOTAL_NET_ACRES"),
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": legal_desc,
        }


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Server-side filter on block + the deed name's distinctive keywords
    (see module docstring on why not the literal phrase), then an exact
    per-lot filter client-side against the lot number parsed from
    GIS_LEGAL_DESC."""
    tokens = _subdivision_tokens(subdivision_name)
    clauses = [f"UPPER(GIS_LEGAL_DESC) LIKE UPPER('%{t}%')" for t in tokens]
    if block:
        clauses.append(f"BLOCK_NO = '{block.replace(chr(39), chr(39) + chr(39))}'")
    where = " AND ".join(clauses) if clauses else "1=1"
    results = list(iter_parcels(where=where))
    wanted = {l.upper() for l in lots}
    return [r for r in results if r["lot"] and r["lot"].upper() in wanted]
