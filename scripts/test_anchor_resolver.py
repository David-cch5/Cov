"""Smoke tests for app/gis/anchor_resolver.py -- the tiered deterministic ->
Opus 5 -> Fable 5 -> geocode-approximate anchor-resolution orchestrator.

Deliberately does NOT exercise a live LLM tier here (that's this project's
most expensive, slowest possible call -- a real run against covid 5838 took
51 tool-call turns and ~53 minutes). What's tested instead is exactly the
part that's cheap, fast, and where a real bug already happened once this
session: the deterministic Tier-0 attempts, and the independent verification
gate an LLM's own reported result must pass before anything is ever
committed (never trusting a self-reported confidence number, per
CLAUDE.md's never-fabricate rule) -- confirmed load-bearing by this
project's own history, not a hypothetical: my own manual covid 5838 tie
attempt accepted a 0.843 length_ratio (a genuinely wrong correspondence)
before this exact class of check existed.

Usage: python3 scripts/test_anchor_resolver.py
"""
import json

import sys

sys.path.insert(0, ".")

from app.db.session import get_session
from app.gis.anchor_resolver import (
    _MAX_ACREAGE_DEVIATION, _polygon_area_acres, _try_parcel_tie, _try_sibling_tract_tie,
    _try_stated_coordinate, _verify_llm_anchor,
)
from app.parsing.legal_description.metes_bounds import extract_courses

_SIMPLE_SQUARE_COURSES_TEXT = (
    "BEGINNING at a point;\n"
    "THENCE North 00 deg. 00 min. 00 sec. East, a distance of 660.00 feet to a point;\n"
    "THENCE South 90 deg. 00 min. 00 sec. East, a distance of 660.00 feet to a point;\n"
    "THENCE South 00 deg. 00 min. 00 sec. West, a distance of 660.00 feet to a point;\n"
    "THENCE North 90 deg. 00 min. 00 sec. West, a distance of 660.00 feet to the POINT OF BEGINNING, "
    "containing 10.00 acres of land, more or less"
)


def test_stated_coordinate_found_and_placed() -> None:
    """covid 3194's own real case (Montgomery, a known zone): a stated State
    Plane X/Y anchors the traverse via pure projection math, no judgment."""
    courses = extract_courses(_SIMPLE_SQUARE_COURSES_TEXT)
    assert len(courses) == 4, courses
    text_with_coord = _SIMPLE_SQUARE_COURSES_TEXT + " BEGINNING at a point, X = 3,732,239.49 and Y = 10,104,887.10"
    candidate = _try_stated_coordinate("48339", text_with_coord, courses)
    assert candidate is not None, "expected a stated-coordinate candidate for a known TX zone (Montgomery)"
    assert candidate["method"] == "stated_coordinate"
    assert candidate["geojson"]["type"] == "MultiPolygon"
    print(f"PASS: stated-coordinate anchor found and placed -> {candidate['method']}, "
          f"confidence={candidate['confidence']}")


def test_stated_coordinate_absent_returns_none() -> None:
    courses = extract_courses(_SIMPLE_SQUARE_COURSES_TEXT)
    candidate = _try_stated_coordinate("48339", _SIMPLE_SQUARE_COURSES_TEXT, courses)
    assert candidate is None, "no X=/Y= coordinate in the text -- must not fabricate one"
    print("PASS: no stated coordinate in text -> correctly returns None, not a guess")


def test_stated_coordinate_unknown_county_returns_none() -> None:
    """Never guess a State Plane zone for a county this project hasn't
    actually confirmed one for -- even if a coordinate-shaped string is
    present in the text."""
    courses = extract_courses(_SIMPLE_SQUARE_COURSES_TEXT)
    text_with_coord = _SIMPLE_SQUARE_COURSES_TEXT + " X = 1,000,000.00 and Y = 5,000,000.00"
    candidate = _try_stated_coordinate("99999", text_with_coord, courses)
    assert candidate is None, "county not in _KNOWN_TX_ZONES -- must not guess a zone"
    print("PASS: unknown county -> correctly returns None rather than guessing a State Plane zone")


def test_sibling_and_parcel_tie_are_deliberately_stubbed() -> None:
    """Tiers 0b/0c are intentionally not automated in this pass -- vertex-
    to-real-corner correspondence needs judgment (confirmed the hard way:
    my own manual covid 5838 attempt got this wrong, accepting a 16%
    length mismatch). They must always defer to the LLM tiers, never
    half-guess a correspondence."""
    with get_session() as session:
        assert _try_sibling_tract_tie(session, covid=5838, tract_no=1) is None
        assert _try_parcel_tie(session, "48355", "any text", []) is None
    print("PASS: sibling-tie and parcel-tie tiers correctly deferred (not half-automated)")


def test_verify_llm_anchor_rejects_acreage_mismatch() -> None:
    """An LLM's own self-reported confidence must never be trusted alone --
    a candidate whose independently-recomputed area is off from the deed's
    own stated acreage by more than _MAX_ACREAGE_DEVIATION is rejected
    regardless of what confidence the model reported."""
    # A ~10-acre square near Montgomery County, TX -- real, valid geometry.
    small_square = {
        "type": "MultiPolygon",
        "coordinates": [[[
            [-95.50, 30.30], [-95.50, 30.302], [-95.498, 30.302], [-95.498, 30.30], [-95.50, 30.30],
        ]]],
    }
    with get_session() as session:
        session.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO covenant (covid, county_fips, status, stated_acreage) "
                "VALUES (999999, '48339', 'ingested', 500.0) "
                "ON CONFLICT (covid) DO UPDATE SET stated_acreage = 500.0"
            )
        )
        llm_result = {
            "anchored": True, "confidence": 0.99, "reasoning": "high self-reported confidence",
            "anchor_geojson": json.dumps(small_square), "method": "parcel_tie",
        }
        candidate = _verify_llm_anchor(session, covid=999999, county_fips="48339", llm_result=llm_result)
        assert candidate is None, (
            "a small polygon area wildly inconsistent with a 500-ac stated acreage must be "
            "rejected regardless of the model's own reported 0.99 confidence"
        )
        session.execute(__import__("sqlalchemy").text("DELETE FROM covenant WHERE covid = 999999"))
        session.commit()
    print("PASS: acreage-deviation gate rejects a mismatched LLM anchor despite high self-reported confidence")


def test_verify_llm_anchor_accepts_reconciled_case() -> None:
    """The inverse: a candidate whose recomputed area DOES reconcile with
    the deed's own stated acreage passes this gate (the real spatial
    intersection dry-run in _attempt_and_verify is the second, separate
    check -- this one only covers acreage reconciliation)."""
    ten_ac_square = {
        "type": "MultiPolygon",
        "coordinates": [[[
            [-95.50, 30.300], [-95.50, 30.3018132], [-95.4979, 30.3018132], [-95.4979, 30.300], [-95.50, 30.300],
        ]]],
    }
    with get_session() as session:
        area = _polygon_area_acres(session, ten_ac_square)
        assert area is not None and 8 < area < 12, f"expected roughly 10 ac, got {area}"
        session.execute(
            __import__("sqlalchemy").text(
                "INSERT INTO covenant (covid, county_fips, status, stated_acreage) "
                "VALUES (999999, '48339', 'ingested', :ac) "
                "ON CONFLICT (covid) DO UPDATE SET stated_acreage = :ac"
            ),
            {"ac": area},
        )
        llm_result = {
            "anchored": True, "confidence": 0.9, "reasoning": "closes tightly, ties confirmed two ways",
            "anchor_geojson": json.dumps(ten_ac_square), "method": "ngs_monument_tie",
        }
        candidate = _verify_llm_anchor(session, covid=999999, county_fips="48339", llm_result=llm_result)
        assert candidate is not None, "a reconciled acreage match must pass this gate"
        assert candidate["confidence"] <= 0.95, "confidence is capped, never blindly passed through"
        session.execute(__import__("sqlalchemy").text("DELETE FROM covenant WHERE covid = 999999"))
        session.commit()
    print(f"PASS: acreage-reconciled LLM anchor passes the gate -> confidence={candidate['confidence']}")


def test_verify_llm_anchor_rejects_malformed_geojson() -> None:
    with get_session() as session:
        for bad_result in [
            {"anchored": True, "confidence": 0.9, "anchor_geojson": "not json at all"},
            {"anchored": True, "confidence": 0.9, "anchor_geojson": None},
            {"anchored": True, "confidence": 0.9},
        ]:
            assert _verify_llm_anchor(session, covid=1, county_fips="48339", llm_result=bad_result) is None
    print("PASS: malformed/missing anchor_geojson correctly rejected, never crashes")


if __name__ == "__main__":
    test_stated_coordinate_found_and_placed()
    test_stated_coordinate_absent_returns_none()
    test_stated_coordinate_unknown_county_returns_none()
    test_sibling_and_parcel_tie_are_deliberately_stubbed()
    test_verify_llm_anchor_rejects_acreage_mismatch()
    test_verify_llm_anchor_accepts_reconciled_case()
    test_verify_llm_anchor_rejects_malformed_geojson()
    print("\nall anchor_resolver smoke tests passed")
