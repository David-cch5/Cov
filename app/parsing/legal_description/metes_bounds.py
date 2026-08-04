"""Deterministic metes-and-bounds parsing: extract bearing/distance calls from raw
survey text and walk them into a polygon. Pure trigonometry, never an LLM call --
see the design discussion this followed: the shape is exact math, but anchoring
that shape to real-world coordinates is a separate, much harder problem (see
resolve_metes_and_bounds_anchor in app/gis/anchor_resolver.py -- the tiered
deterministic-then-LLM-escalation orchestrator this project actually built for
that problem, never guessed at).
"""
import math
import re
from dataclasses import dataclass

_COURSE_RE = re.compile(
    r"THENCE\s+(North|South)\s+(\d{1,3})\s*(?:degrees?|deg\.?|°)\s*(\d{1,2})\s*(?:minutes?|min\.?|'|’)\s*(\d{1,2})\s*"
    r"(?:seconds?|sec\.?|\"|”)?\s+(East|West)"
    # The intervening text lists adjoiner tracts by ACREAGE, not feet, so the first
    # "<number> feet to <something>" after the bearing is reliably the course's own
    # distance -- more robust than anchoring on the literal phrase "distance of",
    # which OCR sometimes splits across a line break.
    # Confirmed real (covid 5838/Nueces): a surveyor's own "(Deed = X feet)"
    # annotation -- comparing a re-measured distance against the original deed's
    # recited figure -- sometimes sits directly between the real distance and
    # "to", e.g. "1255.59 feet (Deed = 1254.90 feet) to a point"; tolerated here
    # as an optional aside rather than treated as the course's own distance.
    r"[^;]*?([\d,]+\.?\d*)\s*feet\s*(?:\(Deed[^)]*\))?\s+to\b",
    re.IGNORECASE | re.DOTALL,
)

# Confirmed real (covid 5838/Nueces): a run of courses along a single meandering
# natural boundary (mean high tide line) is sometimes recited as ONE "THENCE
# following said mean high tide line ...:" lead-in followed by several bare
# "<bearing>, a distance of <N> feet;" calls with no repeated THENCE and no "to
# <landmark>" -- _COURSE_RE's own THENCE-per-call and "feet to" requirements
# both miss these outright. Matched only where the compact ", a distance of ...
# feet;" phrasing is literally present, so this never overlaps what _COURSE_RE
# already catches (which always ends in "to", not a bare semicolon).
_COMPOUND_BEARING_RE = re.compile(
    r"(North|South)\s+(\d{1,3})\s*(?:degrees?|deg\.?|°)\s*(\d{1,2})\s*(?:minutes?|min\.?|'|’)\s*(\d{1,2})\s*"
    r"(?:seconds?|sec\.?|\"|”)?\s+(East|West)\s*,\s*a distance of\s+([\d,]+\.?\d*)\s*feet\s*(?:\(Deed[^)]*\))?\s*;",
    re.IGNORECASE,
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
    closure, which is the point (flag it, don't silently accept it).

    Combines _COURSE_RE's THENCE-led calls with _COMPOUND_BEARING_RE's bare
    semicolon-terminated calls (see that regex's own docstring) and re-sorts by
    position in the source text -- the two patterns are mutually exclusive in
    what they match (one requires a trailing "to", the other a trailing bare
    ";"), so this never double-counts a course, but a compound-list run can be
    interleaved between THENCE-led courses and must come back in true traversal
    order, not "all THENCE courses, then all compound ones"."""
    matches: list[tuple[int, Course]] = []
    for m in _COURSE_RE.finditer(text):
        ns, deg, minute, sec, ew, dist = m.groups()
        matches.append((m.start(), Course(
            ns=ns, degrees=float(deg), minutes=float(minute), seconds=float(sec),
            ew=ew, distance_ft=float(dist.replace(",", "")),
        )))
    for m in _COMPOUND_BEARING_RE.finditer(text):
        # Confirmed real (covid 5838/Nueces): this pattern's own "<bearing>, a
        # distance of N feet;" shape also matches monument/reference TIES
        # embedded in a curve call's descriptive text -- "for a corner of this
        # tract from which a 60D nail bears North 35°43'56" East, a distance of
        # 2.28 feet;" is where a nail sits relative to the corner, not a call
        # walking the boundary itself. A real THENCE-style call is never phrased
        # as "<landmark> bears <direction>" -- checking for that verb in the few
        # words right before the bearing reliably tells the two apart without
        # needing to hand-list every possible tie phrasing.
        if "bears" in text[max(0, m.start() - 20):m.start()].lower():
            continue
        ns, deg, minute, sec, ew, dist = m.groups()
        matches.append((m.start(), Course(
            ns=ns, degrees=float(deg), minutes=float(minute), seconds=float(sec),
            ew=ew, distance_ft=float(dist.replace(",", "")),
        )))
    matches.sort(key=lambda pair: pair[0])
    return [c for _, c in matches]


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
