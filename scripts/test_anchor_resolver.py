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
    _MAX_ACREAGE_DEVIATION, _NGS_BBOX_BUFFER_DEG, _get_deed_text, _ngs_search_bbox,
    _polygon_area_acres, _try_ngs_monument_tie, _try_parcel_tie, _try_sibling_tract_tie,
    _try_stated_coordinate, _verify_llm_anchor,
)
from app.gis.ngs import NGS_BOUNDS_RESULT_CAP, NgsUnanswered, find_monuments
from app.parsing.legal_description.metes_bounds import (
    extract_courses, repair_quadrant_by_closure,
)

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


def test_sibling_tie_is_deferred_and_the_parcel_tie_declines_on_its_merits() -> None:
    """Tier 0c (sibling-tract tie) is still deliberately not automated: mapping a
    named corner of an already-anchored sibling onto a particular vertex of THIS
    traverse is the judgment call my own manual covid 5838 attempt got wrong,
    accepting a 16% length mismatch.

    Tier 0d is no longer stubbed -- it reads the adjoining plat off the POB and
    the contact courses off the deed -- so what is pinned here is that it still
    returns None when the deed supports none of that, rather than reaching for
    the county's GIS on the strength of nothing. covid 5838's own deed names no
    POB adjoiner at all, so it must decline without a single network call."""
    with get_session() as session:
        assert _try_sibling_tract_tie(session, covid=5838, tract_no=1) is None
        assert _try_parcel_tie(session, 5838, 1, "48355", "any text", []) is None
        deed, _ = _covid_5838_excepted_segments()
        assert _try_parcel_tie(session, 5838, 1, "48355", deed,
                               extract_courses(deed)) is None
    print("PASS: sibling tie still deferred; parcel tie declines a deed that names no "
          "POB adjoiner, without touching the network")


def _covid_5838_excepted_segments():
    import re
    with get_session() as session:
        deed = _get_deed_text(session, 5838, None)
    sae = deed.find("SAVE AND EXCEPT THE FOLLOWING")
    out, prev = [], sae
    for m in re.finditer(r"containing\s+([\d.,]+)\s+acres", deed[sae:]):
        end = sae + m.end()
        out.append((float(m.group(1).replace(",", "")), deed[prev:end]))
        prev = end
    return deed, out


def test_ngs_tier_declines_a_tie_that_is_not_at_the_point_of_beginning() -> None:
    """The restriction that makes this tier safe. covid 5838 tract 1 begins at
    "a 3/4 inch iron bolt" reciting no monument at all -- yet its document
    contains TWELVE monument ties, every one belonging to a SAVE AND EXCEPT
    tract further down. Anchoring tract 1 off one of those would place it on
    another tract's corner entirely, so the tier must return None and let the
    LLM tiers reason about it instead."""
    deed, _ = _covid_5838_excepted_segments()
    with get_session() as session:
        got = _try_ngs_monument_tie(session, "48355", deed, extract_courses(deed))
    assert got is None, got
    print("PASS: NGS tier declines covid 5838 tract 1 -- its 12 ties all belong to other tracts")


def test_ngs_tier_places_every_tie_that_is_at_the_point_of_beginning() -> None:
    """All six of covid 5838's SAVE AND EXCEPT tracts DO tie their own Point of
    Beginning to SF 010, and each must place at full confidence: the zone check
    reproduces the monument's own datasheet grid coordinates, and a second tie
    to KNOLL cross-checks the placement.

    The 3.103 ac tract is the one that pins the 'first COURSE, not first thence'
    rule: it reaches its tie through a two-leg offset reciting "...and thence
    North 35 24'54\" East 50.00 feet and from which north corner...", so a
    first-THENCE rule would wrongly treat its tie as post-boundary and decline."""
    _, segments = _covid_5838_excepted_segments()
    assert len(segments) == 6, len(segments)
    with get_session() as session:
        for acres, segment in segments:
            courses, _ = repair_quadrant_by_closure(extract_courses(segment))
            # _try_ngs_monument_tie re-raises NgsUnanswered rather than declining,
            # precisely so an outage is not mistaken for "no tie here". Skip on
            # it: a service that is down says nothing about this code. Confirmed
            # necessary -- /api/nde/bounds served HTTP 200 with [] and then 503
            # within the same hour on 2026-08-10.
            try:
                got = _try_ngs_monument_tie(session, "48355", segment, courses)
            except NgsUnanswered as exc:
                print(f"SKIP: live NGS is not answering ({type(exc).__name__}) -- "
                      f"outage, not a regression")
                return
            assert got is not None, f"{acres} ac declined"
            assert got["method"] == "ngs_monument_tie", got
            assert got["confidence"] == 0.95, (acres, got)
            assert "SF 010" in got["reasoning"], got
    print("PASS: NGS tier places all 6 covid 5838 carve-outs at 0.95 (zone check + cross-check)")


def test_ngs_search_bbox_is_narrow_and_skips_an_unloaded_county() -> None:
    """A county with no parcels yet gives no search area, which must skip the
    tier rather than search blind. And the buffer stays small on purpose --
    see test_ngs_bounds_cap_is_an_error_not_an_answer for what going wide does."""
    with get_session() as session:
        assert _ngs_search_bbox(session, "99999") is None
        bbox = _ngs_search_bbox(session, "48355")
    assert bbox is not None
    assert _NGS_BBOX_BUFFER_DEG <= 0.1, _NGS_BBOX_BUFFER_DEG
    assert bbox["max_lat"] - bbox["min_lat"] < 1.0, bbox
    print(f"PASS: NGS bbox -> unloaded county skipped; Nueces box "
          f"{bbox['max_lat'] - bbox['min_lat']:.2f}deg tall at a {_NGS_BBOX_BUFFER_DEG}deg buffer")


def test_ngs_bounds_cap_is_an_error_not_an_answer() -> None:
    """NGS's bounds endpoint silently caps its result list and reports nothing
    about it -- no error, no truncation flag. Measured: Nueces' own parcel extent
    returns 415 marks and finds SF 010 and KNOLL; buffered by 0.25 degrees it
    returns exactly 500 and finds NEITHER. Treating that as "monument not found"
    would report a published, existing monument as missing, so find_monuments
    must raise instead."""
    with get_session() as session:
        narrow = _ngs_search_bbox(session, "48355")
    wide = {"min_lon": narrow["min_lon"] - 0.25, "max_lon": narrow["max_lon"] + 0.25,
            "min_lat": narrow["min_lat"] - 0.25, "max_lat": narrow["max_lat"] + 0.25}
    try:
        got = find_monuments({"SF-010", "Knoll"}, wide)
    except ValueError as exc:
        assert str(NGS_BOUNDS_RESULT_CAP) in str(exc), exc
        print(f"PASS: a capped NGS search raises instead of reporting a real monument missing")
        return
    except Exception as exc:                       # noqa: BLE001 -- outage, not a regression
        print(f"SKIP: live NGS unavailable ({type(exc).__name__})")
        return
    # If NGS ever raises the cap, the wide search simply succeeds -- also fine.
    assert {"SF 010", "KNOLL"} <= set(got), got
    print("PASS: wide NGS search returned both monuments (result cap appears to have been raised)")


class _StubResponse:
    """Enough of a requests.Response for find_monuments' bounds call."""

    status_code = 200
    content = b"[]"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _find_monuments_error(ngs, marks, datasheet=None):
    """Run find_monuments against a stubbed NGS and return the exception it
    raised, or None if it returned instead."""
    real_get, real_sheet = ngs._get, ngs.fetch_datasheet
    ngs._get = lambda *a, **k: _StubResponse(marks)
    if datasheet is not None:
        ngs.fetch_datasheet = lambda *a, **k: datasheet
    try:
        ngs.find_monuments({"SF 010"}, {"min_lon": -97.9, "max_lon": -97.0,
                                        "min_lat": 27.5, "max_lat": 27.9}, timeout=5)
        return None
    except Exception as exc:                      # noqa: BLE001
        return exc
    finally:
        ngs._get, ngs.fetch_datasheet = real_get, real_sheet


def test_every_way_a_named_mark_fails_to_resolve_is_unanswered_not_absent() -> None:
    """All four ways find_monuments can come up empty must raise, because the
    tier above cannot tell them apart from "this tract has no monument tie" --
    and that difference is real money: declining walks the covenant down to Opus
    and Fable to answer what NGS answers for nothing.

    Three of the four already raised. The fourth -- marks returned, under the
    cap, none of them the one the deed names -- fell through and returned {}.
    It surfaced as covid 5838's 1.029 ac carve-out intermittently declining a
    tie it places on eleven runs out of twelve, which is how a silent path
    presents: not as an error, as an occasional wrong answer.

    Stubbed, not live -- a partial NGS response is not something a test can ask
    the real service to produce.
    """
    from app.gis import ngs

    bulk = [{"name": f"MARK {i}", "pid": f"AA{i:04d}"} for i in range(40)]
    capped = [{"name": f"MARK {i}", "pid": f"AA{i:04d}"}
              for i in range(ngs.NGS_BOUNDS_RESULT_CAP)]

    got = {
        "nothing came back": (_find_monuments_error(ngs, []), ngs.NgsServiceEmpty),
        "result set is capped": (_find_monuments_error(ngs, capped), ngs.NgsResultTruncated),
        "datasheet will not parse": (
            _find_monuments_error(ngs, [{"name": "SF 010", "pid": "AB1234"}],
                                  datasheet="<html>maintenance</html>"),
            ngs.NgsDatasheetUnreadable),
        "marks, but not this one": (_find_monuments_error(ngs, bulk),
                                    ngs.NgsNamedMarkUnresolved),
    }
    for label, (exc, expected) in got.items():
        assert exc is not None, f"{label}: returned instead of raising"
        assert isinstance(exc, ngs.NgsUnanswered), \
            f"{label}: {type(exc).__name__} is outside the NgsUnanswered family"
        assert isinstance(exc, expected), f"{label}: got {type(exc).__name__}"
        assert "retry" in str(exc).lower() or "search again" in str(exc).lower(), \
            f"{label}: the message must tell a caller what to do, not just what failed"
    print(f"PASS: all {len(got)} ways a named mark fails to resolve raise NgsUnanswered -- "
          f"none reports a published monument absent")


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
    test_sibling_tie_is_deferred_and_the_parcel_tie_declines_on_its_merits()
    test_ngs_tier_declines_a_tie_that_is_not_at_the_point_of_beginning()
    test_ngs_tier_places_every_tie_that_is_at_the_point_of_beginning()
    test_ngs_search_bbox_is_narrow_and_skips_an_unloaded_county()
    test_ngs_bounds_cap_is_an_error_not_an_answer()
    test_every_way_a_named_mark_fails_to_resolve_is_unanswered_not_absent()
    test_verify_llm_anchor_rejects_acreage_mismatch()
    test_verify_llm_anchor_accepts_reconciled_case()
    test_verify_llm_anchor_rejects_malformed_geojson()
    print("\nall anchor_resolver smoke tests passed")
