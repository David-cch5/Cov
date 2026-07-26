"""Rough, explicitly-flagged placement for metes-and-bounds tracts that have no
confirmed real-world anchor.

This is deliberately the "leave it unresolved but useful" path, not a substitute
for real anchoring. It writes tract.approximate_geom (never tract.geom -- see
migration 0008's docstring for why those are kept separate) and sets the
covenant to needs_review. Picking the anchor point itself is a human/LLM judgment
call made once per covenant (which of several same-named-road geocode hits is
the right one) -- this module takes that anchor as an input rather than trying
to automate the disambiguation, consistent with never silently guessing a
location per CLAUDE.md's never-fabricate rule.
"""
import json
import math

import requests
from sqlalchemy import text

from app.parsing.legal_description.metes_bounds import walk_traverse
from app.parsing.legal_description.metes_bounds_llm import extract_courses_llm, to_course_objects

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires a descriptive User-Agent identifying the
# application; requests without one are liable to be blocked.
_USER_AGENT = "covenant-processing-system/0.1 (research5@covenantclearinghouse.com)"

FT_PER_DEG_LAT = 364000.0  # standard surveying approximation, adequate at this scale


def geocode(query: str) -> list[dict]:
    """Free-text geocode via Nominatim. Returns every candidate match --
    disambiguating between them (e.g. multiple segments of the same rural
    highway) is left to the caller, not guessed here."""
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": query, "format": "jsonv2", "limit": 5},
        headers={"User-Agent": _USER_AGENT},
        timeout=15,
    )
    resp.raise_for_status()
    return [{"lat": float(r["lat"]), "lon": float(r["lon"]), "display_name": r["display_name"]} for r in resp.json()]


def parcel_centroid(adapter_module, where: str) -> dict:
    """Fetch a single matched parcel's centroid, for anchoring a tract whose
    Point of Beginning ties to a corner of a real, currently-existing platted
    parcel (a reserve, lot, or block named in the tie-call) rather than a
    free-text place name. The centroid is a proxy for the exact corner --
    good to roughly the parcel's own extent, not exact -- disambiguating
    which parcel is the right one is left to the caller (pass a `where`
    clause specific enough to match exactly one), same principle as
    geocode()'s caller-disambiguates-candidates design."""
    from app.gis.adapters.base_arcgis import query_features
    r = query_features(adapter_module.BASE_URL, where=where, out_fields="*", return_geometry=True)
    feats = r.get("features", [])
    if len(feats) != 1:
        raise RuntimeError(f"expected exactly 1 parcel match for anchor where={where!r}, got {len(feats)}")
    ring = feats[0]["geometry"]["rings"][0]
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return {"lon": sum(lons) / len(lons), "lat": sum(lats) / len(lats), "attributes": feats[0]["attributes"]}


def traverse_to_geojson(vertices_ft: list[tuple[float, float]], anchor_lat: float, anchor_lon: float) -> dict:
    """Translate a local (feet, arbitrary-origin) traverse -- whose first vertex
    is the Point of Beginning -- so that vertex lands at (anchor_lat, anchor_lon),
    using a flat-earth degree-per-foot approximation. Fine for a single tract's
    extent; not a projection suitable for anything larger."""
    ft_per_deg_lon = FT_PER_DEG_LAT * math.cos(math.radians(anchor_lat))
    pob_x, pob_y = vertices_ft[0]
    ring = []
    for x, y in vertices_ft:
        lon = anchor_lon + (x - pob_x) / ft_per_deg_lon
        lat = anchor_lat + (y - pob_y) / FT_PER_DEG_LAT
        ring.append([lon, lat])
    return {"type": "MultiPolygon", "coordinates": [[ring]]}


def resolve_metes_bounds_approximate(
    session, covid: int, course_text: str, anchor_lat: float, anchor_lon: float,
    anchor_notes: str, tract_no: int = 1, confidence: float = 0.15,
    method: str = "geocoded_point_of_beginning",
) -> dict:
    """Extract courses via LLM, walk the traverse, place it at the given anchor,
    and persist as tract.approximate_geom -- geom stays NULL and the covenant is
    flagged needs_review, since this is a shape validation + rough placement,
    never a confirmed boundary."""
    extraction = extract_courses_llm(course_text)
    courses = to_course_objects(extraction)
    traverse = walk_traverse(courses)
    geojson = traverse_to_geojson(traverse["vertices"], anchor_lat, anchor_lon)

    closure_ratio = traverse["closure_ratio"]
    closure_display = f"1:{round(1 / closure_ratio)}" if closure_ratio else "n/a (zero perimeter)"
    notes = (
        f"Shape validated via LLM-assisted course extraction + deterministic COGO traverse: "
        f"{len(courses)} courses, closure ratio {closure_display}, computed area "
        f"{traverse['area_acres']:.2f} acres. NOT anchored to a surveyed position -- {anchor_notes}"
    )

    session.execute(
        text("""
            INSERT INTO tract (covid, tract_no, geom, approximate_geom, approximate_geom_method,
                                approximate_geom_confidence, approximate_geom_notes, updated_at)
            VALUES (:covid, :tract_no, NULL, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326),
                    :method, :confidence, :notes, now())
            ON CONFLICT (covid, tract_no) DO UPDATE SET
                geom = NULL,
                approximate_geom = EXCLUDED.approximate_geom,
                approximate_geom_method = EXCLUDED.approximate_geom_method,
                approximate_geom_confidence = EXCLUDED.approximate_geom_confidence,
                approximate_geom_notes = EXCLUDED.approximate_geom_notes,
                updated_at = now()
        """),
        {"covid": covid, "tract_no": tract_no, "geojson": json.dumps(geojson),
         "method": method, "confidence": confidence, "notes": notes},
    )
    session.execute(
        text("""
            UPDATE covenant SET status = 'needs_review', review_reason =
                'Metes-and-bounds tract shape validated (see tract.approximate_geom_notes) but not '
                'yet anchored to a real surveyed position -- placement is a rough geocode, not a confirmed boundary.'
            WHERE covid = :covid
        """),
        {"covid": covid},
    )

    return {
        "courses_extracted": len(courses),
        "closure_ratio": closure_ratio,
        "area_acres": traverse["area_acres"],
        "extraction_confidence": extraction.get("confidence"),
        "extraction_notes": extraction.get("notes"),
    }
