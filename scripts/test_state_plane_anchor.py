"""Smoke test for app/gis/state_plane_anchor.py's traverse_to_geojson_via_parcel_ties
-- a third metes-and-bounds anchoring path (alongside a stated State Plane POB
coordinate and a shared-corner registration onto an already-anchored sibling
tract), generalized from the real fix built live against covid 8245
(2026-07-28/29): its original tract.geom was shifted enough to miss its own
two real parcels and instead spatially catch 8 unrelated ones, traced to an
incomplete _textcache_final copy of the deed's Exhibit A (missing the opening
courses -- the full text was in _textcache, not _final). Re-derived from the
complete metes-and-bounds text and anchored to 4 real corners of the
adjoining, already-platted Oak Ridge North Sec. 5 lots the deed's own course
3 explicitly ties to.

Usage: python3 scripts/test_state_plane_anchor.py
"""
import json
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import get_session
from app.gis.state_plane_anchor import traverse_to_geojson_via_parcel_ties
from app.parsing.legal_description.metes_bounds import Course, walk_traverse

# covid 8245's real deed courses (Charles Eisterwall Survey, Abstract 191, Montgomery
# County) -- the CURRENT re-survey bearings/distances, not the deed's own historical
# "Deed call" alternates.
COURSES = [
    Course(ns="North", degrees=12, minutes=20, seconds=19, ew="West", distance_ft=420.10),
    Course(ns="North", degrees=77, minutes=39, seconds=41, ew="East", distance_ft=368.28),
    Course(ns="South", degrees=40, minutes=23, seconds=0, ew="East", distance_ft=447.31),
    Course(ns="South", degrees=48, minutes=36, seconds=18, ew="West", distance_ft=51.97, is_curve=True),
    Course(ns="South", degrees=77, minutes=39, seconds=10, ew="West", distance_ft=533.16),
]

# The deed's own stated cumulative tie distances along course 3 (47.81/147.81/247.81/
# 347.81 ft), corresponding to the real, shared corners of Oak Ridge North Sec. 5 lots
# 527/528, 528/529, 529/530, 530/531 -- confirmed by direct coincidence between each
# pair's own real parcel geometry (fetched live from Montgomery's ArcGIS service).
REAL_TIES_LONLAT = [
    (-95.4464637, 30.1520429),
    (-95.4462675, 30.1518271),
    (-95.4460713, 30.1516114),
    (-95.445875, 30.1513956),
]
TIE_CUM_DISTANCES_FT = [47.81, 147.81, 247.81, 347.81]


def test_traverse_closes_tightly() -> None:
    """The deed's own current-survey courses (not its historical 'Deed call'
    alternates) close to within 0.01 ft over a 1820-ft perimeter -- confirms
    these are the correct courses before anchoring them anywhere."""
    result = walk_traverse(COURSES)
    assert result["closure_error_ft"] < 0.01, result
    assert abs(result["area_acres"] - 4.6055) < 0.02, result
    print("PASS: walk_traverse (covid 8245 courses) -> closes to within 0.01 ft, "
          "area matches the deed's stated 4.6055 ac")


def test_anchor_via_parcel_ties_matches_real_parcels() -> None:
    """The corrected polygon (built via traverse_to_geojson_via_parcel_ties)
    must dominantly overlap the tract's two real parcels (APN 451910, 41116)
    and have negligible overlap with the parcels the original, mis-anchored
    geometry wrongly matched instead."""
    traverse = walk_traverse(COURSES)
    vertices = traverse["vertices"][:-1]  # drop the duplicate closing vertex
    v2, v3 = vertices[2], vertices[3]  # NE corner (start of course 3), SE corner (end)

    local_ties = []
    for d in TIE_CUM_DISTANCES_FT:
        t = d / COURSES[2].distance_ft
        local_ties.append((v2[0] + t * (v3[0] - v2[0]), v2[1] + t * (v3[1] - v2[1])))

    geojson = traverse_to_geojson_via_parcel_ties(vertices, local_ties, REAL_TIES_LONLAT, anchor_lat=30.152)

    with get_session() as session:
        area = session.execute(text("""
            SELECT ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(:gj),4326)::geography) / 4046.8564224 AS acres,
                   ST_IsValid(ST_SetSRID(ST_GeomFromGeoJSON(:gj),4326)) AS valid
        """), {"gj": json.dumps(geojson)}).fetchone()
        assert area.valid, area
        assert abs(area.acres - 4.6055) < 0.05, area

        overlaps = {}
        for apn in ["451910", "41116", "129590", "41103"]:
            row = session.execute(text("""
                SELECT ST_Area(ST_Intersection(ST_SetSRID(ST_GeomFromGeoJSON(:gj),4326), p.geom)::geography)
                       / NULLIF(ST_Area(p.geom::geography), 0) AS overlap_fraction
                FROM parcel p WHERE p.county_fips = '48339' AND p.apn = :apn
            """), {"gj": json.dumps(geojson), "apn": apn}).fetchone()
            overlaps[apn] = float(row.overlap_fraction or 0)

    assert overlaps["451910"] > 0.9, overlaps   # the real Reserve A equivalent
    assert overlaps["41116"] > 0.9, overlaps    # the real Reserve B equivalent
    assert overlaps["129590"] < 0.01, overlaps  # wrongly matched by the original, mis-anchored geometry
    assert overlaps["41103"] < 0.01, overlaps   # likewise
    print("PASS: traverse_to_geojson_via_parcel_ties (covid 8245) -> corrected polygon "
          "dominantly overlaps the 2 real parcels, negligibly overlaps the 2 wrongly-matched ones")


if __name__ == "__main__":
    test_traverse_closes_tightly()
    test_anchor_via_parcel_ties_matches_real_parcels()
    print("\nall state_plane_anchor smoke tests passed")
