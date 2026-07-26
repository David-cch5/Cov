"""Llano County, TX parcel adapter (Llano Central Appraisal District data via
ArcGIS Online). Endpoint discovered through the ArcGIS Online catalog search
(BUILD_SPEC's fallback path); field mapping confirmed against the live service
(2026-07-24).

Quirk vs. Montgomery: `tract_or_lot` and `Block` exist as dedicated columns but
are inconsistently populated (frequently null even when the lot number is
plainly visible in `legal_desc`) -- confirmed by sampling the Escondido
Subdivision roster, where most records carry the lot only in `legal_desc`
("ESCONDIDO LT 121-B  0.770 AC"). So, like Tarrant/Dallas, this is a text match
against `legal_desc` (parsed via regex), not an exact attribute filter, despite
the schema nominally having a structured lot field.

Also worth knowing: Llano appears to record one master development under a
single `abs_subdv_cd` across differently-labeled phases (e.g. "ESCONDIDO",
"ESCONDIDO II", "ESCONDIDO PHASE 3" all share the same code) -- a deed that
just says "Escondido Subdivision" without a phase can match a lot number in
any of them, and lot numbers are only unique within a phase, not across the
whole code. Matching on the bare subdivision token ("ESCONDIDO") deliberately
casts across all phases rather than guessing which one.
"""
import re

from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://services.arcgis.com/3fXpNNO2cx0O3RtY/arcgis/rest/services/LlanoCADWebService/FeatureServer/0"
COUNTY_FIPS = "48299"

FIELD_MAPPING = {
    "apn": "prop_id",
    "owner_name": "file_as_name",
    "legal_desc": "legal_desc",
    "legal_desc2": "legal_desc2",
    "legal_desc3": "legal_desc3",
    "acreage": "legal_acreage",
    "abstract_subdivision_code": "abs_subdv_cd",
    "situs_num": "situs_num",
    "situs_street": "situs_street",
    "situs_city": "situs_city",
    "situs_zip": "situs_zip",
    "deed_volume": "Volume",
    "deed_page": "Page",
    "deed_date": "Deed_Date",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())

# Matches the lot token immediately following "LT " in legal_desc, e.g.
# "121-B", "C-31", "20D", "244-B", "207" -- but not multi-lot common-area
# descriptions ("LT PT OF C-14A, C-14B, ..."), which are left unparsed (lot=None)
# rather than guessed at.
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
            "block": None,  # not reliably populated; see module docstring
            "acreage": attrs.get("legal_acreage"),
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": " ".join(
                filter(None, [legal_desc, attrs.get("legal_desc2"), attrs.get("legal_desc3")])
            ),
        }


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Text match against legal_desc -- see module docstring for why this can't
    be an exact attribute filter here, and why the subdivision token is
    deliberately left phase-agnostic (matches "ESCONDIDO", "ESCONDIDO II",
    "ESCONDIDO PHASE 3", ... all at once). `block` accepted for interface
    parity but not used -- Llano's Block column isn't reliably populated."""
    token = subdivision_name.split(",")[0].strip().replace("'", "''")
    # first word only: "Escondido Subdivision" -> "ESCONDIDO", so the search
    # isn't accidentally narrowed to an exact phrase a phase label would break.
    token = token.split()[0] if token.split() else token
    where = f"UPPER(legal_desc) LIKE UPPER('%{token}%')"
    results = list(iter_parcels(where=where))
    wanted = {l.upper() for l in lots}
    return [r for r in results if r["lot"] and r["lot"].upper() in wanted]
