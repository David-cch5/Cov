"""Bexar County, TX parcel adapter (Bexar Appraisal District's own ArcGIS
Server, maps.bcad.org -- discovered directly on the district's own domain,
the strongest kind of provenance in this project's adapters so far). Field
mapping confirmed against the live service (2026-07-24).

Quirk: this service is a SQL view join, so every field name is fully
qualified ("PAMaps.dbo.web_map_property.legal_desc"). In a `where` clause it
MUST be double-quoted (bracket-quoting like some ArcGIS/SQL Server stacks
accept does not work here -- confirmed by testing both); in `outFields` it
must NOT be quoted -- quoting it there causes the service to fail the whole
query (confirmed by testing both ways). So FIELD_MAPPING holds the bare
dotted names, and only the `where`-building helpers add quotes.

Quirk: legal descriptions here are NCB (New City Block)/Block/Lot based and
don't always name a real subdivision -- a deed can say "City of Leon Valley"
(the municipality, not a platted subdivision) with no other name to search on.
For that case, situs address is a far more precise anchor than a lot/block
guess (see query_by_situs, used when query_by_subdivision_and_lots comes up
empty or the "subdivision" is really just a city name).

Quirk: this service errors ("Failed to execute query") whenever returnGeometry
is combined with a `where` filter on a web_map_property column (any of them --
confirmed with situs, legal_desc, and even just PROP_ID from that table)
regardless of outSR. Filtering on ParcelFabric_Parcels.PROP_ID (the geometry
table's own column) works fine with geometry. So iter_parcels runs two
queries: attributes-only (with the caller's `where`) to find matching
PROP_IDs, then a geometry-only query filtered on that PROP_ID list.
"""
import re

from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://maps.bcad.org/arcgis/rest/services/PAMapSearch/MapServer/6"
COUNTY_FIPS = "48029"

FIELD_MAPPING = {
    "apn": "PAMaps.DBO.ParcelFabric_Parcels.PROP_ID",
    "owner_name": "PAMaps.dbo.web_map_property.owner_name",
    "legal_desc": "PAMaps.dbo.web_map_property.legal_desc",
    "situs": "PAMaps.dbo.web_map_property.situs",
    "abstract_subdivision_code": "PAMaps.dbo.web_map_property.abs_subdv_cd",
    "addr_city": "PAMaps.dbo.web_map_property.addr_city",
    "addr_zip": "PAMaps.dbo.web_map_property.addr_zip",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())


def _q(field: str) -> str:
    """Double-quote a dotted field name for use in a `where` clause."""
    return f'"{field}"'

# Captures the base lot token(s) after "LOT " in Bexar's legal_desc, e.g.
# "LOT 1, 2, NW IRR 4.73 FT OF 3 & N IRR 20.94 FT OF 19" -- deliberately loose
# (a partial-lot phrase like this is common when the tract is an irregular
# remainder, not a whole lot) since this is only used to narrow candidates,
# never to claim an exact single-lot match on its own.
_LOT_RE = re.compile(r"\bLOT\b\s+([0-9][0-9A-Z,\-\s&]*?)(?:\s+NCB\b|$)", re.IGNORECASE)


def _parse_lots(legal_desc: str | None) -> list[str]:
    if not legal_desc:
        return []
    m = _LOT_RE.search(legal_desc)
    if not m:
        return []
    # split the loose capture on commas/ampersands/"OF" into individual lot numbers
    tokens = re.split(r"[,&]|\bOF\b", m.group(1))
    lots = []
    for t in tokens:
        num = re.search(r"\d+[A-Z]?", t)
        if num:
            lots.append(num.group())
    return lots


def iter_parcels(max_records: int | None = None, geometry: dict | None = None, where: str = "1=1"):
    # step 1: attributes only, no geometry -- this is the query that can carry
    # a web_map_property filter safely.
    records = list(iter_all_features(
        BASE_URL, where=where, out_fields=OUT_FIELDS, return_geometry=False,
        max_records=max_records, geometry=geometry,
    ))
    if not records:
        return
    prop_ids = [str(r["attributes"].get("PAMaps.DBO.ParcelFabric_Parcels.PROP_ID")) for r in records]
    prop_id_list = ",".join(prop_ids)

    # step 2: geometry only, filtered on the geometry table's own PROP_ID --
    # the one combination that doesn't trip the service's query-plan error.
    geom_where = f"{_q('PAMaps.DBO.ParcelFabric_Parcels.PROP_ID')} IN ({prop_id_list})"
    geoms_by_id = {}
    for feat in iter_all_features(
        BASE_URL, where=geom_where, out_fields="PAMaps.DBO.ParcelFabric_Parcels.PROP_ID",
        return_geometry=True, out_sr=4326,
    ):
        pid = str(feat["attributes"].get("PAMaps.DBO.ParcelFabric_Parcels.PROP_ID"))
        geoms_by_id[pid] = feat.get("geometry")

    for rec in records:
        attrs = rec["attributes"]
        legal_desc = attrs.get("PAMaps.dbo.web_map_property.legal_desc")
        pid = str(attrs.get("PAMaps.DBO.ParcelFabric_Parcels.PROP_ID"))
        geom = geoms_by_id.get(pid)
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": pid,
            "owner_name_raw": attrs.get("PAMaps.dbo.web_map_property.owner_name"),
            "situs_address": attrs.get("PAMaps.dbo.web_map_property.situs"),
            "city": attrs.get("PAMaps.dbo.web_map_property.addr_city") or None,
            "zip_code": attrs.get("PAMaps.dbo.web_map_property.addr_zip") or None,
            "lot": (_parse_lots(legal_desc) or [None])[0],
            "block": None,
            "acreage": None,  # not exposed on this layer; left for the caller to compute from geometry
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": legal_desc,
        }


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Text match against legal_desc. Only useful when the deed names a real
    subdivision -- a deed that just says "City of <municipality>" won't match
    anything real here and the caller should fall back to query_by_situs."""
    token = subdivision_name.split(",")[0].strip().replace("'", "''")
    where = f"UPPER({_q('PAMaps.dbo.web_map_property.legal_desc')}) LIKE UPPER('%{token}%')"
    results = list(iter_parcels(where=where))
    wanted = {l.upper() for l in lots}
    return [r for r in results if r["lot"] and r["lot"].upper() in wanted]


def query_by_situs(situs_fragment: str):
    """Match by situs address -- the right anchor when the legal description
    doesn't name a real, searchable subdivision (see module docstring)."""
    token = situs_fragment.strip().replace("'", "''")
    where = f"UPPER({_q('PAMaps.dbo.web_map_property.situs')}) LIKE UPPER('%{token}%')"
    return list(iter_parcels(where=where))
