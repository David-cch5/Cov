"""Regression tests for app/parsing/legal_description/metes_bounds.py's course
extraction. No prior test file covered this despite two real, previously-live
parser bugs having been found and fixed against real covenant text (Collin
covid 3028, Nueces covid 5838) -- this closes that gap so a future regex
change can't silently re-break either case.

Usage: python3 scripts/test_metes_bounds.py
"""
import sys

sys.path.insert(0, ".")

from app.parsing.legal_description.metes_bounds import extract_courses, walk_traverse
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
    """End-to-end regression: all 15 real courses in covid 5838's primary
    318.779-acre tract (Nueces), combining every fix above, close to within a
    fraction of a percent of the stated acreage -- confirmed the remaining
    ~32 ft closure error is attributable to one small (31.95 ft arc) curve
    segment deliberately left unwalked (no chord bearing recited in the deed,
    and not independently derivable without guessing)."""
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
    assert len(courses) == 15, (len(courses), courses)

    result = walk_traverse(courses)
    stated_acreage = 318.779
    area_diff_pct = abs(result["area_acres"] - stated_acreage) / stated_acreage
    assert area_diff_pct < 0.005, (result["area_acres"], stated_acreage, area_diff_pct)
    # the one deliberately-unwalked curve segment (arc length 31.95 ft, no
    # recited chord bearing) should account for essentially all of the
    # remaining closure error -- confirms no OTHER course was mis-parsed.
    assert abs(result["closure_error_ft"] - 31.95) < 1.0, result["closure_error_ft"]
    print(f"PASS: covid 5838 primary tract -> {len(courses)} courses, "
          f"area={result['area_acres']:.2f} ac (stated {stated_acreage}), "
          f"closure_error={result['closure_error_ft']:.2f} ft (~= the one unwalked curve arc)")


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


if __name__ == "__main__":
    test_spelled_out_bearing_units()
    test_curly_quote_minutes_marker()
    test_deed_annotation_between_distance_and_to()
    test_compound_bearing_list_without_thence_or_to()
    test_compound_bearing_excludes_monument_ties()
    test_covid_5838_primary_tract_closes()
    test_to_course_objects_rejects_malformed_schema()
    print("\nall metes-and-bounds parser tests passed")
