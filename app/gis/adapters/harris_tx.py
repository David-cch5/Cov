"""Harris County, TX parcel adapter (Harris County's own GIS server,
gis.hctx.net -- HCAD's parcel layer hosted directly on the county's domain).
Field mapping confirmed against the live service (2026-07-24).

IMPORTANT, corrected 2026-07-24 while re-resolving covid 7991: the subdivision
NAME lives in legal_dscr_2 ("GEORGE RICH", "SAGEMONT SEC 1", etc.), not
legal_dscr_1 -- legal_dscr_1 is always the "LT <n> BLK <n>" / "TR <n>" /
"RES <letter> BLK <n>" fragment. The original version of this adapter searched
legal_dscr_1 for the subdivision token, which could never match anything (confirmed
against covid 7991: "GEO RICH" / "GEO. RICH SUBDIVISION" appears nowhere in
legal_dscr_1 across the whole county, but is legal_dscr_2 on the exact parcel,
HCAD_NUM 0741070100001, that the covenant's own lots resolve to). The
structured LOT_NUM/BLK_NUM columns are also frequently NULL even when
legal_dscr_1 plainly states a lot/block (e.g. "LT 106 BLK 6" -> LOT_NUM=None) --
not reliable as a sole filter, so lot/block are parsed from legal_dscr_1 by
regex instead, same approach as the Llano/Nueces/Hunt/Kerr adapters use.

Harris also merges multiple original plat lots into a single tax parcel over
time (e.g. "LTS 1 2 & 3 BLK 2" is one HCAD account covering all three) --
query_by_subdivision_and_lots yields one dict per matched *lot*, not one per
parcel, so a merged parcel matching lots 1, 2, and 3 is returned three times
(same apn/geometry) rather than once with an ambiguous combined lot value.
This keeps it compatible with classifier.py's per-lot matched/missing set
logic; the geometry union in resolve_subdivision_plat_tract is idempotent to
the duplication (ST_Union of the same polygon twice is the same polygon).
"""
import re

from app.gis.adapters.base_arcgis import iter_all_features, esri_rings_to_geojson_multipolygon

BASE_URL = "https://www.gis.hctx.net/arcgis/rest/services/HCAD/Parcels/MapServer/0"
COUNTY_FIPS = "48201"

FIELD_MAPPING = {
    "apn": "HCAD_NUM",
    "owner_name": "owner_name_1",
    "legal_1": "legal_dscr_1", "legal_2": "legal_dscr_2",
    "legal_3": "legal_dscr_3", "legal_4": "legal_dscr_4",
    "block": "BLK_NUM", "lot": "LOT_NUM",
    "acreage": "Acreage",
    "situs_street": "site_str_name", "situs_num": "site_str_num",
    "situs_city": "site_city", "situs_zip": "site_zip",
}
OUT_FIELDS = ",".join(FIELD_MAPPING.values())

_LOTS_BLOCK_RE = re.compile(r"\bLTS?\.?\s*([\d\s,&-]+?)\s*\bBLK\.?\s*([\dA-Z]+)", re.IGNORECASE)
_LOT_TOKEN_RE = re.compile(r"\d+(?:-\d+)?")


def _parse_lots_block(legal_dscr_1: str | None) -> tuple[list[str], str | None]:
    """Parse every individual lot number and the block out of a Harris
    legal_dscr_1 fragment like 'LTS 1 2 & 3 BLK 2' or 'LT 749 BLK 19'.
    Returns ([], None) for fragments with no LT/BLK pattern (e.g. 'TR 6',
    'RES A BLK 1' -- reserves/tracts aren't platted lots)."""
    if not legal_dscr_1:
        return [], None
    m = _LOTS_BLOCK_RE.search(legal_dscr_1)
    if not m:
        return [], None
    lots: list[str] = []
    for tok in _LOT_TOKEN_RE.findall(m.group(1)):
        if "-" in tok:
            start, end = tok.split("-")
            lots.extend(str(i) for i in range(int(start), int(end) + 1))
        else:
            lots.append(tok)
    return lots, m.group(2)


def _situs(attrs: dict) -> str | None:
    parts = [attrs.get("site_str_num"), attrs.get("site_str_pfx"), attrs.get("site_str_name"), attrs.get("site_str_sfx")]
    joined = " ".join(str(p).strip() for p in parts if p)
    return joined or None


def iter_parcels(max_records: int | None = None, geometry: dict | None = None, where: str = "1=1"):
    for feat in iter_all_features(
        BASE_URL, where=where, out_fields=OUT_FIELDS, return_geometry=True, out_sr=4326,
        max_records=max_records, geometry=geometry,
    ):
        attrs = feat["attributes"]
        geom = feat.get("geometry")
        acreage = attrs.get("Acreage")
        try:
            acreage = float(acreage) if acreage not in (None, "") else None
        except ValueError:
            acreage = None
        parsed_lots, parsed_block = _parse_lots_block(attrs.get("legal_dscr_1"))
        yield {
            "county_fips": COUNTY_FIPS,
            "apn": str(attrs.get("HCAD_NUM")),
            "owner_name_raw": attrs.get("owner_name_1"),
            "situs_address": _situs(attrs),
            "city": attrs.get("site_city") or None,
            "zip_code": attrs.get("site_zip") or None,
            "lot": attrs.get("LOT_NUM") or (parsed_lots[0] if len(parsed_lots) == 1 else None),
            "lots": parsed_lots,
            "block": attrs.get("BLK_NUM") or parsed_block,
            "acreage": acreage,
            "geojson": esri_rings_to_geojson_multipolygon(geom["rings"]) if geom and geom.get("rings") else None,
            "recited_legal_description": " ".join(
                filter(None, [attrs.get("legal_dscr_1"), attrs.get("legal_dscr_2"),
                              attrs.get("legal_dscr_3"), attrs.get("legal_dscr_4")])
            ),
        }


def query_by_subdivision_and_lots(subdivision_name: str, lots: list[str], block: str | None = None):
    """Subdivision name lives in legal_dscr_2 -- see module docstring. Lot
    numbers are matched against legal_dscr_1's parsed lot list (not the
    unreliable LOT_NUM column); a parcel covering multiple requested lots
    (a historical merge) is yielded once per matching lot."""
    token = subdivision_name.split(",")[0].strip().replace("'", "''")
    where = f"UPPER(legal_dscr_2) LIKE UPPER('%{token}%')"
    results = list(iter_parcels(where=where))

    wanted = {l.upper() for l in lots}
    matches = []
    for r in results:
        if block and (r["block"] or "").upper() != block.upper():
            continue
        hit_lots = [l for l in r["lots"] if l.upper() in wanted]
        for lot in hit_lots:
            matches.append({**r, "lot": lot})
    return matches
