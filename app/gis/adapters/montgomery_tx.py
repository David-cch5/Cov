"""Montgomery County, TX parcel adapter. Endpoint confirmed in BUILD_SPEC; field
mapping confirmed directly against the live service (2026-07-23).
"""
import re

from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://services1.arcgis.com/PRoAPGnMSUqvTrzq/arcgis/rest/services/Tax_Parcel_view/FeatureServer/0"
COUNTY_FIPS = "48339"

FIELD_MAPPING = {
    "apn": "PIN",
    "owner_name": "ownerName",
    "owner_address": "ownerAddress",
    "situs": "situs",
    "legal_description": "legalDescription",
    "area_sqft": "Shape__Area",
    "lot": "Lot",
    "block": "Block",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())

_ACRES_RE = re.compile(r"ACRES\s+([\d.]+)", re.IGNORECASE)


def _parse_recited_acreage(legal_description: str | None) -> float | None:
    if not legal_description:
        return None
    m = _ACRES_RE.search(legal_description)
    return float(m.group(1)) if m else None


def iter_parcels(max_records: int | None = None, geometry: dict | None = None, where: str = "1=1"):
    """Yield normalized parcel dicts ready for the repository upsert. `geometry` is an
    Esri envelope {xmin,ymin,xmax,ymax,spatialReference} to scope the query -- e.g. to
    a covenant's tract bounding box -- rather than pulling the whole county roll."""
    for feat in iter_all_features(
        BASE_URL, where=where, out_fields=OUT_FIELDS, return_geometry=True, out_sr=4326,
        max_records=max_records, geometry=geometry,
    ):
        attrs = feat["attributes"]
        geom = feat.get("geometry")
        area_sqft = attrs.get("Shape__Area")
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": str(attrs.get("PIN")),
            "owner_name_raw": attrs.get("ownerName"),
            "situs_address": attrs.get("situs") or None,
            "lot": attrs.get("Lot"),
            "block": attrs.get("Block"),
            "acreage": (area_sqft / 43560.0) if area_sqft else _parse_recited_acreage(attrs.get("legalDescription")),
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": attrs.get("legalDescription"),
            "recited_acreage": _parse_recited_acreage(attrs.get("legalDescription")),
        }


def _sql_quote_list(values: list[str]) -> str:
    escaped = [v.replace("'", "''") for v in values]
    return ",".join(f"'{v}'" for v in escaped)


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Match parcels by (a) legalDescription containing the subdivision name and
    (b) Lot in the given list -- Montgomery's GIS carries Lot as its own structured
    field, so this is a real attribute filter, not a text-similarity guess.

    Pass `block` when the legal description names one: a replat sharing the same
    subdivision-name substring can reuse lot numbers in a different block (e.g.
    "Crescent Cove 03 Replat No 1, BLOCK 2, Lot 1" vs the original plat's own
    "BLOCK 1, Lot 1") -- without the block filter, both match "Lot 1" and silently
    pull in a parcel the deed never described."""
    subdivision_token = subdivision_name.split(",")[0].strip().replace("'", "''")
    where = f"UPPER(legalDescription) LIKE UPPER('%{subdivision_token}%') AND Lot IN ({_sql_quote_list(lots)})"
    if block:
        where += f" AND Block = '{block.replace(chr(39), chr(39)+chr(39))}'"
    return list(iter_parcels(where=where))
