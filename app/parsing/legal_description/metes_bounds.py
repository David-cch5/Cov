"""Deterministic metes-and-bounds parsing: extract bearing/distance calls from raw
survey text and walk them into a polygon. Pure trigonometry, never an LLM call --
see the design discussion this followed: the shape is exact math, but anchoring
that shape to real-world coordinates is a separate, much harder problem (see
resolve_metes_and_bounds_tract in app/gis/classifier.py for why that stays
unresolved for now rather than guessed at).
"""
import math
import re
from dataclasses import dataclass

_COURSE_RE = re.compile(
    r"THENCE\s+(North|South)\s+(\d{1,3})\s*(?:deg\.?|°)\s*(\d{1,2})\s*(?:min\.?|')\s*(\d{1,2})\s*"
    r"(?:sec\.?|\")?\s+(East|West)"
    # The intervening text lists adjoiner tracts by ACREAGE, not feet, so the first
    # "<number> feet to <something>" after the bearing is reliably the course's own
    # distance -- more robust than anchoring on the literal phrase "distance of",
    # which OCR sometimes splits across a line break.
    r"[^;]*?([\d,]+\.?\d*)\s*feet\s+to\b",
    re.IGNORECASE | re.DOTALL,
)

_BEGINNING_RE = re.compile(r"BEGINNING\s+at\s+([^;]+);", re.IGNORECASE | re.DOTALL)


@dataclass
class Course:
    ns: str          # 'North' or 'South'
    degrees: float
    minutes: float
    seconds: float
    ew: str          # 'East' or 'West'
    distance_ft: float          # for a curve call, this is the CHORD distance
    # Curve calls (e.g. "an arc distance of 449.88 feet ... a chord bearing and
    # distance of N05°38'47"E, 440.67 feet") are walked using the chord bearing/
    # distance above like any other course -- the chord's two endpoints ARE the
    # real PC/PT corner points transcribed from the deed, so this fabricates
    # nothing; the only simplification is that the true boundary bows out
    # slightly along that one edge rather than following the arc exactly. These
    # fields are kept for audit/notes only, not used by walk_traverse.
    is_curve: bool = False
    radius_ft: float | None = None
    delta_deg: float | None = None
    curve_direction: str | None = None  # 'left' or 'right'
    arc_length_ft: float | None = None

    @property
    def azimuth_degrees(self) -> float:
        """Standard surveying azimuth: clockwise from North, 0-360."""
        angle = self.degrees + self.minutes / 60 + self.seconds / 3600
        ns, ew = self.ns.upper(), self.ew.upper()
        if ns == "NORTH" and ew == "EAST":
            return angle
        if ns == "SOUTH" and ew == "EAST":
            return 180 - angle
        if ns == "SOUTH" and ew == "WEST":
            return 180 + angle
        return 360 - angle  # NORTH / WEST


def extract_courses(text: str) -> list[Course]:
    """Pull every THENCE bearing/distance call out of raw survey text. Tolerant of
    the descriptive adjoiner-tract text between the bearing and the distance
    (surveys routinely interleave 'a called N acre tract described in a deed to...'
    clauses before finally stating the distance) -- but does NOT correct OCR
    errors in the numbers themselves; garbled digits will show up as a bad
    closure, which is the point (flag it, don't silently accept it)."""
    courses = []
    for m in _COURSE_RE.finditer(text):
        ns, deg, minute, sec, ew, dist = m.groups()
        courses.append(Course(
            ns=ns, degrees=float(deg), minutes=float(minute), seconds=float(sec),
            ew=ew, distance_ft=float(dist.replace(",", "")),
        ))
    return courses


def extract_point_of_beginning(text: str) -> str | None:
    m = _BEGINNING_RE.search(text)
    return m.group(1).strip() if m else None


def walk_traverse(courses: list[Course]) -> dict:
    """Walk the courses from an arbitrary local origin (0,0), in feet. Returns the
    vertex list, the closure error (distance from the last point back to the
    first), the closure ratio (error / perimeter -- the standard surveying QA
    figure), and the enclosed area via the shoelace formula."""
    x, y = 0.0, 0.0
    vertices = [(x, y)]
    perimeter = 0.0
    for c in courses:
        rad = math.radians(c.azimuth_degrees)
        x += c.distance_ft * math.sin(rad)
        y += c.distance_ft * math.cos(rad)
        vertices.append((x, y))
        perimeter += c.distance_ft

    closure_error_ft = math.hypot(vertices[-1][0] - vertices[0][0], vertices[-1][1] - vertices[0][1])
    closure_ratio = (closure_error_ft / perimeter) if perimeter else None

    area = 0.0
    pts = vertices[:-1]  # drop the duplicate closing point for the shoelace sum
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    area_sqft = abs(area) / 2.0
    area_acres = area_sqft / 43560.0

    return {
        "vertices": vertices,
        "perimeter_ft": perimeter,
        "closure_error_ft": closure_error_ft,
        "closure_ratio": closure_ratio,
        "area_acres": area_acres,
    }
