"""Diff two readings of one tract description, course by course.

The check that was missing, and the reason it cost days. Covid 4981's tract 3
had its course 7 retyped as South 11°24'24" East where the recorded instrument
reads South 13°24'24" East -- two degrees on a 289.82 ft course, about 10 ft of
displacement, and the whole east-west discrepancy the closure math had been
pointing at. Both readings were on disk. I compared them by eye, said they
agreed, and was wrong.

Comparing readings is mechanical and belongs in code. app/ingestion/text_compare.py
compares a tract's ACREAGE and ABSTRACT across sources -- enough to tell that two
rows describe the same land, not enough to tell that they describe it differently.
This closes that gap.

Two design points earn their keep:

  DIFFERENCES ARE RANKED BY DISPLACEMENT, not by course number. A tenth of a foot
  on a 21 ft jog does not matter and a two-degree bearing on a 290 ft course moves
  the boundary ten feet; reading a diff top-down should put the second first. The
  displacement of a difference is the length of the vector between the two versions
  of that course, which is exact rather than a heuristic.

  THE READINGS MAY NOT HAVE THE SAME NUMBER OF COURSES. One may drop a call the
  other reads, or carry a reconstructed one -- covid 4981's corrected reading has
  35 courses against the sheet's 34. So courses are ALIGNED rather than zipped,
  and an unmatched course is reported as present in one reading only. Zipping past
  a missing call renumbers everything after it and turns one difference into
  thirty.
"""
import difflib
import math

# Distances agreeing to this are the same call; the alignment keys on distance
# because a bearing misread leaves the distance intact far more often than the
# other way round, which is what keeps a bearing difference aligned instead of
# reading as an insert plus a delete.
_ALIGN_ROUNDING = 2
_TRIVIAL_DISPLACEMENT_FT = 0.02


def _vector(course) -> tuple[float, float]:
    azimuth = math.radians(course.azimuth_degrees)
    return (course.distance_ft * math.sin(azimuth),
            course.distance_ft * math.cos(azimuth))


def _bearing_text(course) -> str:
    return (f"{course.ns} {course.degrees:02.0f}°{course.minutes:02.0f}'"
            f"{course.seconds:02.0f}\" {course.ew}")


def _describe(course) -> str:
    return f"{_bearing_text(course)} {course.distance_ft:.2f} ft"


def displacement_ft(a, b) -> float:
    """How far apart the two versions of a course put the next corner.

    The exact figure, not an approximation: the length of the difference between
    the two course vectors. A pure bearing difference gives the chord across the
    angle; a pure distance difference gives the difference in length; a course
    differing in both gives their combination.
    """
    ax, ay = _vector(a)
    bx, by = _vector(b)
    return math.hypot(ax - bx, ay - by)


def diff_readings(courses_a: list, courses_b: list,
                  label_a: str = "A", label_b: str = "B") -> dict:
    """Align two readings of one description and report every difference.

    Returns the differences ranked by displacement, plus the courses present in
    only one reading. Says nothing about which reading is right -- that is what
    closure, area and the recorded instrument are for.
    """
    keys_a = [round(c.distance_ft, _ALIGN_ROUNDING) for c in courses_a]
    keys_b = [round(c.distance_ft, _ALIGN_ROUNDING) for c in courses_b]
    matcher = difflib.SequenceMatcher(a=keys_a, b=keys_b, autojunk=False)

    differences, only_a, only_b = [], [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            # Same distance -- the bearing may still differ, which is exactly the
            # case that hid for two days.
            for offset in range(i2 - i1):
                a, b = courses_a[i1 + offset], courses_b[j1 + offset]
                moved = displacement_ft(a, b)
                if moved > _TRIVIAL_DISPLACEMENT_FT:
                    differences.append({
                        "course_a": i1 + offset + 1, "course_b": j1 + offset + 1,
                        "field": "bearing", "displacement_ft": moved,
                        label_a: _describe(a), label_b: _describe(b),
                    })
            continue
        if tag == "replace":
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                a, b = courses_a[i1 + offset], courses_b[j1 + offset]
                moved = displacement_ft(a, b)
                same_bearing = abs(a.azimuth_degrees - b.azimuth_degrees) < 1e-6
                differences.append({
                    "course_a": i1 + offset + 1, "course_b": j1 + offset + 1,
                    "field": "distance" if same_bearing else "bearing and distance",
                    "displacement_ft": moved,
                    label_a: _describe(a), label_b: _describe(b),
                })
            only_a.extend({"course": i + 1, "course_text": _describe(courses_a[i])}
                          for i in range(i1 + paired, i2))
            only_b.extend({"course": j + 1, "course_text": _describe(courses_b[j])}
                          for j in range(j1 + paired, j2))
            continue
        if tag == "delete":
            only_a.extend({"course": i + 1, "course_text": _describe(courses_a[i])}
                          for i in range(i1, i2))
        elif tag == "insert":
            only_b.extend({"course": j + 1, "course_text": _describe(courses_b[j])}
                          for j in range(j1, j2))

    differences.sort(key=lambda d: -d["displacement_ft"])
    return {
        "labels": (label_a, label_b),
        "courses": (len(courses_a), len(courses_b)),
        "differences": differences,
        f"only_in_{label_a}": only_a,
        f"only_in_{label_b}": only_b,
        "largest_displacement_ft": differences[0]["displacement_ft"] if differences else 0.0,
        "agree": not differences and not only_a and not only_b,
    }


def format_diff(result: dict) -> str:
    """The diff as a person reads it: worst first."""
    label_a, label_b = result["labels"]
    count_a, count_b = result["courses"]
    lines = [f"{label_a}: {count_a} courses   {label_b}: {count_b} courses"]
    if result["agree"]:
        lines.append("  the two readings agree on every course")
        return "\n".join(lines)
    for d in result["differences"]:
        lines.append(f"  course {d['course_a']}/{d['course_b']}  {d['field']}, "
                     f"moves the corner {d['displacement_ft']:.2f} ft")
        lines.append(f"      {label_a}: {d[label_a]}")
        lines.append(f"      {label_b}: {d[label_b]}")
    for key in (f"only_in_{label_a}", f"only_in_{label_b}"):
        for entry in result[key]:
            lines.append(f"  {key.replace('_', ' ')}: course {entry['course']}  "
                         f"{entry['course_text']}")
    return "\n".join(lines)
