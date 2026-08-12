"""Tests for state_plane_anchor.anchor_by_adjoining_plat -- Tier 0's
adjoining-plat tie.

The synthetic cases pin the REFUSALS, which are the whole value of the function:
a tier that answers confidently when it shouldn't is worse than one that never
runs. The live case pins the answer, against the one anchor in this database
that was first derived by hand -- covid 4981's Andrew S. Young Survey tract.

Every refusal here is a mistake this function actually made while being built,
and each one produced a confident wrong answer, not an error:

  scoring all vertices               2,338 ft off  (preferred sitting INSIDE the plat;
                                                    only the live case can catch this --
                                                    a traverse reaches the fit as a SHAPE,
                                                    so "dropped inside" is not something
                                                    an input can express)
  scoring 4 consecutive vertices     1,379 ft off  (fitted four SHORT closing courses)
  requiring a 200 ft span            1,242 ft off  (a STRAIGHT run slides along a
                                                    straight boundary and fits all
                                                    the way)
  filtering seeds by the top 20      1,194 ft off  (the true corner ranked below it)
  requiring the raw seed be exterior 1,194 ft off  (a corner with an unrefined
                                                    traverse hung off it has a
                                                    vertex inside, of course)

Usage: python3 scripts/test_adjoining_plat_anchor.py
"""
import math
import sys

sys.path.insert(0, ".")

from shapely.geometry import Polygon

from app.gis.state_plane_anchor import anchor_by_adjoining_plat

# An L-shaped plat, the shape of the real case: its east line runs north to
# (1000, 1000), where the north boundary STEPS 400 ft west and the plat carries
# on north beyond it. The tract fills that notch.
#
# The step is what makes the fixture a test. A straight contact run on a straight
# boundary does not fix position ALONG that line -- it slides and fits perfectly
# the whole way, which is how two earlier fixtures here quietly passed the wrong
# answer (24.9 ft west, and 4.9 ft south along the side edges of a plat that was
# exactly as wide as the tract). Position is determined by the plat TURNING.
PLAT = Polygon([(200, 700), (1000, 700), (1000, 1000), (600, 1000),
                (600, 1400), (200, 1400)])
# Out of the notch: west 400 with the plat's north line, north 300 with the east
# line of its northern leg, then east and back south to close.
TRACT = [(1000.0, 1000.0), (600.0, 1000.0), (600.0, 1300.0), (1000.0, 1300.0),
         (1000.0, 1000.0)]
ON_THE_PLAT = [0, 1, 2]  # courses 0..2 run with the plat, around its corner


def _shifted(dx, dy):
    return [(x + dx, y + dy) for x, y in TRACT]


def test_it_finds_the_corner_it_was_not_told() -> None:
    """The traverse is handed to the function in arbitrary local coordinates --
    it is not told where the plat is. Only the two courses running with the
    plat's north line are named."""
    got = anchor_by_adjoining_plat(_shifted(-5000, -20000), PLAT, ON_THE_PLAT,
                                   stated_acres=400 * 300 / 43560.0,
                                   min_contact_vertices=3, min_contact_span_ft=600)
    assert got["anchored"], got["reason"]
    assert got["rms_ft"] < 0.5, got
    assert math.dist(got["pob_xy"], (1000.0, 1000.0)) < 1.0, got["pob_xy"]
    print(f"PASS: placed on the plat's own corner to {got['rms_ft']:.3f} ft, "
          f"POB {math.dist(got['pob_xy'], (1000.0, 1000.0)):.2f} ft off")


def test_it_refuses_a_contact_run_too_short_to_fix_position() -> None:
    """Three courses of 10 ft touching a boundary is not a tie: the placement
    can slide anywhere along that boundary and fit just as well."""
    got = anchor_by_adjoining_plat(TRACT, PLAT, ON_THE_PLAT,
                                   min_contact_vertices=3, min_contact_span_ft=5000)
    assert not got["anchored"] and "span" in got["reason"], got
    print(f"PASS: refused too little frontage -- {got['reason'][:72]}")


def test_it_refuses_when_the_plat_is_not_on_grid_bearings() -> None:
    """The one assumption the method makes -- that the deed's bearings are the
    plat's -- is tested rather than trusted. Rotate the plat and the rotated fit
    wins, which is exactly the signature that must stop the tier."""
    turned = Polygon([(math.cos(math.radians(8)) * x - math.sin(math.radians(8)) * y,
                       math.sin(math.radians(8)) * x + math.cos(math.radians(8)) * y)
                      for x, y in PLAT.exterior.coords])
    got = anchor_by_adjoining_plat(TRACT, turned, ON_THE_PLAT,
                                   min_contact_vertices=3, min_contact_span_ft=600,
                                   rotation_probe_deg=10.0, rotation_probe_step=1.0)
    assert not got["anchored"], got
    assert "grid-referenced" in got["reason"] or "rotation" in got["reason"], got["reason"]
    print(f"PASS: refused a plat off grid bearings -- {got['reason'][:72]}")


def test_it_refuses_an_area_that_contradicts_the_deed() -> None:
    got = anchor_by_adjoining_plat(TRACT, PLAT, ON_THE_PLAT, stated_acres=40.0,
                                   min_contact_vertices=3, min_contact_span_ft=600)
    assert not got["anchored"] and "stated" in got["reason"], got
    print(f"PASS: refused an area contradicting the deed -- {got['reason'][:66]}")


def test_it_reproduces_covid_4981_from_live_collin_geometry() -> None:
    """The real case, end to end and unattended: covid 4981's Young Survey tract,
    whose deed names 'the Easterly Northeast corner of The Heights at Westridge
    Phase I' and then runs four courses with that plat's north line. Nothing here
    tells the function where that corner is -- Collin publishes Phase I, and the
    fit finds it."""
    import shapely.wkt
    from sqlalchemy import text

    from app.db.session import get_session
    from app.gis.adapters import collin_tx as collin
    from app.ingestion.corrected_text import load_correction
    from app.parsing.legal_description.metes_bounds import extract_courses, walk_traverse

    try:
        rows = [r for r in collin.iter_all_features(
            collin.BASE_URL,
            where="UPPER(legalAbsSubName) = 'HEIGHTS AT WESTRIDGE PHASE I THE'",
            out_fields="PROP_ID", return_geometry=True) if r.get("geometry", {}).get("rings")]
    except Exception as exc:                                   # noqa: BLE001
        print(f"SKIP: live Collin GIS unavailable ({type(exc).__name__})")
        return
    assert len(rows) > 300, f"Phase I should be ~398 parcels, got {len(rows)}"

    import json

    with get_session() as session:
        session.execute(text("CREATE TEMP TABLE ph1(g geometry) ON COMMIT DROP"))
        for r in rows:
            session.execute(
                text("INSERT INTO ph1 VALUES "
                     "(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326), 2276))"),
                {"g": json.dumps(collin.esri_rings_to_geojson_multipolygon(
                    r["geometry"]["rings"]))})
        # Dissolve the lot lines and fill the streets: the deed runs with the
        # PLAT's outer line, which no individual tax parcel carries.
        wkt = session.execute(text(
            "SELECT ST_AsText(ST_Buffer(ST_Buffer(ST_Union(g), 45), -45)) FROM ph1")).scalar()
        session.rollback()

    text_rec = load_correction(4981, "tract_young_survey_11878")
    assert text_rec and text_rec["verified"], "the corrected reading must be on record first"
    vertices = walk_traverse(extract_courses(text_rec["text"]))["vertices"]

    got = anchor_by_adjoining_plat(vertices, shapely.wkt.loads(wkt), [0, 1, 2, 3, 4],
                                   epsg=2276, stated_acres=11.878)
    assert got["anchored"], got["reason"]
    assert got["contact_span_ft"] > 600, got
    assert got["rms_ft"] < 10, got
    assert got["overlap_sqft"] < 2000, got
    assert abs(got["area_delta_pct"]) < 0.5, got
    assert got["geojson"]["type"] == "MultiPolygon", got["geojson"]["type"]
    # The placement committed to tract(4981, 2), found by hand before this
    # function existed. Agreeing with it to under a foot is the point.
    off = math.dist(got["pob_xy"], (2503682.18, 7119055.42))
    assert off < 5.0, f"POB {off:.2f} ft from the committed anchor"
    print(f"PASS: covid 4981's Young Survey tract anchored unattended -- POB {off:.2f} ft "
          f"from the committed one, residual {got['rms_ft']:.2f} ft over "
          f"{got['contact_span_ft']:.0f} ft of frontage, area {got['area_delta_pct']:+.2f}%")


if __name__ == "__main__":
    test_it_finds_the_corner_it_was_not_told()
    test_it_refuses_a_contact_run_too_short_to_fix_position()
    test_it_refuses_when_the_plat_is_not_on_grid_bearings()
    test_it_refuses_an_area_that_contradicts_the_deed()
    test_it_reproduces_covid_4981_from_live_collin_geometry()
    print("\nall adjoining-plat anchor tests passed")
