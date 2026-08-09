"""Regression tests for app/parsing/legal_description/metes_bounds.py's course
extraction. No prior test file covered this despite two real, previously-live
parser bugs having been found and fixed against real covenant text (Collin
covid 3028, Nueces covid 5838) -- this closes that gap so a future regex
change can't silently re-break either case.

Usage: python3 scripts/test_metes_bounds.py
"""
import re
import sys

sys.path.insert(0, ".")

from app.db.session import get_session
from app.ingestion.walk import get_deed_text
from app.parsing.legal_description.metes_bounds import (
    Course,
    _feet,
    extract_courses,
    repair_quadrant_by_closure,
    walk_traverse,
)
from app.parsing.legal_description.metes_bounds_llm import to_course_objects


def test_spelled_out_bearing_units() -> None:
    """covid 3028 (Collin): deed spells out "degrees"/"minutes"/"seconds" in
    full instead of using "deg."/"min." or the degree symbol."""
    text = 'THENCE North 89 degrees 58 minutes 15 seconds East, a distance of 500.00 feet to a point;'
    courses = extract_courses(text)
    assert len(courses) == 1, courses
    c = courses[0]
    assert (c.ns, c.degrees, c.minutes, c.seconds, c.ew) == ("North", 89.0, 58.0, 15.0, "East"), c
    assert c.distance_ft == 500.00, c
    print("PASS: spelled-out bearing units (covid 3028) ->", c)


def test_curly_quote_minutes_marker() -> None:
    """covid 5838 (Nueces): OCR rendered the minutes mark as a curly right
    single quote (U+2019 '’') instead of a straight apostrophe -- e.g.
    "North 56° 00’ 00\" West". Missing this dropped the course entirely."""
    text = 'THENCE North 56° 00’ 00" West, a distance of 2330.05 feet to a point;'
    courses = extract_courses(text)
    assert len(courses) == 1, courses
    c = courses[0]
    assert (c.ns, c.degrees, c.minutes, c.seconds, c.ew) == ("North", 56.0, 0.0, 0.0, "West"), c
    assert c.distance_ft == 2330.05, c
    print("PASS: curly-quote minutes marker (covid 5838) ->", c)


def test_deed_annotation_between_distance_and_to() -> None:
    """covid 5838 (Nueces): a surveyor's own "(Deed = X feet)" aside -- comparing
    a re-measured distance against the original deed's recited figure -- sits
    between the real distance and "to", e.g. "1255.59 feet (Deed = 1254.90
    feet) to a point". The real (measured) distance must win, not the aside."""
    text = ('THENCE South 55° 14\' 00" East, along the common boundary of said tracts, '
            'at 1005.59 feet pass a 5/8 inch iron rod set for a reference point, in all a total '
            'distance of 1255.59 feet (Deed = 1254.90 feet) to a point;')
    courses = extract_courses(text)
    assert len(courses) == 1, courses
    c = courses[0]
    assert c.distance_ft == 1255.59, c  # the measured figure, not the Deed=1254.90 aside
    print("PASS: Deed-annotation aside between distance and 'to' (covid 5838) ->", c)


def test_compound_bearing_list_without_thence_or_to() -> None:
    """covid 5838 (Nueces): a run of courses along one meandering natural
    boundary is recited as ONE "THENCE following said mean high tide line...:"
    lead-in, then several bare "<bearing>, a distance of N feet;" calls -- no
    repeated THENCE, no "to <landmark>" on any of them."""
    text = ('THENCE following said mean high tide line of said Gulf of Mexico: '
            'South 33° 00\' 00" West, a distance of 470.00 feet; '
            'South 37° 00\' 00" West, a distance of 1065.00 feet; '
            'South 35° 15\' 00" West, a distance of 850.00 feet;')
    courses = extract_courses(text)
    assert len(courses) == 3, courses
    assert [c.distance_ft for c in courses] == [470.00, 1065.00, 850.00], courses
    assert [c.degrees for c in courses] == [33.0, 37.0, 35.0], courses
    print(f"PASS: compound bearing list, no THENCE/no 'to' (covid 5838) -> {len(courses)} courses")


def test_compound_bearing_excludes_monument_ties() -> None:
    """covid 5838 (Nueces): the same "<bearing>, a distance of N feet;" shape
    also matches a monument TIE embedded in a curve call's descriptive text --
    "for a corner of this tract from which a 60D nail bears North 35°43'56\" East,
    a distance of 2.28 feet;" describes where a nail sits relative to the
    corner, not a call walking the boundary. Real THENCE-style calls are never
    phrased as "<landmark> bears <direction>"."""
    text = ('THENCE North 53° 41\' 15" West, a distance of 979.59 feet to a corner of this '
            'tract from which a 60D nail bears North 35° 43\' 56" East, a distance of 2.28 feet; '
            'THENCE North 57° 16\' 24" West, a distance of 245.61 feet to a point;')
    courses = extract_courses(text)
    assert len(courses) == 2, courses  # the tie (2.28 ft) must NOT show up as a third course
    assert [c.distance_ft for c in courses] == [979.59, 245.61], courses
    print(f"PASS: monument-tie exclusion (covid 5838) -> {len(courses)} real courses, tie skipped")


def test_covid_5838_primary_tract_closes() -> None:
    """End-to-end regression: covid 5838's primary 318.779-acre tract (Nueces),
    combining every fix in this file, now closes essentially exactly.

    An earlier version of this test asserted 15 courses and a ~32 ft closure,
    on the reasoning that the deed's one bearingless curve call was "not
    independently derivable without guessing". That was wrong, and the closure
    error was the proof: 31.95 ft, exactly the arc's own length to 0.004 ft.
    The curve is non-tangential, so the previous course says nothing about it --
    but the deed recites the RADIUS BEARING at the point of curvature, which
    fixes the tangent, and the chord is that tangent rotated half the central
    angle toward the side the curve turns. Walking it takes closure from
    1:674 to better than 1:1,000,000 and the area to within 0.001 ac of the
    deed's own stated figure.
    """
    text = (
        'BEGINNING at a 3/4 inch iron bolt found for the northwest corner of an 8.720 acre tract;\n'
        'THENCE South 55° 14\' 00" East, along the common boundary of said 8.720 acre tract '
        'and said 289.6 acre tract, at 1005.59 feet pass a 5/8 inch iron rod set for a reference '
        'point, in all a total distance of 1255.59 feet (Deed = 1254.90 feet) to a point;\n'
        'THENCE South 34° 35\' 00" West, following the meandering mean high tide, a distance '
        'of 1895.03 feet to a 5/8 inch iron rod set for a corner of this tract;\n'
        'THENCE South 32° 30\' 00" West, continuing with the meandering said mean high tide, '
        'a distance of 501.90 feet to a point for the most southerly corner of this tract;\n'
        'THENCE North 56° 00’ 00" West, along the southwest boundary line of said 289.6 '
        'acre tract, at 250.00 feet pass a 5/8 inch iron rod set for a reference point, in all a '
        'total distance of 2330.05 feet to a 5/8 inch iron rod found in the southeast '
        'right-of-way line of Park Road 53;\n'
        'THENCE North 35° 31\' 40" East, along said southeast right-of-way line of Park Road '
        '53, a distance of 7233.62 feet to a 5/8 inch iron rod set for the most northerly corner '
        'of this tract;\n'
        'THENCE South 56° 00\' 00" East, leaving said southeast right-of-way line of Park '
        'Road 53, at 2194.34 feet pass a 5/8 inch iron rod set for a reference point, in all a '
        'total distance of 2244.34 feet to a point in the mean high tide line of said Gulf of '
        'Mexico for the eastern most corner of this tract;\n'
        'THENCE following said mean high tide line of said Gulf of Mexico: '
        'South 33° 00\' 00" West, a distance of 470.00 feet; '
        'South 37° 00\' 00" West, a distance of 1065.00 feet; '
        'South 35° 15\' 00" West, a distance of 850.00 feet; '
        'South 27° 00\' 00" West, a distance of 130.00 feet; '
        'South 34° 53\' 00" West, a distance of 179.76 feet;\n'
        'THENCE North 53° 41\' 15" West (Deed = North 53°39\' 00" West), leaving said '
        'mean high tide line, same being the northeast boundary line of a 16.29 acre tract, at '
        '250.00 feet pass a 5/8 inch iron rod set for a reference point, in all a total distance '
        'of 979.59 feet (Deed=582.95 feet) to a 5/8 inch iron rod found for an interior corner of '
        'this tract;\n'
        'THENCE South 36° 13\' 31" West (Deed = South 36°21\'00" West), along the '
        'northwest boundary line of said 16.29 acre tract, a distance of 719.14 feet (Deed = '
        '717.02 feet) to a 5/8 inch iron rod found in the centerline of Beach Access Road 1 for '
        'the point of curvature of a non-tangential circular curve to the right having a central '
        'angle of 00°57\'31"", a radius of 1909.86 feet, and an arc length of 31.95 feet from '
        'which the radius bears North 31°46\'05" East, a distance of 1909.86 feet, for a '
        'corner of this tract from which a 60D nail bears north 35° 43\' 56" East, a distance '
        'of 2.28 feet;\n'
        'THENCE in a northwesterly direction, an arc distance of 31.95 feet to a 5/8 inch iron rod '
        'found for the point of tangency of said circular curve to the right;\n'
        'THENCE North 57° 16\' 24" West (Deed =North 57°12\'00" West), along the '
        'centerline of said Beach Access Road 1, a distance of 245.61 feet (Deed = 245.70 feet) '
        'to a 5/8 inch iron rod found being the most northerly corner of a 9.400 acre tract, and '
        'for an interior corner of this tract;\n'
        'THENCE South 34° 46\' 00" West, leaving said centerline of Beach Access Road 1, a '
        'distance of 1440.19 feet (Deed = 1440.10 feet) to the POINT OF BEGINNING and containing '
        '318.779 acres of land, more or less'
    )
    courses = extract_courses(text)
    assert len(courses) == 16, (len(courses), courses)

    curves = [c for c in courses if c.is_curve]
    assert len(curves) == 1, curves
    c = curves[0]
    # derived, not recited: tangent perpendicular to the North 31\u00b046'05" East
    # radius, rotated +delta/2 because the curve turns right
    assert (c.ns, c.ew) == ("North", "West"), c
    assert abs(c.degrees + c.minutes / 60 + c.seconds / 3600 - 57.7526) < 0.01, c
    assert abs(c.distance_ft - 31.953) < 0.01, c        # chord = 2R sin(delta/2)
    assert c.curve_direction == "right" and c.radius_ft == 1909.86, c
    # traversal order, not appended at the end: the curve sits between the course
    # that ends at the point of curvature and the one that starts at the point of
    # tangency, even though its parameters are recited in the FORMER's sentence
    i = courses.index(c)
    assert i == 13, i
    assert (courses[i - 1].ns, courses[i - 1].degrees) == ("South", 36.0), courses[i - 1]
    assert (courses[i + 1].ns, courses[i + 1].degrees) == ("North", 57.0), courses[i + 1]

    result = walk_traverse(courses)
    stated_acreage = 318.779
    area_diff_pct = abs(result["area_acres"] - stated_acreage) / stated_acreage
    assert area_diff_pct < 0.0001, (result["area_acres"], stated_acreage, area_diff_pct)
    assert result["closure_error_ft"] < 0.1, result["closure_error_ft"]
    assert result["closure_ratio"] < 1e-6, result["closure_ratio"]
    print(f"PASS: covid 5838 primary tract -> {len(courses)} courses incl. the derived curve chord, "
          f"area={result['area_acres']:.3f} ac (stated {stated_acreage}), "
          f"closure {result['closure_error_ft']:.3f} ft (1:{round(1 / result['closure_ratio']):,})")


def test_non_tangential_curve_direction_must_be_stated() -> None:
    """The general direction ("in a northwesterly direction") is what picks
    between the radius's two perpendiculars. A curve whose stated direction
    matches NEITHER is a deed that contradicts itself -- the course is dropped
    so it surfaces as a bad closure, rather than a bearing being invented."""
    base = (
        'THENCE South 36\u00b0 13\' 31" West, a distance of 719.14 feet to a point for '
        'the point of curvature of a non-tangential circular curve to the right having a '
        'central angle of 00\u00b057\'31"", a radius of 1909.86 feet, and an arc length of '
        '31.95 feet from which the radius bears North 31\u00b046\'05" East, a distance of '
        '1909.86 feet, for a corner of this tract;\n'
        'THENCE in a {direction} direction, an arc distance of 31.95 feet to a point;'
    )
    ok = extract_courses(base.format(direction="northwesterly"))
    assert len([c for c in ok if c.is_curve]) == 1, ok
    # both perpendiculars of a N31\u00b046'E radius run NW or SE -- never NE
    bad = extract_courses(base.format(direction="northeasterly"))
    assert not [c for c in bad if c.is_curve], bad
    print("PASS: a non-tangential curve whose stated direction matches neither perpendicular "
          "of its own radius bearing is dropped, not guessed at")


def test_to_course_objects_rejects_malformed_schema() -> None:
    """Confirmed real (covid 4981, Collin): without tool-input strict mode,
    Claude can occasionally return a `courses` entry that doesn't match
    COURSE_EXTRACTION_TOOL's own declared schema (e.g. plain strings instead
    of course objects) on a genuinely messy, multi-tract Exhibit A -- even
    though the identical call succeeds on a retry. to_course_objects must
    raise a clear ValueError, not a bare TypeError/KeyError, so
    extract_courses_with_escalation can catch it and escalate to the next
    tier instead of the whole anchor-resolution attempt crashing."""
    try:
        to_course_objects({"courses": ["not a real course object", "another string"]})
        assert False, "expected a ValueError"
    except ValueError as exc:
        assert "schema" in str(exc), exc
    print("PASS: to_course_objects -> malformed course entries raise a clear ValueError, not a bare crash")


def _covid_5838_excepted_segments() -> list[tuple[str, float]]:
    """Segment covid 5838's SAVE AND EXCEPT block on each tract's own
    "containing N acres" terminator rather than on its header. There are SIX
    excepted tracts, not five: the 3.282 acre tract's header is missing from the
    OCR entirely and the deed's own numbering skips 6, so any header-driven split
    silently loses land. The acreage terminator is present on every one."""
    with get_session() as session:
        text = " ".join((get_deed_text(session, 5838) or "").split())
    start = text.find("SAVE AND EXCEPT THE FOLLOWING")
    assert start != -1, "SAVE AND EXCEPT block not found"
    segments, prev = [], start
    for m in re.finditer(r"containing\s+([\d.,]+)\s+acres", text[start:]):
        end = start + m.end()
        segments.append((text[prev:end], float(m.group(1).replace(",", ""))))
        prev = end
    return segments


def test_covid_5838_save_and_except_tracts_all_close() -> None:
    """All six SAVE AND EXCEPT tracts must parse and close, because they are
    subtracted from the encumbered land -- an unparsed carve-out silently
    over-states what the covenant covers.

    Each of the six exposed a distinct OCR defect that had been dropping courses
    outright: `central \u00abgle of` for "central angle of"; `a dius of` for "a
    radius of"; a doubled seconds mark (`33\u00b022'23"'`); "feet" reduced to
    `\u201cet`; a dollar sign for the 5 in `$6\u00b037'37"`; and, in TRACT 7, a
    quadrant letter that is simply wrong in the deed.

    Tolerances differ by tract for one principled reason: curves are walked as
    CHORDS, so a curve-heavy tract's area is legitimately short by the circular
    segments between each arc and its chord. On the 3.282 acre tract that is
    0.0971 ac (curve right, bulging out) minus 0.0033 ac (curve left, bulging
    in) = 0.0938 ac against an observed 0.0940 -- fully explained, so the
    closure error, not the area, is what proves these correct."""
    total_stated = total_walked = 0.0
    for segment, stated in _covid_5838_excepted_segments():
        courses, _ = repair_quadrant_by_closure(extract_courses(segment))
        assert courses, f"{stated} ac tract parsed to zero courses"
        result = walk_traverse(courses)
        assert result["closure_error_ft"] < 1.0, (stated, result["closure_error_ft"])
        assert abs(result["area_acres"] - stated) / stated < 0.035, (stated, result["area_acres"])
        total_stated += stated
        total_walked += result["area_acres"]
    assert len(_covid_5838_excepted_segments()) == 6, "expected six excepted tracts"
    assert abs(total_stated - 15.350) < 0.001, total_stated
    assert abs(total_walked - total_stated) < 0.14, (total_walked, total_stated)
    print(f"PASS: covid 5838 -> all 6 SAVE AND EXCEPT tracts close; "
          f"{total_walked:.3f} ac walked vs {total_stated:.3f} ac stated")


def test_quadrant_repair_fixes_covid_5838_tract_7() -> None:
    """covid 5838's 0.554 acre TRACT 7 parses cleanly and its area comes out
    right, but the traverse misses its own Point of Beginning by 811.64 feet:
    the closing call reads "North 56\u00b037'37\" East ... 485.94 feet to the
    Point of Beginning", which runs back OUT along Beach Access Road No. 1
    instead of returning down it. The three preceding courses independently
    demand 485.92 ft on azimuth 303.39\u00b0; N56\u00b037'37"W is azimuth
    303.373\u00b0. Distance and angle were transcribed correctly -- only the
    quadrant letter is wrong."""
    segment = next(seg for seg, ac in _covid_5838_excepted_segments() if ac == 0.554)
    courses = extract_courses(segment)
    assert walk_traverse(courses)["closure_error_ft"] > 800

    repaired, repair = repair_quadrant_by_closure(courses)
    assert repair is not None, "the quadrant error was not recovered"
    assert repair["course_index"] == 3, repair
    assert repair["field"] == "ew" and repair["from"] == "East" and repair["to"] == "West", repair
    assert abs(repair["distance_ft"] - 485.94) < 0.01, repair
    assert walk_traverse(repaired)["closure_error_ft"] < 0.1, repair
    print(f"PASS: quadrant repair -> {repair['bearing_before']} read as {repair['bearing_after']}, "
          f"closure {repair['closure_before_ft']:.2f} -> {repair['closure_after_ft']:.2f} ft")


def test_quadrant_repair_leaves_a_sound_traverse_alone() -> None:
    """The repair must never fire on a traverse that already closes -- otherwise
    it would be free to 'improve' correct data. covid 5838's 1.582 acre excepted
    tract closes to 0.00 ft and must come back as the very same list object."""
    segment = next(seg for seg, ac in _covid_5838_excepted_segments() if ac == 1.582)
    courses = extract_courses(segment)
    before = walk_traverse(courses)
    assert before["closure_error_ft"] < 0.1, before
    repaired, repair = repair_quadrant_by_closure(courses)
    assert repair is None, repair
    assert repaired is courses
    assert walk_traverse(repaired)["closure_error_ft"] == before["closure_error_ft"]
    print("PASS: a traverse that already closes is returned untouched by the quadrant repair")


def test_quadrant_repair_refuses_an_ambiguous_case() -> None:
    """A repair is only a correction when the closure identifies it uniquely. A
    two-course stub is open no matter which letter is flipped, so nothing may be
    changed -- the bad closure has to survive as the signal it is meant to be."""
    stub = [Course(ns="North", degrees=45, minutes=0, seconds=0, ew="East", distance_ft=100.0),
            Course(ns="North", degrees=45, minutes=0, seconds=0, ew="East", distance_ft=250.0)]
    repaired, repair = repair_quadrant_by_closure(stub)
    assert repair is None, repair
    assert repaired is stub
    print("PASS: an ambiguous traverse is left untouched rather than guessed at")


def test_ocr_decimal_comma_is_not_read_as_a_thousands_separator() -> None:
    """A live, corpus-wide bug, not a 5839 quirk. Distances were parsed with
    `float(raw.replace(",", ""))`, so covid 5839's "a tangent length of 244,30
    feet" became 24,430 ft -- a silent hundredfold error that corrupts a
    traverse instead of failing loudly, which is the worst way for this to go
    wrong.

    A comma means a decimal point only when exactly two digits follow it and
    there is no real decimal point. Genuine thousands separators always have
    three digits after them, so the two can never be confused."""
    assert _feet("244,30") == 244.30
    assert _feet("128,32") == 128.32
    assert _feet("1270,00") == 1270.00
    assert _feet("1,516.17") == 1516.17          # real thousands separator
    assert _feet("2,364.98") == 2364.98
    assert _feet("7,233") == 7233.0              # thousands, no decimals at all
    assert _feet("160.00") == 160.00
    print("PASS: '244,30 feet' reads as 244.30 ft, not 24,430 -- thousands separators unaffected")


def test_point_of_curvature_curve_needs_no_radius_bearing() -> None:
    """covid 5839's third curve family. A curve at a POINT OF CURVATURE states
    no radius bearing because it does not need one: by definition it is tangent
    to the course arriving there, so the tangent IS that course's azimuth.

    Also exercises this deed's own wording and OCR: "delta angle" where every
    earlier deed said "central angle", and "with the circular curve" without
    the "said" the previous pattern required."""
    text = (
        'THENCE North 00 degrees 00 minutes 00 seconds East, a distance of 100.00 feet to a '
        '5/8 inch iron rod set for the point of curvature of a circular curve to the right '
        'which has a delta angle of 90 degrees 00\'00", a radius of 100.00 feet, a tangent '
        'length of 100.00 feet and an arc length of 157.08 feet; '
        'THENCE, with the circular curve to the right, an arc length of 157.08 feet to a point;'
    )
    courses = extract_courses(text)
    curves = [c for c in courses if c.is_curve]
    assert len(curves) == 1, courses
    chord = curves[0]
    # tangent = the arriving course's due-north azimuth; a 90-degree right turn
    # puts the chord at N45E, length 2*100*sin(45) = 141.42 ft
    assert chord.ns == "North" and chord.ew == "East", chord
    assert abs(chord.azimuth_degrees - 45.0) < 0.01, chord
    assert abs(chord.distance_ft - 141.42) < 0.05, chord
    print(f"PASS: point-of-curvature curve -> chord N45E {chord.distance_ft:.2f} ft, "
          f"tangent taken from the arriving course (no radius bearing stated)")


def test_a_curve_with_no_radius_bearing_and_no_previous_course_is_dropped() -> None:
    """The guard on the above: with nothing arriving at the point of curvature
    there is no tangent to inherit, and a chord must not be invented."""
    text = (
        'BEGINNING at a point for the point of curvature of a circular curve to the right '
        'which has a delta angle of 90 degrees 00\'00", a radius of 100.00 feet and an arc '
        'length of 157.08 feet; THENCE, with the circular curve to the right, an arc length '
        'of 157.08 feet to a point;'
    )
    assert not [c for c in extract_courses(text) if c.is_curve], "invented a chord with no tangent"
    print("PASS: a point-of-curvature curve with no arriving course is dropped, not guessed at")


if __name__ == "__main__":
    test_spelled_out_bearing_units()
    test_curly_quote_minutes_marker()
    test_deed_annotation_between_distance_and_to()
    test_compound_bearing_list_without_thence_or_to()
    test_compound_bearing_excludes_monument_ties()
    test_covid_5838_primary_tract_closes()
    test_non_tangential_curve_direction_must_be_stated()
    test_to_course_objects_rejects_malformed_schema()
    test_covid_5838_save_and_except_tracts_all_close()
    test_quadrant_repair_fixes_covid_5838_tract_7()
    test_quadrant_repair_leaves_a_sound_traverse_alone()
    test_quadrant_repair_refuses_an_ambiguous_case()
    test_ocr_decimal_comma_is_not_read_as_a_thousands_separator()
    test_point_of_curvature_curve_needs_no_radius_bearing()
    test_a_curve_with_no_radius_bearing_and_no_previous_course_is_dropped()
    print("\nall metes-and-bounds parser tests passed")
