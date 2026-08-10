"""Regression tests for NGS monument-tie anchoring -- app/gis/ngs.py,
app/parsing/legal_description/monument_ties.py, and state_plane_anchor.py's
anchor_by_ngs_monument_tie.

The logic tests run entirely offline against synthetic monuments, so a NGS
outage can never look like a code regression. One live smoke test at the end
exercises the real lookup; it reports SKIP rather than failing if the service is
unreachable, because that is not a defect in this code.

Usage: python3 scripts/test_monument_ties.py
"""
import re
import sys

sys.path.insert(0, ".")

from app.db.session import get_session
from app.gis.ngs import NgsMonument, normalize_designation, parse_datasheet
from app.gis.state_plane_anchor import (
    cross_check_monument_ties,
    verify_monument_zone,
)
from app.ingestion.walk import get_deed_text
from app.parsing.legal_description.monument_ties import extract_ngs_monument_ties

# Real published values, straight off each mark's NGS datasheet.
SF010 = NgsMonument(
    designation="SF 010", pid="AH1674", lat=27.807717922, lon=-97.084877875,
    realization=2011, spc_zone="TX S", spc_north_sft=17185006.24,
    spc_east_sft=1441718.04, convergence_deg=0.642472, grid_scale=0.99999362,
    condition="GOOD",
)
KNOLL = NgsMonument(
    designation="KNOLL", pid="AH1137", lat=27.792516744, lon=-97.087468425,
    realization=1993, spc_zone="TX S", spc_north_sft=17179470.57,
    spc_east_sft=1440942.49, convergence_deg=0.641306, grid_scale=0.99998992,
    condition="MARK NOT FOUND",
)


def _covid_5838_ties():
    with get_session() as session:
        text = " ".join((get_deed_text(session, 5838) or "").split())
    return text, extract_ngs_monument_ties(text)


def test_designation_normalisation_keeps_distinct_marks_distinct() -> None:
    """A deed's stamping "SF-010" is NGS designation "SF 010" -- the same mark.
    But KNOLL, KNOLL ECC and KNOLL RM 2 are three different physical monuments
    metres apart, and collapsing them would silently anchor off the wrong one."""
    assert normalize_designation("SF-010") == normalize_designation("SF 010")
    assert normalize_designation("Knoll") == "KNOLL"
    assert len({normalize_designation(n) for n in ("KNOLL", "KNOLL ECC", "KNOLL RM 2")}) == 3
    print("PASS: designation normalisation matches SF-010/SF 010, keeps the three KNOLLs apart")


def test_datasheet_parsing_reads_the_grid_line() -> None:
    """The State Plane line's leading dash is a column separator, not a sign --
    reading it as one would put the mark 17 million feet south of Texas."""
    sheet = """
 AH1137  DESIGNATION -  KNOLL
 AH1137* NAD 83(1993) POSITION- 27 47 33.06028(N) 097 05 14.88633(W)   ADJUSTED
 AH1137;SPC TX S     -17,179,470.57  1,440,942.49   sFT  0.99998992   +0 38 28.7
 AH1137  CONDITION   -  MARK NOT FOUND
"""
    got = parse_datasheet(sheet)
    assert got["spc_zone"] == "TX S", got
    assert got["spc_north_sft"] == 17179470.57 and got["spc_east_sft"] == 1440942.49, got
    assert got["realization"] == 1993, got
    assert abs(got["lat"] - 27.792516744) < 1e-8 and got["lon"] < 0, got
    assert abs(got["convergence_deg"] - 0.641306) < 1e-5, got
    print(f"PASS: datasheet -> zone {got['spc_zone']}, N {got['spc_north_sft']:,.2f}, "
          f"E {got['spc_east_sft']:,.2f}, NAD83({got['realization']})")


def test_zone_mapping_is_verified_against_the_datasheets_own_grid_coords() -> None:
    """NGS_SPC_ZONE_TO_EPSG is checkable, not assumed: reprojecting the mark's
    own published lat/lon into the zone its own sheet names must reproduce the
    sheet's own northing/easting. A wrong zone is off by miles."""
    for mon in (SF010, KNOLL):
        err = verify_monument_zone(mon)
        assert err is not None and err < 1.0, (mon.designation, err)
    wrong_zone = NgsMonument(**{**SF010.__dict__, "spc_zone": "TX C"})
    assert verify_monument_zone(wrong_zone) > 1000, "a wrong zone must not pass the check"
    print(f"PASS: zone check -> SF 010 {verify_monument_zone(SF010):.3f} ft, "
          f"KNOLL {verify_monument_zone(KNOLL):.3f} ft; wrong zone rejected")


def test_covid_5838_ties_extract_including_the_ocr_damaged_one() -> None:
    """All twelve ties across the six SAVE AND EXCEPT tracts, each naming the
    corner it runs from. One SF-010 tie's seconds read "18°06'S5\"" -- the letter
    S for a 5 -- and losing it would leave that tract with a single, uncheckable
    tie."""
    _, ties = _covid_5838_ties()
    assert len(ties) == 12, len(ties)
    assert {normalize_designation(t.designation) for t in ties} == {"SF 010", "KNOLL"}
    assert all(t.corner and t.corner.endswith("corner") for t in ties), \
        [t.corner for t in ties]
    ocr = [t for t in ties if t.distance_ft == 6093.14]
    assert len(ocr) == 1 and ocr[0].seconds == 55, ocr
    print(f"PASS: covid 5838 -> 12 ties, corners named on all, "
          f"OCR'd seconds \"S5\" read as {ocr[0].seconds:.0f}")


def test_cross_check_confirms_a_sound_pair() -> None:
    """The two ties share an unknown corner, so subtracting them cancels it and
    leaves a monument-to-monument vector checkable against published truth."""
    _, ties = _covid_5838_ties()
    sf, kn = ties[0], ties[1]          # the 1.029 acre tract's own pair
    got = cross_check_monument_ties(sf, SF010, kn, KNOLL)
    assert got["agree"], got
    assert abs(got["distance_error_ft"]) < 1.0, got
    assert abs(got["azimuth_error_deg"]) < 0.01, got
    print(f"PASS: sound tie pair agrees to {got['distance_error_ft']:+.2f} ft "
          f"and {got['azimuth_error_deg']:+.4f} deg on a 5,590 ft vector")


def test_cross_check_isolates_the_quadrant_error_rather_than_just_failing() -> None:
    """covid 5838's 5.800 and 0.554 acre tracts recite KNOLL as bearing
    South 20°44'36" WEST where the geometry requires East. The distance agrees to
    0.03 ft and the angle to under three arc-minutes, so only the letter is
    wrong -- the same defect repair_quadrant_by_closure recovers in the courses.
    The cross-check must NAME the bad tie and trust the other, not merely report
    a disagreement, or both tracts lose their only verified anchor."""
    _, ties = _covid_5838_ties()
    pair = [t for t in ties if t.distance_ft in (4708.73, 1033.98)][:2]
    sf, kn = (pair[0], pair[1]) if pair[0].distance_ft == 4708.73 else (pair[1], pair[0])
    assert kn.ew == "West", kn
    got = cross_check_monument_ties(sf, SF010, kn, KNOLL)
    assert not got["agree"], got
    assert got["trusted"] == "a", got                 # SF-010's tie is the sound one
    assert got["corrected"]["tie"] == "b" and got["corrected"]["field"] == "ew", got
    assert abs(got["corrected"]["distance_error_ft"]) < 1.0, got
    print(f"PASS: quadrant error isolated -> KNOLL tie's East/West reversed; "
          f"corrected pair agrees to {got['corrected']['distance_error_ft']:+.2f} ft")


def test_cross_check_refuses_an_unresolvable_disagreement() -> None:
    """If no single letter flip reconciles the pair, nothing may be trusted --
    the disagreement has to survive as the signal it is."""
    _, ties = _covid_5838_ties()
    sf = ties[0]
    junk = type(sf)(**{**ties[1].__dict__, "distance_ft": ties[1].distance_ft + 900.0})
    got = cross_check_monument_ties(sf, SF010, junk, KNOLL)
    assert not got["agree"] and got["trusted"] is None and got["corrected"] is None, got
    print("PASS: an unresolvable tie disagreement is reported, never silently resolved")


def test_live_ngs_lookup_places_all_six_carve_outs() -> None:
    """End-to-end against the real NGS service."""
    from app.gis.ngs import find_monuments
    from app.gis.state_plane_anchor import anchor_by_ngs_monument_tie
    from app.parsing.legal_description.metes_bounds import (
        extract_courses, repair_quadrant_by_closure, walk_traverse,
    )
    text, ties = _covid_5838_ties()
    try:
        mons = find_monuments({t.designation for t in ties},
                              {"min_lat": 27.70, "max_lat": 27.90,
                               "min_lon": -97.16, "max_lon": -97.00})
    except Exception as exc:                     # noqa: BLE001 -- outage, not a regression
        print(f"SKIP: live NGS lookup unavailable ({type(exc).__name__}: {exc})")
        return
    assert set(mons) == {"SF 010", "KNOLL"}, sorted(mons)

    start = text.find("SAVE AND EXCEPT THE FOLLOWING")
    prev, placed = start, 0
    for m in re.finditer(r"containing\s+([\d.,]+)\s+acres", text[start:]):
        end = start + m.end()
        courses, _ = repair_quadrant_by_closure(extract_courses(text[prev:end]))
        got = anchor_by_ngs_monument_tie(
            walk_traverse(courses)["vertices"],
            [t for t in ties if prev <= t.position < end], mons)
        assert got["verified"], (m.group(1), got)
        assert got["monument"] == "SF 010", got     # the 2011-realization mark wins
        assert got["zone_check_ft"] < 0.1, got
        placed += 1
        prev = end
    assert placed == 6, placed
    print(f"PASS: live NGS -> all {placed} covid 5838 carve-outs placed and verified")


def test_service_not_answering_is_not_evidence_of_no_monument() -> None:
    """Two ways NGS can fail to answer, both of which used to look exactly like
    "there is no monument here" -- and that misreading is expensive. A deed that
    recites a tie to a named NGS monument is itself evidence the monument exists
    nearby, so an unusable answer must never send the covenant down to
    anchor_resolver's paid Opus/Fable tiers to buy what NGS publishes for free.

    Tested with a stubbed transport rather than by waiting for an outage. The
    empty case is real: on 2026-08-10 /api/nde/bounds answered HTTP 200 with []
    for every bbox tried, including dense control areas.
    """
    from app.gis import ngs as ngs_module
    from app.gis.ngs import (
        NGS_BOUNDS_RESULT_CAP, NgsResultTruncated, NgsServiceEmpty, NgsUnanswered,
        find_monuments,
    )

    bbox = {"min_lat": 27.70, "max_lat": 27.90, "min_lon": -97.16, "max_lon": -97.00}

    class _Resp:
        def __init__(self, payload): self._payload = payload
        def raise_for_status(self): pass
        def json(self): return self._payload

    original = ngs_module.requests.get
    try:
        # 1. Nothing at all -> transient, retry.
        ngs_module.requests.get = lambda *a, **k: _Resp([])
        try:
            find_monuments({"SF-010"}, bbox)
        except NgsServiceEmpty as e:
            assert "NO marks at all" in str(e), str(e)
            assert "do not treat it as" in str(e), "must say how NOT to read it"
        else:
            raise AssertionError("an empty result set must raise, not report the monument missing")

        # 2. Capped -> incomplete, and retrying the same bbox will not help.
        ngs_module.requests.get = lambda *a, **k: _Resp(
            [{"name": f"OTHER {i}", "pid": f"XX{i:04d}"} for i in range(NGS_BOUNDS_RESULT_CAP)])
        try:
            find_monuments({"SF-010"}, bbox)
        except NgsResultTruncated as e:
            assert "truncated" in str(e), str(e)
        else:
            raise AssertionError("a capped result set must raise")

        # Both are one family, so a caller can decline to escalate on either.
        assert issubclass(NgsServiceEmpty, NgsUnanswered)
        assert issubclass(NgsResultTruncated, NgsUnanswered)

        # 3. A genuine "not in this area" -- marks came back, ours is not among
        #    them, nowhere near the cap -- is a real answer and must NOT raise.
        ngs_module.requests.get = lambda *a, **k: _Resp(
            [{"name": "SOMETHING ELSE", "pid": "AA0001"}])
        assert find_monuments({"SF-010"}, bbox) == {}, (
            "a real, complete answer of 'not here' must return empty, not raise")
    finally:
        ngs_module.requests.get = original
    print("PASS: an unanswered NGS search raises (empty=retry, capped=fix the bbox) while a "
          "genuine 'not in this area' still returns empty")


def test_anchor_resolver_does_not_buy_what_ngs_gives_free() -> None:
    """The consequence, at the tier that spends money: an NGS outage must
    propagate out of the NGS tier rather than returning None and letting the
    resolver walk down to the paid LLM tiers."""
    import inspect

    from app.gis import anchor_resolver

    src = inspect.getsource(anchor_resolver._try_ngs_monument_tie)
    assert "except NgsUnanswered" in src, (
        "the NGS tier must distinguish an unanswered search from 'cannot place'")
    ngs_clause = src.index("except NgsUnanswered")
    broad_clause = src.index("except Exception")
    assert ngs_clause < broad_clause, (
        "the NgsUnanswered clause must come BEFORE the broad handler, or the broad "
        "one swallows it and the fall-through to paid tiers returns")
    assert "raise" in src[ngs_clause:broad_clause], "it must re-raise, not return None"
    print("PASS: anchor_resolver re-raises an unanswered NGS search instead of "
          "falling through to the paid tiers")


if __name__ == "__main__":
    test_designation_normalisation_keeps_distinct_marks_distinct()
    test_datasheet_parsing_reads_the_grid_line()
    test_zone_mapping_is_verified_against_the_datasheets_own_grid_coords()
    test_covid_5838_ties_extract_including_the_ocr_damaged_one()
    test_cross_check_confirms_a_sound_pair()
    test_cross_check_isolates_the_quadrant_error_rather_than_just_failing()
    test_cross_check_refuses_an_unresolvable_disagreement()
    test_service_not_answering_is_not_evidence_of_no_monument()
    test_anchor_resolver_does_not_buy_what_ngs_gives_free()
    test_live_ngs_lookup_places_all_six_carve_outs()
    print("\nall monument-tie tests passed")
