"""Re-anchor covid 5838 tract 1 from its own deed traverse, net of the six
SAVE AND EXCEPT tracts.

WHAT WAS WRONG
tract.geom claimed boundary_resolution_method='metes_and_bounds_traverse' but
held a parcel union: 6 disjoint parts, 96 points, a 27,922 ft perimeter and
303.893 ac, sourced from a Nueces CAD spatial query. The deed's own traverse is a
single 17-point ring, 21,571.8 ft, 318.778 ac against a stated 318.779. It was
the only tract in the database whose geometry contradicted its stated method, and
because it was built from parcels it silently omitted the carve-out land rather
than excepting it -- the polygon carried zero interior rings, so nothing had ever
been subtracted.

HOW THE POINT OF BEGINNING IS FIXED, WITHOUT GUESSING
Tract 1's POB is "a 3/4 inch iron bolt found for the northwest corner of an 8.720
acre tract ... same being an interior corner of a 289.6 acre tract". It recites no
State Plane coordinate and no NGS tie of its own, so it cannot be placed directly.

Two of the SAVE AND EXCEPT tracts reach it. Each is independently anchored off NGS
monument SF 010 (PID AH1674) by its own monument tie, and each then ties that same
bolt from its own north corner:

    1.582 ac tract:  S54°34'21"E 119.74 ft
    3.103 ac tract:  S54°34'21"E 105.56 ft, thence N35°24'54"E 50.00 ft

These are genuinely independent paths -- different traverses, different ties,
different tie geometry -- and they land 0.017 ft apart. That agreement is the
check; the midpoint is used. The deed's bearings are Texas South Zone grid
azimuths (proven by the monument cross-check closing at 0.007°), so the traverse
walks straight into EPSG:2279 with no rotation solve.

WHY THE STORED GEOMETRY IS NET, NOT GROSS
Saved-and-excepted land is not encumbered, and tract.geom is what
classify_metes_and_bounds_tract enumerates parcels against. Storing the gross
traverse would pull in 120 SUNFLOWER BEACH parcels that sit inside the carve-outs
-- every one of the 120 overlaps a carve-out, which is exactly why they must not
be counted. Subtracting the carve-outs removes all 120.

RESULT -- the parcel census does not move
    gross traverse   318.787 ac
    net of carve-outs 304.190 ac   (old parcel-union footprint: 303.893 ac)
    census parcels    630 before, 630 after, 0 added
    4.048 ac of the old footprint falls outside the deed line -- boundary sliver
    only (0.1-15% edges of large KM BEACH / KM LINKS parcels), no parcel leaves.

So this changes no fee liability. It replaces a parcel-derived approximation with
the deed's own boundary and makes the provenance honest.

Usage: python3 scripts/reanchor_covid5838_tract1.py [--commit]
"""
import json
import math
import re
import sys

sys.path.insert(0, ".")

from pyproj import CRS, Transformer
from shapely.geometry import Polygon, mapping
from sqlalchemy import text

from app.db.repository import insert_source
from app.db.review_notes import merge_tagged_note
from app.db.session import get_session
from app.gis.ngs import find_monuments
from app.gis.state_plane_anchor import anchor_by_ngs_monument_tie
from app.ingestion.walk import get_deed_text
from app.parsing.legal_description.metes_bounds import (
    extract_courses,
    repair_quadrant_by_closure,
    walk_traverse,
)
from app.parsing.legal_description.monument_ties import extract_ngs_monument_ties

COVID, TRACT_NO, COUNTY_FIPS, GRID_EPSG = 5838, 1, "48355", 2279
# Search box around Mustang Island / Port Aransas. A tie runs thousands of feet,
# so the monument is routinely well outside the tract itself.
NGS_BBOX = {"min_lat": 27.70, "max_lat": 27.90, "min_lon": -97.16, "max_lon": -97.00}
# Each carve-out's own tie to the 3/4 inch iron bolt, read from the deed.
BOLT_TIES = {
    1.582: [("S", 54, 34, 21, "E", 119.74)],
    3.103: [("S", 54, 34, 21, "E", 105.56), ("N", 35, 24, 54, "E", 50.00)],
}
MAX_BOLT_DISAGREEMENT_FT = 1.0


def _offset(ns, deg, minute, sec, ew, dist):
    """(east, north) offset in feet for a grid bearing."""
    a = deg + minute / 60 + sec / 3600
    az = {("N", "E"): a, ("S", "E"): 180 - a, ("S", "W"): 180 + a, ("N", "W"): 360 - a}[(ns, ew)]
    return dist * math.sin(math.radians(az)), dist * math.cos(math.radians(az))


def build() -> dict:
    with get_session() as session:
        deed = " ".join((get_deed_text(session, COVID) or "").split())
    sae = deed.find("SAVE AND EXCEPT THE FOLLOWING")
    ties = extract_ngs_monument_ties(deed)
    monuments = find_monuments({t.designation for t in ties}, NGS_BBOX)

    to_grid = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_epsg(GRID_EPSG), always_xy=True)
    from_grid = Transformer.from_crs(CRS.from_epsg(GRID_EPSG), CRS.from_epsg(4326), always_xy=True)

    # --- every SAVE AND EXCEPT tract, anchored off its own monument tie.
    # Segment on each tract's own "containing N acres": the 3.282 ac tract's
    # header is missing from the OCR and the numbering skips 6, so a
    # header-driven split loses a whole carve-out.
    carve_outs, prev = {}, sae
    for m in re.finditer(r"containing\s+([\d.,]+)\s+acres", deed[sae:]):
        end = sae + m.end()
        acres = float(m.group(1).replace(",", ""))
        courses, _ = repair_quadrant_by_closure(extract_courses(deed[prev:end]))
        placed = anchor_by_ngs_monument_tie(
            walk_traverse(courses)["vertices"],
            [t for t in ties if prev <= t.position < end], monuments)
        if not placed["verified"]:
            raise SystemExit(f"carve-out {acres} ac did not verify: {placed}")
        ring = placed["geojson"]["coordinates"][0][0]
        carve_outs[acres] = {"geojson": placed["geojson"],
                             "pob_grid": to_grid.transform(*ring[0]),
                             "monument": placed["monument"]}
        prev = end
    if len(carve_outs) != 6:
        raise SystemExit(f"expected 6 SAVE AND EXCEPT tracts, found {len(carve_outs)}")

    # --- the 3/4 inch iron bolt, reached independently from two carve-outs
    bolts = {}
    for acres, legs in BOLT_TIES.items():
        east, north = carve_outs[acres]["pob_grid"]
        for leg in legs:
            de, dn = _offset(*leg)
            east, north = east + de, north + dn
        bolts[acres] = (east, north)
    (a, pa), (b, pb) = bolts.items()
    disagreement = math.dist(pa, pb)
    if disagreement > MAX_BOLT_DISAGREEMENT_FT:
        raise SystemExit(f"the two paths to the bolt disagree by {disagreement:.3f} ft "
                         f"-- refusing to anchor on an unconfirmed corner")
    pob = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)

    # --- walk tract 1's own courses from the bolt, in grid coordinates
    start = deed.find("BEGINNING at a 3/4 inch iron bolt")
    end = start + re.search(r"containing\s+([\d.,]+)\s+acres", deed[start:sae]).end()
    courses = extract_courses(deed[start:end])
    walked = walk_traverse(courses)
    east, north, ring = pob[0], pob[1], [pob]
    for course in courses:
        az = math.radians(course.azimuth_degrees)
        east += course.distance_ft * math.sin(az)
        north += course.distance_ft * math.cos(az)
        ring.append((east, north))
    ring[-1] = ring[0]                       # GEOS needs a bit-identical close
    gross = Polygon([from_grid.transform(x, y) for x, y in ring])

    return {"gross": mapping(gross), "carve_outs": carve_outs, "courses": len(courses),
            "walked_acres": walked["area_acres"], "closure_ft": walked["closure_error_ft"],
            "bolt_disagreement_ft": disagreement, "monuments": monuments}


def main(commit: bool) -> None:
    built = build()
    print(f"  tract 1 traverse : {built['courses']} courses, {built['walked_acres']:.3f} ac "
          f"(deed 318.779), closure {built['closure_ft']:.3f} ft")
    print(f"  bolt cross-check : two independent paths agree to "
          f"{built['bolt_disagreement_ft']:.3f} ft")

    carves = [json.dumps(v["geojson"]) for v in built["carve_outs"].values()]
    with get_session() as session:
        net = session.execute(text("""
            WITH g AS (SELECT ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:gross), 4326)) geom),
                 c AS (SELECT ST_Union(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(x), 4326))) geom
                       FROM unnest(CAST(:carves AS text[])) x)
            SELECT ST_AsGeoJSON(ST_Multi(ST_Difference(g.geom, c.geom))) FROM g, c
        """), {"gross": json.dumps(built["gross"]), "carves": carves}).scalar()

        stats = session.execute(text("""
            WITH n AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON(:net), 4326) geom)
            SELECT ROUND((ST_Area(geom::geography) / 4046.856)::numeric, 3) acres,
                   ST_NumGeometries(geom) parts, ST_NPoints(geom) npts, ST_IsValid(geom) valid
            FROM n
        """), {"net": net}).mappings().one()
        print(f"  net of carve-outs: {stats['acres']} ac, {stats['parts']} parts, "
              f"{stats['npts']} points, valid={stats['valid']}")

        kept = session.execute(text("""
            WITH n AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON(:net), 4326) geom)
            SELECT COUNT(*) FROM (
                SELECT DISTINCT p.apn, p.geom FROM parcel p
                JOIN parcel_covenant pc ON pc.apn = p.apn AND pc.county_fips = p.county_fips
                WHERE pc.covid = :covid AND pc.tract_no = :tract_no) p, n
            WHERE ST_Area(ST_Intersection(p.geom, n.geom)) / NULLIF(ST_Area(p.geom), 0) > 0.5
        """), {"net": net, "covid": COVID, "tract_no": TRACT_NO}).scalar()
        print(f"  census parcels >50% inside the new polygon: {kept}")
        if kept != 630:
            raise SystemExit(f"expected all 630 census parcels retained, got {kept}")

        if not commit:
            print("\n  dry run -- pass --commit to write")
            return

        source_id = insert_source(
            # New source_type: the geometry is derived from the deed's own field
            # notes, not retrieved from any external service. The NGS lookup only
            # placed it; the shape is the document's.
            session, source_type="deed_traverse",
            reference=(f"deed traverse anchored via NGS SF 010 (AH1674) through the "
                       f"1.582/3.103 ac SAVE AND EXCEPT tracts' shared 3/4 inch iron bolt; "
                       f"net of all six excepted tracts"),
            confidence=0.95,
        )
        session.execute(text("""
            UPDATE tract SET geom = ST_SetSRID(ST_GeomFromGeoJSON(:net), 4326),
                             source_id = :source_id, updated_at = now()
            WHERE covid = :covid AND tract_no = :tract_no
        """), {"net": net, "source_id": source_id, "covid": COVID, "tract_no": TRACT_NO})

        note = (
            "TRACT 1 GEOMETRY PROVENANCE (automated): RESOLVED. tract.geom was a Nueces CAD "
            "parcel union (6 parts | 96 points | 303.893 ac) despite claiming "
            "metes_and_bounds_traverse, and carried no interior rings, so the six SAVE AND "
            "EXCEPT tracts had never been subtracted. It is now the deed's own 16-course "
            "traverse (318.778 ac walked against a stated 318.779, closure 0.017 ft, "
            "1:1,257,690), net of all six excepted tracts, at 304.190 ac. The Point of "
            "Beginning -- a 3/4 inch iron bolt reciting no coordinate and no monument tie of "
            "its own -- was fixed from the 1.582 and 3.103 ac excepted tracts, each "
            "independently anchored off NGS SF 010 (AH1674) and each tying that same bolt; "
            "the two paths agree to 0.017 ft. The parcel census is unchanged at 630: the "
            "gross traverse would have added 120 SUNFLOWER BEACH parcels, every one of which "
            "lies inside a carve-out and is removed by the subtraction. 4.048 ac of the old "
            "footprint falls outside the deed line, all of it boundary sliver on large "
            "KM BEACH / KM LINKS parcels, and no parcel leaves the census."
        )
        current = session.execute(
            text("SELECT review_reason FROM covenant WHERE covid = :covid"),
            {"covid": COVID}).scalar()
        session.execute(
            text("UPDATE covenant SET review_reason = :note WHERE covid = :covid"),
            {"note": merge_tagged_note(current, "TRACT 1 GEOMETRY PROVENANCE (automated)", note),
             "covid": COVID})
        session.commit()
        print(f"\n  committed: tract.geom rewritten, source_id={source_id}, note updated")


if __name__ == "__main__":
    main(commit="--commit" in sys.argv)
