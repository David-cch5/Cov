"""One-off script: commit covid 8534 tract 1's CONFIRMED anchor -- replacing
the earlier approximate placement (scripts/commit_covid8534_tract1_approximate.py,
confidence 0.45, tract.approximate_geom only) with a real, independently
cross-checked corner tie (confidence 0.85, tract.geom).

WHAT CHANGED: two automated LLM escalation attempts and two recorder-portal
chain-of-title searches all tried to tie this tract to a NAMED PARTY (the
declarant "Wolski", the grantor "Ginnings") and failed -- every historical
adjoiner has been subdivided away with no distinct current parcel left. The
actual answer was sitting in the deed's own Exhibit A the whole time, missed
because prior attempts jumped straight to name-based GIS/recorder searches
instead of reading the full POB call carefully:

    "BEGINNING for the Southeast corner of the tract being described herein
    at a PK Nail set for the Southeast corner of said Ginnings tract on the
    Westerly right-of-way line of F.M Highway 428 (Sherman Drive) and in or
    near the centerline of Hercules Drive"

This ties the POB to a real STREET INTERSECTION, not a party name --
Sherman Drive is FM 428 (stated explicitly in the deed itself) and "Hercules
Drive" is this same deed's own inconsistent spelling of Hercules Lane (Denton
GIS's own situsStreetName vocabulary near this tract has only 'HERCULES' and
'HERCULES WAY' -- confirmed no second, distinct "Hercules Drive" exists).

REAL TIES USED (all independently verifiable, none fabricated):
  1. OpenStreetMap's own road-network topology has a shared node where
     Hercules Lane's easternmost segment (way 9970081) and East Sherman
     Drive/FM 428 (way 1190631857) meet: lat=33.2539423, lon=-97.1124473.
     This is the real centerline intersection the deed's POB sits "near."
  2. Two real Denton CAD parcels front the west ROW line of Sherman Drive
     bracketing that latitude: APN 108004 (Sherman Drive Church of Christ,
     2321 Sherman) just south, and APN 744897 (SHERCROSS HOA INC, 1911
     Hercules -- its own name is "Sher[man]+[Her]cross[ing]", independent
     corroboration this HOA parcel sits at this exact real-world corner)
     just north. Interpolating their real boundary vertices at the
     intersection's exact latitude gives the actual west-ROW-line longitude:
     -97.112682 -- a ~71.6 ft offset west of the centerline node, consistent
     with the church parcel's own direct offset (~68 ft) computed the same
     way independently.
  3. This POB, walked through all 16 courses, independently reproduces THREE
     more real ties along the traverse with no further free parameters:
       - The NW corner (vertex 4) lands at lat 33.257359 -- 23 ft from
         Forman Williamsburg Square's own real, independently-established
         south edge (33.2572960734264, a real recorded plat boundary),
         matching the deed's own call that this corner sits "in the South
         line of Forman Williamsburg Square."
       - A second point on the tract's own east line (vertex 13, the west
         line of Sherman Drive far to the north) lands ~81 ft west of
         Sherman Drive's real OSM centerline at that latitude -- consistent
         with the ~68-72 ft ROW-half-width found independently at the POB
         itself, ~2,860 ft away along the traverse.
       - Vertex 7 (NE corner of the severed 16.83-ac tract, deed-called as
         "in ... the South line of State Highway Loop 288") lands within
         ~65 ft in latitude of Loop 288's own real OSM centerline node
         (33.2597909, -97.1146463) after propagating through 7 courses.
  Three independent, unrelated real-world features (a subdivision plat edge,
  a highway ROW, another highway's ROW) all corroborating a POB anchored
  from nothing but a street-intersection call is real convergent evidence,
  not coincidence -- this is what "confirmed" means here, short of an actual
  surveyed monument reading.

Confidence 0.85 (matching this project's own precedent for a comparably-
corroborated tie, e.g. scripts/manual_commit_covid4780_tract1.py's
sibling-tract tie) -- not higher, since the ROW-line position itself is
still derived from nearby parcel boundaries rather than a stated ROW width
or a directly-surveyed monument, and the deed's own POB call says "in or
near," not "at."

Uses the exact same code path (classify_metes_and_bounds_tract,
reconcile_covenant) the app's own automated pipeline uses.

Usage: python3 scripts/manual_commit_covid8534_tract1_confirmed.py
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.db.repository import insert_source
from app.db.session import get_session
from app.gis.classifier import classify_metes_and_bounds_tract
from app.gis.reconcile import reconcile_covenant
from app.parsing.legal_description.metes_bounds import Course, walk_traverse

COVID, TRACT_NO = 8534, 1
FT_PER_DEG_LAT = 364000.0

# Real courses transcribed directly from Exhibit A (_textcache_final/8534_8534_D1414.pdf.json).
# Course 1's N/S prefix was missing in the source OCR -- resolved to North via
# independent closure verification (0.010 ft over an 8,496 ft perimeter, 1:835,215 --
# see scripts/commit_covid8534_tract1_approximate.py's own docstring for that test).
_C = lambda ns, d, m, s, ew, dist: Course(ns=ns, degrees=d, minutes=m, seconds=s, ew=ew, distance_ft=dist)
COURSES = [
    _C("North", 89, 39, 19, "West", 1709.42),
    _C("North", 1, 10, 28, "East", 410.95),
    _C("North", 88, 49, 32, "West", 342.26),
    _C("North", 1, 10, 28, "East", 815.51),
    _C("South", 87, 38, 25, "East", 107.80),
    _C("South", 87, 41, 46, "East", 798.00),
    _C("North", 2, 19, 8, "East", 857.07),
    _C("South", 78, 30, 47, "East", 389.24),   # curve chord (radius 5619.58 ft, arc 389.32 ft)
    _C("South", 69, 41, 45, "East", 684.20),
    _C("South", 59, 38, 23, "East", 586.57),
    _C("South", 61, 52, 33, "East", 285.22),
    _C("South", 14, 39, 23, "East", 76.18),
    _C("South", 31, 34, 17, "West", 129.57),
    _C("South", 29, 39, 51, "West", 603.13),
    _C("South", 29, 39, 53, "West", 121.25),
    _C("South", 29, 39, 52, "West", 579.96),
]

# Real OSM road-network node: Hercules Lane's own east end (way 9970081) meets
# East Sherman Drive/FM 428 (way 1190631857) at this exact shared coordinate.
SHERMAN_HERCULES_INTERSECTION_LAT = 33.2539423
SHERMAN_HERCULES_INTERSECTION_LON = -97.1124473

# Real Denton CAD parcel boundary vertices fronting Sherman Drive's west ROW
# line, bracketing the intersection's own latitude (used to interpolate the
# real ROW line position at that exact latitude, not an assumed ROW width):
_CHURCH_PT = (-97.112655, 33.253907)     # APN 108004, Sherman Drive Church of Christ (south)
_SHERCROSS_PT = (-97.112748, 33.254027)  # APN 744897, SHERCROSS HOA INC (north)


def compute_geojson_and_diagnostics() -> dict:
    result = walk_traverse(COURSES)
    print(f"  traverse: {len(COURSES)} courses, closure_error={result['closure_error_ft']:.3f} ft, "
          f"closure_ratio={result['closure_ratio']:.2e}, area={result['area_acres']:.3f} ac "
          f"(deed states 85.74 ac)")

    lon0, lat0 = _CHURCH_PT
    lon1, lat1 = _SHERCROSS_PT
    frac = (SHERMAN_HERCULES_INTERSECTION_LAT - lat0) / (lat1 - lat0)
    anchor_lon = lon0 + frac * (lon1 - lon0)
    anchor_lat = SHERMAN_HERCULES_INTERSECTION_LAT

    ft_per_deg_lon = FT_PER_DEG_LAT * math.cos(math.radians(anchor_lat))
    row_offset_ft = (SHERMAN_HERCULES_INTERSECTION_LON - anchor_lon) * ft_per_deg_lon
    print(f"  anchor (POB): lat={anchor_lat:.6f}, lon={anchor_lon:.6f} "
          f"({row_offset_ft:.1f} ft west of the real Sherman/Hercules centerline node)")

    vertices = result["vertices"]
    pob_x, pob_y = vertices[0]
    ring = []
    for x, y in vertices:
        lon = anchor_lon + (x - pob_x) / ft_per_deg_lon
        lat = anchor_lat + (y - pob_y) / FT_PER_DEG_LAT
        ring.append([lon, lat])
    ring[-1] = ring[0]

    nw_lat = anchor_lat + (vertices[4][1] - pob_y) / FT_PER_DEG_LAT
    print(f"  cross-check: NW corner lat={nw_lat:.6f} vs Forman Williamsburg Square's real "
          f"south edge 33.257296 (diff {(nw_lat - 33.2572960734264) * FT_PER_DEG_LAT:.1f} ft)")

    return {"type": "MultiPolygon", "coordinates": [[ring]]}


def main() -> None:
    geojson = compute_geojson_and_diagnostics()

    with get_session() as session:
        source_id = insert_source(
            session, source_type="manual_entry",
            reference=(
                "Street-intersection tie read directly from the deed's own POB call: SE corner "
                "on the west ROW line of FM 428 (Sherman Drive) at/near the centerline of "
                "Hercules Drive/Lane. Anchored via a real OSM road-network node (Hercules Lane x "
                "Sherman Dr, lat=33.2539423 lon=-97.1124473) and the real Denton CAD parcel "
                "boundaries of the two parcels bracketing that latitude on Sherman's west ROW "
                "line (APN 108004 Sherman Drive Church of Christ, APN 744897 SHERCROSS HOA INC). "
                "Independently cross-checked at 3 further points along the 16-course traverse: "
                "NW corner within 23 ft of Forman Williamsburg Square's real south edge; a second "
                "point on the east line within ~81 ft of Sherman Drive's real centerline "
                "(consistent with the ~70 ft ROW half-width found independently at the POB); NE "
                "corner within ~65 ft (latitude) of Loop 288's real centerline. Closure error "
                "0.010 ft over an 8,496 ft perimeter (1:835,215); area 85.723 ac vs deed's stated "
                "85.74 ac (0.02% deviation)."
            ),
            confidence=0.85,
        )
        session.execute(
            text("""
                INSERT INTO tract (covid, tract_no, geom, boundary_resolution_method, source_id, updated_at)
                VALUES (:covid, :tract_no, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326), 'metes_and_bounds_traverse', :source_id, now())
                ON CONFLICT (covid, tract_no) DO UPDATE SET
                    geom = EXCLUDED.geom, boundary_resolution_method = EXCLUDED.boundary_resolution_method,
                    source_id = EXCLUDED.source_id, updated_at = now()
            """),
            {"covid": COVID, "tract_no": TRACT_NO, "geojson": json.dumps(geojson), "source_id": source_id},
        )

        classify_result = classify_metes_and_bounds_tract(session, covid=COVID, tract_no=TRACT_NO)
        print("classify_result:", json.dumps(classify_result, indent=2, default=str))

        existing = session.execute(
            text("SELECT review_reason FROM covenant WHERE covid = :covid"), {"covid": COVID}
        ).scalar() or ""
        stale_note = (
            "Metes-and-bounds tract shape validated (see tract.approximate_geom_notes) but not "
            "yet anchored to a real surveyed position -- placement is a rough geocode, not a confirmed boundary."
        )
        cleaned = existing.replace(stale_note, "").strip("; ").strip()
        new_note = (
            "ANCHOR RESOLVED (manual, tier=named_feature_tie, confidence=0.85): tract 1 anchored "
            "to a real, independently cross-checked position (deed's own POB call to the Sherman "
            "Drive/Hercules Lane street intersection, verified against real OSM road topology + "
            "Denton CAD parcel boundaries, cross-checked at 3 further points along the traverse) "
            "and spatially classified against live parcel data. Supersedes the earlier approximate "
            "placement (confidence 0.45, subdivision-footprint proxy) -- resolved instead by "
            "reading the deed's full Exhibit A text carefully rather than name-based GIS/recorder "
            "search, which could never succeed once the historical adjoiners were subdivided away."
        )
        session.execute(
            text("UPDATE covenant SET review_reason = :reason, updated_at = now() WHERE covid = :covid"),
            {"covid": COVID, "reason": f"{cleaned}; {new_note}" if cleaned else new_note},
        )
        session.commit()

    with get_session() as session:
        result = reconcile_covenant(session, covid=COVID)
        session.commit()
    print("reconcile_covenant result:")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
