"""Tests for reconstructing a curve's missing chord from its siblings.

The case: covid 4981's 55.73 ac tract loses a whole "THENCE along said curve ...
(Chord Bearing ...)" line in retyping, keeping the curve's setup data. R and
delta fix the chord's LENGTH and say nothing about its direction -- which is
where assuming tangency to the incoming course went wrong, since the deed
recites "to a point on a curve" and tangency would exit S 01-36-23 W where the
next call reads S 00-25-11 W.

The direction is in the document: three intact street crossings along the same
west line recite the same figure. What transfers is the RELATIVE angle from the
preceding course to the chord, not the absolute bearing.

Usage: python3 scripts/test_curve_siblings.py
"""
import sys

sys.path.insert(0, ".")

from app.ingestion.exha_sheet import read_sheet
from app.parsing.legal_description.curve_siblings import (
    as_chord_recital, curve_recitals, reconstruct_missing_chords)
from app.parsing.legal_description.metes_bounds import extract_courses, walk_traverse


def _tract_3() -> str:
    return [r for r in read_sheet() if r.row_number == 1667][0].text


def test_the_siblings_supply_the_direction_r_and_delta_cannot() -> None:
    text = _tract_3()
    recitals = curve_recitals(text)
    assert len(recitals) > 10, len(recitals)
    assert sum(1 for r in recitals if r["chord_azimuth"] is not None) >= 3

    got = [g for g in reconstruct_missing_chords(text) if g.get("reconstructed")]
    assert len(got) == 1, [(g["radius_ft"], g["delta_deg"]) for g in got]
    curve = got[0]
    assert abs(curve["radius_ft"] - 1985.0) < 0.01, curve
    assert curve["from_siblings"] >= 2, curve
    assert curve["sibling_spread_deg"] < 1.0, curve
    # east, near-perpendicular to the southbound travel -- the motif, not tangency
    assert 85.0 < curve["chord_azimuth"] < 95.0, curve["chord_azimuth"]
    assert abs(curve["chord_ft"] - 46.28) < 0.02, curve
    # and the mislabelled "length of 23.14 feet" is this curve's tangent
    assert abs(curve["tangent_ft"] - 23.14) < 0.01, curve
    print(f"PASS: chord {curve['chord_ft']:.2f} ft at azimuth "
          f"{curve['chord_azimuth']:.2f} from {curve['from_siblings']} siblings agreeing to "
          f"{curve['sibling_spread_deg']:.2f} deg; tangent {curve['tangent_ft']:.2f} = the "
          f"deed's mislabelled 'length'")


def test_the_reconstruction_improves_closure_by_five_times() -> None:
    """The closure test is the arbiter, and it is applied to the reconstruction
    rather than assumed by it."""
    text = _tract_3()
    before = walk_traverse(extract_courses(text))
    curve = [g for g in reconstruct_missing_chords(text) if g.get("reconstructed")][0]
    patched = text[:curve["position"]] + as_chord_recital(curve) + " " + text[curve["position"]:]
    after = walk_traverse(extract_courses(patched))
    assert len(extract_courses(patched)) == len(extract_courses(text)) + 1
    assert after["closure_error_ft"] < before["closure_error_ft"] / 5
    assert abs(before["closure_error_ft"] - 56.17) < 0.05, before["closure_error_ft"]
    assert abs(after["closure_error_ft"] - 10.07) < 0.15, after["closure_error_ft"]
    print(f"PASS: closure {before['closure_error_ft']:.2f} ft -> "
          f"{after['closure_error_ft']:.2f} ft with the reconstructed chord inserted")


def test_a_different_figure_is_not_a_sibling() -> None:
    """The guard that matters. The same description carries a 40-degree curve on
    another alignment; borrowing the crossing motif's -89.67 deg offset for it
    produced a confident reconstruction that took the traverse from 56 ft out to
    112 ft out. Central angle is what tells the figures apart."""
    got = reconstruct_missing_chords(_tract_3())
    for entry in got:
        if entry.get("reconstructed"):
            assert entry["delta_deg"] < 5.0, entry      # the crossing motif only
    print(f"PASS: only the small-angle crossing curve is reconstructed; the 40-degree "
          f"curve finds no sibling of its own kind")


def test_disagreeing_siblings_are_reported_not_averaged() -> None:
    skips = [g for g in reconstruct_missing_chords(_tract_3()) if not g.get("reconstructed")]
    for skip in skips:
        assert "disagree" in skip["reason"], skip
        assert skip["sibling_offsets_deg"], skip
    print(f"PASS: {len(skips)} recital(s) declined with their siblings' spread reported")


if __name__ == "__main__":
    test_the_siblings_supply_the_direction_r_and_delta_cannot()
    test_the_reconstruction_improves_closure_by_five_times()
    test_a_different_figure_is_not_a_sibling()
    test_disagreeing_siblings_are_reported_not_averaged()
    print("\nall curve-sibling tests passed")
