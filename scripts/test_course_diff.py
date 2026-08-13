"""Tests for the course-by-course diff between two readings.

This is the check whose absence cost days on covid 4981's tract 3. The reviewed
sheet retyped course 7 as South 11 24 24 East where the recorded instrument reads
South 13 24 24 East -- 2 degrees on a 289.82 ft course, 10.12 ft of displacement,
and the entire east-west discrepancy the closure math kept pointing at. Both
readings were on disk the whole time; they were compared by eye and reported as
agreeing.

Usage: python3 scripts/test_course_diff.py
"""
import sys

sys.path.insert(0, ".")

from app.ingestion.corrected_text import load_correction
from app.ingestion.exha_sheet import read_sheet
from app.parsing.legal_description.course_diff import (
    diff_readings, displacement_ft, format_diff)
from app.parsing.legal_description.metes_bounds import extract_courses, replace


def _sheet(row_number: int):
    return extract_courses([r for r in read_sheet() if r.row_number == row_number][0].text)


def test_it_finds_the_two_degree_bearing_that_hid_for_days() -> None:
    instrument = extract_courses(load_correction(4981, "tract_3_cox_55730")["text"])
    got = diff_readings(_sheet(1667), instrument, "sheet", "instrument")
    assert got["courses"] == (34, 35), got["courses"]
    assert len(got["differences"]) == 1, got["differences"]
    worst = got["differences"][0]
    assert worst["field"] == "bearing", worst
    assert worst["course_a"] == 7 and worst["course_b"] == 7, worst
    assert "11°24'24\"" in worst["sheet"], worst["sheet"]
    assert "13°24'24\"" in worst["instrument"], worst["instrument"]
    assert 10.0 < worst["displacement_ft"] < 10.3, worst["displacement_ft"]
    # and the reconstructed arc call is reported as present in one reading only
    assert len(got["only_in_instrument"]) == 1, got["only_in_instrument"]
    assert "46.28 ft" in got["only_in_instrument"][0]["course_text"]
    assert not got["only_in_sheet"], got["only_in_sheet"]
    print(f"PASS: the 2-degree misread on course 7 is found and ranked first at "
          f"{worst['displacement_ft']:.2f} ft; the reconstructed chord is reported as "
          f"instrument-only")


def test_differences_are_ranked_by_what_they_move_not_by_course_number() -> None:
    """A tenth of a foot on a short jog must not outrank ten feet on a long line."""
    courses = _sheet(1666)
    broken = list(courses)
    short = min(range(len(courses)), key=lambda i: courses[i].distance_ft)
    long_ = max(range(len(courses)), key=lambda i: courses[i].distance_ft)
    broken[short] = replace(courses[short], distance_ft=courses[short].distance_ft + 0.10)
    broken[long_] = replace(courses[long_], degrees=courses[long_].degrees + 2)
    got = diff_readings(courses, broken, "recorded", "retyped")
    assert len(got["differences"]) == 2, got["differences"]
    first, second = got["differences"]
    assert first["displacement_ft"] > second["displacement_ft"]
    assert first["course_a"] == long_ + 1, (first, long_)
    print(f"PASS: the 2-degree bearing on the {courses[long_].distance_ft:.0f} ft course "
          f"({first['displacement_ft']:.2f} ft) outranks a 0.10 ft change on the "
          f"{courses[short].distance_ft:.0f} ft one ({second['displacement_ft']:.2f} ft)")


def test_a_dropped_call_does_not_renumber_everything_after_it() -> None:
    """Zipping two readings past a missing call turns one difference into thirty.
    The courses are aligned, so a dropped call is reported as exactly that."""
    courses = _sheet(1667)
    without = courses[:10] + courses[11:]
    got = diff_readings(courses, without, "full", "short")
    assert got["courses"] == (34, 33), got["courses"]
    assert len(got["only_in_full"]) == 1, got["only_in_full"]
    assert not got["differences"], got["differences"]
    print(f"PASS: one dropped call reads as one missing course, not "
          f"{len(courses) - 10} renumbered ones")


def test_identical_readings_agree() -> None:
    courses = _sheet(1666)
    got = diff_readings(courses, list(courses), "a", "b")
    assert got["agree"] and got["largest_displacement_ft"] == 0.0, got
    assert "agree on every course" in format_diff(got)
    print("PASS: a reading compared with itself reports agreement")


def test_displacement_is_the_vector_difference() -> None:
    courses = _sheet(1666)
    course = max(courses, key=lambda c: c.distance_ft)
    turned = replace(course, degrees=course.degrees + 2)
    import math
    expected = 2 * course.distance_ft * math.sin(math.radians(2) / 2)
    assert abs(displacement_ft(course, turned) - expected) < 0.01
    longer = replace(course, distance_ft=course.distance_ft + 7.5)
    assert abs(displacement_ft(course, longer) - 7.5) < 1e-6
    print(f"PASS: a 2-degree turn on {course.distance_ft:.0f} ft displaces "
          f"{expected:.2f} ft; a 7.5 ft lengthening displaces 7.50 ft")


if __name__ == "__main__":
    test_it_finds_the_two_degree_bearing_that_hid_for_days()
    test_differences_are_ranked_by_what_they_move_not_by_course_number()
    test_a_dropped_call_does_not_renumber_everything_after_it()
    test_identical_readings_agree()
    test_displacement_is_the_vector_difference()
    print("\nall course-diff tests passed")
