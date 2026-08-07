"""One-off script: commit covid 8534 tract 1 as an approximate placement --
a fully-verified SHAPE (from the deed's own courses, hand-transcribed and
independently walked) at an ESTIMATED, moderate-confidence position, not a
confirmed survey-tied anchor.

SHAPE: 16 courses read directly from Exhibit A (85.74-ac tract, S. McCracken
Survey Abstract 817, City and County of Denton). One genuine OCR ambiguity
resolved with total confidence: the deed's own first course was missing its
North/South prefix ("89 Degrees 39 Minutes 19 Seconds West"); testing both
readings against the traverse's own closure decided it -- "North 89:39:19
West" closes to 0.010 ft over an 8,496 ft perimeter (1:835,215) and computes
85.723 ac against the deed's own stated 85.74 ac (0.02% deviation). This is
the tightest verified traverse this project has produced.

POSITION: two automated LLM escalation attempts (Opus 5, Fable 5-- ~$45-50
combined) and two full recorder-portal chain-of-title searches (declarant
"Wolski", grantor "Ginnings") did not produce a single precise corner tie --
the historical adjoining tracts (a 20.113-ac Ginnings remainder, the old
McCracken-abstract raw acreage) have all been subdivided away with no
distinct current parcel left to tie to. What DOES corroborate the general
position, strongly: Royal Acres Addition's full current footprint (286
parcels) is 2,680 ft wide and 83.3 ac -- both within ~3% of this traverse's
own 2,757 ft width and 85.74-ac stated area -- and its own northernmost
edge sits at lat 33.25750, almost exactly on Forman Williamsburg Square's
real southern edge (33.25730), matching the deed's own description of the
tract's NW corner touching that subdivision's south line. That real,
structural agreement (not just aggregate stats) is what the anchor below is
built from -- an ESTIMATE, not a confirmed tie, since Hercules Lane's own
exact real-world path (the tract's south boundary) couldn't be isolated
precisely from the surrounding subdivision fabric.

anchor_lat: Forman Williamsburg's own real south edge (33.2572960734264),
    offset by the traverse's own NW-corner-to-POB vertical distance
    (1243.50 ft) to back-solve the POB's own latitude.
anchor_lon: Royal Acres' own real western extent (-97.122061247718, stable
    across both the full 286-parcel union and a rough Hercules-line filter),
    offset by the traverse's own westmost-vertex-to-POB horizontal distance
    (2043.15 ft) to back-solve the POB's own longitude.

Usage: python3 scripts/commit_covid8534_tract1_approximate.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import get_session
from app.gis.geocode_anchor import FT_PER_DEG_LAT, traverse_to_geojson
from app.gis.reconcile import reconcile_covenant
from app.parsing.legal_description.metes_bounds import Course, walk_traverse
import math

COVID, TRACT_NO = 8534, 1

# Real courses transcribed directly from Exhibit A (_textcache_final/8534_8534_D1414.pdf.json).
# Course 1's N/S prefix was missing in the source OCR -- resolved to North via
# independent closure verification (see module docstring).
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

FORMAN_WILLIAMSBURG_SOUTH_EDGE_LAT = 33.2572960734264
ROYAL_ACRES_WEST_EDGE_LON = -97.122061247718


def compute_geojson_and_diagnostics():
    result = walk_traverse(COURSES)
    vertices = result["vertices"]
    nw_corner_y = vertices[4][1]     # index 4: NW corner (touches Forman Williamsburg's south line)
    westmost_x = min(v[0] for v in vertices)  # index 3: westmost vertex

    anchor_lat = FORMAN_WILLIAMSBURG_SOUTH_EDGE_LAT - (nw_corner_y / FT_PER_DEG_LAT)
    ft_per_deg_lon = FT_PER_DEG_LAT * math.cos(math.radians(anchor_lat))
    anchor_lon = ROYAL_ACRES_WEST_EDGE_LON - (westmost_x / ft_per_deg_lon)

    geojson = traverse_to_geojson(vertices, anchor_lat, anchor_lon)
    print(f"  traverse: {len(COURSES)} courses, closure_error={result['closure_error_ft']:.3f} ft, "
          f"closure_ratio={result['closure_ratio']:.2e}, area={result['area_acres']:.3f} ac "
          f"(deed states 85.74 ac)")
    print(f"  anchor: lat={anchor_lat:.6f}, lon={anchor_lon:.6f}")
    return geojson


def main() -> None:
    geojson = compute_geojson_and_diagnostics()

    with get_session() as session:
        from app.gis.geocode_anchor import resolve_metes_bounds_approximate
        approx_result = resolve_metes_bounds_approximate(
            session, covid=COVID, course_text="", tract_no=TRACT_NO,
            anchor_lat=0.0, anchor_lon=0.0,  # unused when precomputed_geojson is given
            confidence=0.45,
            anchor_notes=(
                "Estimated position, not a confirmed tie: two LLM escalation attempts and two "
                "recorder-portal chain-of-title searches (declarant 'Wolski', grantor 'Ginnings') "
                "found no distinct current parcel matching the deed's own historical adjoiners "
                "(a 20.113-ac Ginnings remainder, the old McCracken-abstract raw acreage -- all "
                "since subdivided away). Position is instead estimated from Royal Acres Addition's "
                "real current footprint (286 parcels: 2,680 ft wide, 83.3 ac -- within ~3% of this "
                "traverse's own 2,757 ft width and 85.74-ac stated area) and its own northernmost "
                "edge sitting almost exactly on Forman Williamsburg Square's real southern edge "
                "(33.25750 vs 33.25730), matching the deed's own description of the tract's NW "
                "corner touching that subdivision's south line. Confidence reflects real, "
                "structural corroboration on two independent dimensions (width/area agreement AND "
                "edge alignment), but not a single precise corner tie -- Hercules Lane's own exact "
                "real-world path (the tract's south boundary) could not be isolated from the "
                "surrounding subdivision fabric."
            ),
            method="other",
            precomputed_geojson=geojson,
        )
        print("approx_result:", json.dumps(approx_result, indent=2, default=str))
        session.commit()

    with get_session() as session:
        result = reconcile_covenant(session, covid=COVID)
        session.commit()
    print("reconcile_covenant result:")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
