"""Dallas County, TX parcel adapter (Dallas Central Appraisal District data via
ArcGIS Online). Endpoint discovered through the ArcGIS Online catalog search
(BUILD_SPEC's fallback path); field mapping confirmed against the live service
(2026-07-23).

TWO LAYERS, because no single public one carries both current geometry and the
attributes. GEOMETRY comes from CurrentDcadParcels (updated daily by Dallas GIS,
695,446 parcels); ATTRIBUTES -- owner, legal description, address -- come from
Tax_Parcels_2019, which is the only public layer that has them. Joined on the
account number.

That split exists because the 2019 layer's geometry is not merely old, it is
WRONG about boundaries that have since moved, and the error is large enough to
change an answer. Measured on covid 4956, a 0.9907-acre Farmers Branch tract:

    parcel                       2019 AREA_FEET   current Shape__Area   delta
    24123500010140000 (SSM)              37,154                43,155   +6,001
    24049800010010100 (CONLON)           45,401                39,400   -6,001

Equal and opposite: Dallas moved 6,001 sq ft from one parcel to its neighbour,
reflecting a 2017 conveyance (INT201700012130). On the 2019 geometry the deed's
tract overhung the neighbour by 0.1381 ac and the covenant looked as though it
encumbered part of someone else's land. On the current geometry the tract is
43,154.6 sq ft against the deed's own stated 43,154.9 -- agreement to 0.3 SQUARE
FEET, one parcel, no residual.

The 2019 layer's ATTRIBUTES are still stale in the ordinary way (ownership and
legal descriptions as of 2019), and its AREA_FEET is deliberately not used --
the current layer's own RecAcs text field is stale too ("0.8529 a" on a parcel
its geometry now measures at 0.9907 ac), so acreage comes from the current
GEOMETRY, which is the only figure that matches the deed.

Quirk vs. Montgomery/Tarrant: legal description is split across LEGAL_1..LEGAL_5
fields (subdivision/installment name in LEGAL_1, "BLK X LT Y" in LEGAL_2 with
inconsistent spacing and LT/LOT abbreviation) rather than one text field or
structured Lot/Block columns.
"""
import re

from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

# Attributes only (owner, legal, address). Its geometry and AREA_FEET are not used.
BASE_URL = "https://services2.arcgis.com/rwnOSbfKSwyTBcwN/arcgis/rest/services/Tax_Parcels_2019/FeatureServer/0"
# Geometry, current. Only 5 fields: OBJECTID, RecAcs, GIS_Acct, Shape__Area,
# Shape__Length -- hence the join rather than a straight swap.
CURRENT_GEOM_URL = ("https://services2.arcgis.com/rwnOSbfKSwyTBcwN/arcgis/rest/services/"
                    "CurrentDcadParcels/FeatureServer/0")
CURRENT_ACCT_FIELD = "GIS_Acct"
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


def _current_geometry(accts: list[str]) -> dict:
    """{acct: (geojson, area_sqft)} from the current layer, for the accounts given.

    Batched with an IN(...) predicate rather than one request per parcel: a
    subdivision-wide census is hundreds of accounts and the service caps at 2,000
    records per page anyway.
    """
    out: dict = {}
    for chunk_start in range(0, len(accts), 200):
        chunk = accts[chunk_start:chunk_start + 200]
        quoted = ",".join("'" + a.replace("'", "''") + "'" for a in chunk)
        for feat in iter_all_features(
            CURRENT_GEOM_URL, where=f"{CURRENT_ACCT_FIELD} IN ({quoted})",
            out_fields=f"{CURRENT_ACCT_FIELD},Shape__Area", return_geometry=True, out_sr=4326,
        ):
            attrs = feat["attributes"]
            geom = feat.get("geometry")
            if not (geom and geom.get("rings")):
                continue
            out[str(attrs.get(CURRENT_ACCT_FIELD))] = (
                esri_rings_to_geojson_multipolygon(geom["rings"]),
                attrs.get("Shape__Area"),
            )
    return out


def iter_parcels(max_records: int | None = None, geometry: dict | None = None, where: str = "1=1"):
    # The attribute layer is queried first (it is the one that supports the
    # spatial/where filters callers use), then the current geometry is fetched
    # for exactly the accounts it returned.
    attribute_rows = list(iter_all_features(
        BASE_URL, where=where, out_fields=OUT_FIELDS, return_geometry=True, out_sr=4326,
        max_records=max_records, geometry=geometry,
    ))
    current = _current_geometry([str(f["attributes"].get("ACCT"))
                                for f in attribute_rows if f["attributes"].get("ACCT")])

    for feat in attribute_rows:
        attrs = feat["attributes"]
        acct = str(attrs.get("ACCT"))
        block, lot = _parse_block_lot(attrs.get("LEGAL_2"))
        # Current geometry and its own measured area when available; the 2019
        # geometry and AREA_FEET only as a fallback, flagged by geometry_vintage
        # so a consumer can tell which it got rather than having to guess.
        if acct in current:
            geojson, area_sqft = current[acct]
            vintage = "current"
        else:
            geom = feat.get("geometry")
            geojson = (esri_rings_to_geojson_multipolygon(geom["rings"])
                       if geom and geom.get("rings") else None)
            area_sqft = attrs.get("AREA_FEET")
            vintage = "2019"
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": str(attrs.get("ACCT")),
            "owner_name_raw": attrs.get("TAXPANAME1"),
            "situs_address": _situs(attrs),
            "city": attrs.get("CITY") or None,
            "block": block,
            "lot": lot,
            "acreage": (area_sqft / 43560.0) if area_sqft else None,
            "geojson": geojson,
            "geometry_vintage": vintage,
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
