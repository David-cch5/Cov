"""Tests for repair_distance_by_closure -- the repair that was missing.

repair_bearing_by_closure and repair_quadrant_by_closure have existed here for
weeks. The distance case had not been built, so a misread DISTANCE was the one
single-value defect nothing in this codebase could recover, and covid 4981's
55.73 ac tract sat unread because of it.

The method is the one the other two already use, with one addition the geometry
hands over free: changing a course's distance moves the closure vector along
THAT COURSE'S bearing and nowhere else, so the direction of the misclosure says
which courses can possibly be at fault. It is a solve, not a search.

Usage: python3 scripts/test_distance_repair.py
"""
import sys

sys.path.insert(0, ".")

from app.ingestion.exha_sheet import read_sheet
from app.parsing.legal_description.metes_bounds import (
    extract_courses, repair_distance_by_closure, replace, walk_traverse)


def _sheet_row(number: int):
    return [r for r in read_sheet() if r.row_number == number][0]


def test_a_planted_transposition_is_recovered_exactly() -> None:
    """Ground truth: covid 4981's Young tract closes at 1:186,912 as it stands.
    Transpose two digits of its longest course and the repair must put back the
    original figure -- not merely some figure that closes."""
    courses = extract_courses(_sheet_row(1666).text)
    truth = walk_traverse(courses)
    assert truth["closure_error_ft"] < 0.05, truth["closure_error_ft"]

    index = max(range(len(courses)), key=lambda i: courses[i].distance_ft)
    original = courses[index].distance_ft          # 839.30
    whole, frac = f"{original:.2f}".split(".")
    broken = list(courses)
    broken[index] = replace(courses[index],
                            distance_ft=float(f"{whole[1]}{whole[0]}{whole[2:]}.{frac}"))
    assert walk_traverse(broken)["closure_error_ft"] > 100

    repaired, diag = repair_distance_by_closure(broken, stated_acres=11.878)
    assert diag["repaired"], diag
    assert diag["course_index"] == index, diag
    assert abs(diag["now_ft"] - original) < 0.01, diag
    assert diag["closure_error_ft"] < 0.05, diag
    assert "transposed" in diag["how"], diag["how"]
    print(f"PASS: {diag['was_ft']} -> {diag['now_ft']} recovered ({diag['how']}), "
          f"closure back to {diag['closure_error_ft']:.3f} ft")


def test_it_refuses_when_the_two_constraints_disagree() -> None:
    """Covid 4981's 55.73 ac tract, and the reason it is still open. Two single
    distance corrections are each plausible misreadings, and they disagree:

      course 28, 30.00 -> 80.00   closure 6.49 ft, area 0.027% off stated
      course  3, 534.00 -> 543.00 closure 47.18 ft, area 0.091% off

    and applying BOTH improves closure to 3.54 ft while taking the area to
    0.669% off -- worse than either alone. Nothing here is proven, so nothing is
    applied; the candidates are reported as leads instead. A repair that
    satisfies one constraint is not a finding, which is the rule that keeps this
    from writing a number into a deed reading.
    """
    courses = extract_courses(_sheet_row(1667).text)
    assert len(courses) == 34, len(courses)
    _, diag = repair_distance_by_closure(courses, stated_acres=55.73)
    assert diag["repaired"] is False, diag
    assert diag["candidates"] == 0, diag
    assert diag["leads"], "a decline must still hand over what it saw"
    best = diag["leads"][0]
    assert best["course_index"] == 27, best
    assert abs(best["now_ft"] - 80.00) < 0.01, best
    assert best["area_deviation"] < 0.001, best
    assert best["closure_error_ft"] < 10, best
    print(f"PASS: declines and reports {len(diag['leads'])} lead(s); the best is course "
          f"{best['course_index']+1} {best['was_ft']} -> {best['now_ft']} "
          f"({best['how']}), area {best['area_deviation']*100:.3f}% off, closure "
          f"{best['closure_error_ft']:.2f} ft")


def test_the_misclosure_bearing_narrows_the_suspects() -> None:
    """What makes this a solve: a distance defect can only move the closure
    vector along its own course's bearing, so the gap's direction rules out
    every course not parallel to it. Covid 4981's tract 3 misses by 56.17 ft on
    a bearing of 88.5 degrees -- nearly due east -- so the suspects are its
    east-west courses, not all 34."""
    courses = extract_courses(_sheet_row(1667).text)
    _, diag = repair_distance_by_closure(courses, stated_acres=55.73)
    assert 85 < diag["gap_bearing_azimuth"] < 92, diag["gap_bearing_azimuth"]
    assert abs(diag["gap_length_ft"] - 56.17) < 0.05, diag["gap_length_ft"]
    for lead in diag["leads"]:
        azimuth = courses[lead["course_index"]].azimuth_degrees
        offset = min(abs((azimuth - diag["gap_bearing_azimuth"] + 180) % 360 - 180),
                     abs(abs((azimuth - diag["gap_bearing_azimuth"] + 180) % 360 - 180) - 180))
        assert offset <= 3.0, (lead, azimuth, offset)
    print(f"PASS: gap of {diag['gap_length_ft']:.2f} ft bears "
          f"{diag['gap_bearing_azimuth']:.1f} deg; every lead lies within 3 deg of it")


def test_a_sound_traverse_is_left_alone() -> None:
    courses = extract_courses(_sheet_row(1666).text)
    repaired, diag = repair_distance_by_closure(courses, stated_acres=11.878)
    assert diag is None and repaired == courses
    print("PASS: a traverse that already closes is returned untouched")


if __name__ == "__main__":
    test_a_planted_transposition_is_recovered_exactly()
    test_it_refuses_when_the_two_constraints_disagree()
    test_the_misclosure_bearing_narrows_the_suspects()
    test_a_sound_traverse_is_left_alone()
    print("\nall distance-repair tests passed")
