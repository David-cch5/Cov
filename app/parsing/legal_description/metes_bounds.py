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
from dataclasses import dataclass, replace

# The THENCE token itself is an OCR casualty. Real variants in this corpus, all
# of which silently dropped a whole course before being tolerated here:
#   "Thence,"          covid 5838's SAVE AND EXCEPT tracts (comma, not space)
#   "‘hence,"          covid 5838 -- the leading T read as a curly open quote,
#                      which alone lost the 302.86 ft course out of TRACT JA
#   "TRENCR," / "TRENCE"  covid 4780
# Deliberately an explicit list of observed corruptions rather than dropping the
# requirement: without a leading token, the pattern would also match monument
# ties ("a National Geodetic Survey monument stamped \"SF-010\" bears North
# 16°04'47\" East 5791.31 feet"), which are not courses.
_THENCE = r"[‘’'\"`]?\s*(?:THENCE|THENCR|TRENCE|TRENCR|HENCE|TENCE)"

# The seconds mark is sometimes doubled by OCR -- covid 5838's SAVE AND EXCEPT
# TRACT 5 recites `South 33°22'23"' West`, with a stray apostrophe after the
# closing double quote. A single optional mark cannot match the pair, so the
# whole 3.26 ft course was dropped. Accepting a RUN of mark characters costs
# nothing (they are all noise between the seconds digits and the quadrant).
# covid 5839's scan carries the page's own line-edge artifacts INSIDE bearings --
# "90 degrees : 00'29\"", "51'16\" : Last", "curve to the left, whose radius point
# bears". A colon, semicolon or pipe lands where only whitespace is expected. They
# appear exclusively in SEPARATOR positions, never inside a value, so tolerating
# them between the parts of a bearing costs no precision: the digits, the marks
# and the quadrant words are all still matched exactly.
_SEP = r"[\s:;|]*"

_SECONDS_MARK = r"(?:seconds?|sec\.?|[\"”'’]+)?"

# "feet" is itself an OCR casualty: covid 5838 TRACT 5 recites the 2.59 ft course
# as `a distance of 2.59 “et`, having eaten the "fe" entirely, and elsewhere
# writes `184.66 “eet`. Restricted to the shapes actually observed (a quote mark
# standing in for the lost letters) rather than anything ending in "et", so a
# stray word can never be read as a unit of measure.
_FEET = r"(?:feet|fe?et|[“”‘’]e{1,2}t|ft\.?)"

_COURSE_RE = re.compile(
    _THENCE + r"[,;:]?\s+(North|South)\s*(\d{1,3})\s*(?:degrees?|deg\.?|°)" + _SEP + r"(\d{1,2})\s*(?:minutes?|min\.?|'|’)" + _SEP + r"(\d{1,2})\s*" + _SECONDS_MARK + r"" + _SEP + r"\s*(East|West)"
    # The intervening text lists adjoiner tracts by ACREAGE, not feet, so the first
    # "<number> feet to <something>" after the bearing is reliably the course's own
    # distance -- more robust than anchoring on the literal phrase "distance of",
    # which OCR sometimes splits across a line break.
    # Confirmed real (covid 5838/Nueces): a surveyor's own "(Deed = X feet)"
    # annotation -- comparing a re-measured distance against the original deed's
    # recited figure -- sometimes sits directly between the real distance and
    # "to", e.g. "1255.59 feet (Deed = 1254.90 feet) to a point"; tolerated here
    # as an optional aside rather than treated as the course's own distance.
    #
    # A call can end "for" instead of "to" -- confirmed real throughout covid
    # 5838's own SAVE AND EXCEPT tracts ("a distance of 50.00 feet FOR the south
    # corner (no monumentation found or set) of this tract"), where a corner is
    # calculated rather than monumented so there is no object to run "to". This
    # stays safe because the terminator must follow "feet" immediately: the
    # intermediate "at 1125.15 feet PASS a 5/8 inch iron rod" recitals are still
    # skipped, and the non-greedy match still lands on the total distance.
    r"[^;]*?([\d,]+\.?\d*)\s*" + _FEET + r"\s*(?:\(Deed[^)]*\))?\s+(?:to|for)\b",
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
    r"(North|South)\s+(\d{1,3})\s*(?:degrees?|deg\.?|°)" + _SEP + r"(\d{1,2})\s*(?:minutes?|min\.?|'|’)" + _SEP + r"(\d{1,2})\s*" + _SECONDS_MARK + r"\s+(East|West)\s*,\s*a distance of\s+([\d,]+\.?\d*)\s*feet\s*(?:\(Deed[^)]*\))?\s*;",
    re.IGNORECASE,
)

# Confirmed real, and precisely diagnosed (covid 5838/Nueces): a curve call can
# carry NO bearing of its own --
#
#   "... for the point of curvature of a non-tangential circular curve to the
#    right having a central angle of 00°57'31", a radius of 1909.86 feet, and an
#    arc length of 31.95 feet from which the radius bears North 31°46'05" Kast
#    ...; THENCE in a northwesterly direction, an arc distance of 31.95 feet to
#    a 5/8 inch iron rod found for the point of tangency ..."
#
# The direction is implied by the curve's own geometry, so _COURSE_RE (which
# requires North/South...East/West) drops it silently. The cost is exact and
# measurable: the parsed traverse's closure error was 31.95 ft -- the missing
# arc's own length, to 0.004 ft -- giving a 1:674 closure on what is otherwise a
# clean traverse.
#
# The curve is explicitly NON-TANGENTIAL, so the previous course's bearing says
# nothing about it. What makes it solvable is that the deed states the RADIUS
# BEARING at the point of curvature: the tangent there is perpendicular to the
# radius, and the chord is that tangent rotated by half the central angle,
# toward the side the curve turns. Verified against covid 5838's own traverse:
# the computed chord (North 57°45'09" West, 31.953 ft) closes the traverse to
# within 0.015 ft of the measured gap.
#
# "Kast" is not a typo here -- it is the real OCR of "East" in this document, so
# the quadrant letter tolerates the handful of substitutions actually seen
# rather than being silently unmatchable.
# "central \u00abgle of" is a real OCR rendering of "central angle of" in covid
# 5838 -- it alone dropped that tract's 364.77 ft arc. Matching "<anything>gle"
# tolerates it without loosening the surrounding structure.
# covid 5838 renders "bears North 56°37'37\" West" as "North $6°37'37\" West":
# a dollar sign for the 5. The radius bearing sits inside a very rigid phrase
# ("whose radius point bears <N/S> ... <E/W> <N> feet"), so tolerating the
# standard OCR digit confusions there cannot match anything else -- and any
# wrong reading is caught immediately, because the tangent it implies would no
# longer be perpendicular to the traverse and the tract would not close.
_D = r"[\dOolIB$]"
_OCR_DIGITS = str.maketrans({"$": "5", "O": "0", "o": "0", "l": "1", "I": "1", "B": "8"})


def _ocr_int(raw: str) -> float:
    """Read an integer that may carry OCR letter-for-digit substitutions."""
    return float(raw.translate(_OCR_DIGITS))


# covid 5839 renders one SF-010 tie as "North 24 degrees 51'16\" Last 4,358.04
# feet" -- an L for the E. Without it that tie is lost and the tract falls back
# to a single, uncheckable KNOLL tie.
# The angle a curve turns through is called a "central angle" in covid 5838 and
# a "delta angle" in covid 5839, and OCR mangles the noun itself -- "central
# \u00abgle of", "delta angie of". Matching the keyword, then ANY single token,
# then "of" tolerates every observed form: the keyword plus one word plus "of",
# inside a curve clause, is already specific enough that nothing else matches.
# "circular" is misspelled in the deed itself, not just by OCR -- covid 5839
# writes "cirucular" and "ciruclar" in two different arc calls, and vision OCR
# transcribes both faithfully because they are what the page says. cir\w{0,6}lar
# covers the correct spelling and both transpositions without matching anything
# else in a curve clause.
_CIRCULAR = r"cir\w{0,6}lar"

_DELTA_KEYWORD = r"(?:central|delta)\s+\S{1,12}\s+of"

_EAST = r"(?:[EKBFRL]ast|E\.?)"
_WEST = r"(?:W[ae]st|W\.?)"
# A bearing, written once. Splicing _SEP into these patterns by hand had already
# broken one of them silently -- an rf-string became a plain r-string, so {_D}
# stopped interpolating and the minutes field literally required the text
# "{_D}{1,2}", which matches nothing. Defining the shape once removes that whole
# class of mistake. _DMS_OCR additionally tolerates the letter-for-digit
# substitutions seen in radius bearings (see _D).
_DMS = (r"(\d{1,3})\s*(?:degrees?|deg\.?|°)" + _SEP +
        r"(\d{1,2})\s*(?:minutes?|min\.?|['’])" + _SEP +
        r"(\d{1,2})\s*" + _SECONDS_MARK)
_DMS_OCR = (r"(" + _D + r"{1,3})\s*(?:degrees?|deg\.?|°)" + _SEP +
            r"(" + _D + r"{1,2})\s*(?:minutes?|min\.?|['’])" + _SEP +
            r"(" + _D + r"{1,2})\s*" + _SECONDS_MARK)

_NON_TANGENTIAL_CURVE_RE = re.compile(
    r"curve\s+to\s+the\s+(right|left)\s+having\s+a\s+" + _DELTA_KEYWORD + r"\s*" + _DMS +
    r".{0,80}?\S*dius\s+of\s+([\d,]+\.?\d*)\s*" + _FEET +
    r".{0,120}?radius\s+bears\s+(North|South)\s*" + _DMS_OCR + _SEP +
    r"(" + _EAST + r"|" + _WEST + r")"
    r".{0,300}?" + _THENCE + r"\s+in\s+a\s+(north|south)(east|west)erly\s+direction\s*,?\s*"
    r"an\s+arc\s+(?:distance|length)\s+of\s+([\d,]+\.?\d*)\s*" + _FEET,
    re.IGNORECASE | re.DOTALL,
)

# The same bearingless-curve problem in its second real shape (covid 5838's own
# SAVE AND EXCEPT tracts, and the more common one in this document):
#
#   "... for the west corner ... and for the beginning of a circular curve to the
#    right whose radius point bears South 73°32'45" East 475.00 feet and having a
#    central angle of 06°16'20", a radius of 475.00 feet, a tangent distance of
#    26.03 feet and an arc length of 52.00 feet; Thence, with said circular curve
#    to the right, along the northwest boundary of this tract, an arc length of
#    52.00 feet for the Point of Beginning ..."
#
# Two differences from _NON_TANGENTIAL_CURVE_RE: the radius bearing is recited
# BEFORE the central angle ("whose radius point bears ... and having a central
# angle of"), and the arc call names no compass direction at all -- it is just
# "with said circular curve to the right". With no direction word there is
# nothing to pick between the radius's two perpendiculars, so the tangent is
# resolved against the PREVIOUS course's own azimuth instead (a boundary curve
# continues the traverse, it does not reverse it). Verified on covid 5838's
# TRACT 2: previous course N54°34'21"W (az 305.43°), candidate tangents 16.45°
# and 196.45°, the nearer one giving a chord that closes the tract to 0.03 ft.
_TANGENT_CURVE_RE = re.compile(
    r"curve\s+to\s+the\s+(right|left)[,\s]+whose\s+radius\s+point\s+bears\s+"
    r"(North|South)\s*" + _DMS_OCR + _SEP + r"(" + _EAST + r"|" + _WEST + r")"
    r".{0,90}?" + _DELTA_KEYWORD + r"\s*" + _DMS +
    r".{0,90}?\S*dius\s+of\s+([\d,]+\.?\d*)\s*" + _FEET +
    # The arc call restates the turn as prose ("fo the right", "to the rj ht" in
    # covid 5839) -- already captured above, so it is skipped rather than
    # re-parsed, and never gets a chance to fail on its own OCR damage.
    r".{0,260}?" + _THENCE + r"[,;:]?\s+with\s+(?:said\s+)?(?:the\s+)?" + _CIRCULAR + r"\s+curve"
    # Up to a sentence of prose can sit between the curve restatement and its
    # arc length -- covid 5838 writes "with said circular curve to the right,
    # continuing with the southeast boundary of this tract, an arc length of
    # 364.77 feet". [^;] keeps the match inside that one sentence.
    r"[^;]{0,200}?an\s+arc\s+(?:length|distance)\s+of\s+(?:length\s+of\s+)?([\d,]+\.?\d*)\s*" + _FEET,
    re.IGNORECASE | re.DOTALL,
)


# A curve stated at a POINT OF CURVATURE, with no radius bearing at all --
# covid 5839's third real curve shape:
#
#   "...a distance of 644.54 feet to a 5/8 inch iron rod ... set for the point of
#    curvature of a circular curve to the right which has a delta angle of
#    24 degrees 30'28", a radius of 300.00 feet, a tangent length of 65.16 feet
#    and an arc length of 128.32 feet; Thence, with said circular curve to the
#    right, an arc length of 128,32 feet to a 5/8 inch iron rod ..."
#
# The deed omits the radius bearing because the geometry supplies it: a curve at
# a point of curvature is tangent to the course that arrives there, so the
# tangent is simply the previous course's azimuth. Matched after the two
# radius-bearing forms above and deduplicated against them by arc position, so a
# curve that DOES state its radius bearing is never read twice.
_PC_TANGENT_CURVE_RE = re.compile(
    r"point\s+of\s+curvature\s+of\s+(?:with\s+)?(?:a|an|another)?\s*" + _CIRCULAR + r"\s+(?:curve\s+)?(?:to|fo)\s+the\s+(right|left)"
    r"[^;]{0,40}?" + _DELTA_KEYWORD + r"\s*(\d{1,3})\s*(?:degrees?|deg\.?|°)" + _SEP + r"(\d{1,2})\s*"
    r"(?:minutes?|min\.?|['\u2019])\s*(\d{1,2})\s*" + _SECONDS_MARK +
    r".{0,90}?\S*dius\s+of\s+([\d,]+\.?\d*)\s*" + _FEET +
    r".{0,260}?" + _THENCE + r"[,;:]?\s+with\s+(?:said\s+)?(?:the\s+)?" + _CIRCULAR + r"\s+curve"
    r"[^;]{0,40}?an\s+arc\s+(?:length|distance)\s+of\s+(?:length\s+of\s+)?([\d,]+\.?\d*)\s*" + _FEET,
    re.IGNORECASE | re.DOTALL,
)

_BEGINNING_RE = re.compile(r"BEGINNING\s+at\s+([^;]+);", re.IGNORECASE | re.DOTALL)



def _feet(raw: str) -> float:
    """Read a distance that may carry an OCR'd DECIMAL comma.

    Confirmed real and dangerous (covid 5839/Nueces): that deed recites
    "a tangent length of 244,30 feet" and "an arc length of 128,32 feet".
    Stripping commas as thousands separators turns 244.30 ft into 24,430 ft --
    a silent hundredfold error that corrupts a traverse rather than failing
    loudly, which is the worst way for this to go wrong.

    A comma is read as a DECIMAL POINT only when it is followed by exactly two
    digits at the end of the number and there is no real decimal point already.
    Genuine thousands separators are always followed by three digits
    ("1,516.17", "2,364"), so the two forms cannot be confused.
    """
    raw = raw.strip()
    if re.fullmatch(r"\d{1,3}(?:,\d{3})*,\d{2}", raw) or re.fullmatch(r"\d+,\d{2}", raw):
        head, _, dec = raw.rpartition(",")
        return float(head.replace(",", "") + "." + dec)
    return float(raw.replace(",", ""))


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


def _chord(turn: str, delta_deg: float, radius_ft: float, arc_ft: float,
           tangent: float) -> "Course":
    """Chord course from a resolved tangent: the tangent rotated half the
    central angle toward the side the curve turns, of length 2R sin(delta/2)."""
    az = (tangent + (delta_deg / 2.0 if turn.lower() == "right" else -delta_deg / 2.0)) % 360.0
    chord_ft = 2.0 * radius_ft * math.sin(math.radians(delta_deg) / 2.0)

    ns = "North" if (az < 90.0 or az > 270.0) else "South"
    ew = "East" if az < 180.0 else "West"
    acute = az if az < 90.0 else (360.0 - az if az > 270.0 else abs(180.0 - az))
    deg = int(acute)
    minutes = int((acute - deg) * 60)
    seconds = (((acute - deg) * 60) - minutes) * 60
    return Course(
        ns=ns, degrees=float(deg), minutes=float(minutes), seconds=seconds,
        ew=ew, distance_ft=chord_ft, is_curve=True, radius_ft=radius_ft,
        delta_deg=delta_deg, curve_direction=turn.lower(), arc_length_ft=arc_ft,
    )


def _chord_from_curve(
    turn: str, delta_deg: float, radius_ft: float, arc_ft: float,
    radius_ns: str | None = None, radius_brg_deg: float | None = None,
    radius_ew: str | None = None,
    general_ns: str | None = None, general_ew: str | None = None,
    prev_azimuth: float | None = None,
) -> Course:
    """Chord course for a curve whose own bearing the deed never states.

    The radius bearing fixes the tangent (perpendicular to it); the chord is
    that tangent rotated half the central angle toward the side the curve
    turns. The radius has TWO perpendiculars, and which one is the tangent is
    resolved -- never guessed -- either by the deed's own stated direction
    ("in a northwesterly direction") where it gives one, or otherwise by the
    previous course's azimuth, since a boundary curve continues the traverse.
    With neither available this raises, and the caller drops the course so the
    gap surfaces as a bad closure."""
    if radius_ns is None:
        # A POINT OF CURVATURE curve states no radius bearing because it does not
        # need one: by definition the curve is tangent to the course arriving at
        # that point, so the tangent IS the previous course's azimuth and there
        # is nothing to resolve. Confirmed real (covid 5839/Nueces): "...644.54
        # feet to a 5/8 inch iron rod set for the point of curvature of a
        # circular curve to the right which has a delta angle of 24 degrees
        # 30'28\", a radius of 300.00 feet". Without a previous course this is
        # genuinely unanchored, so it raises rather than assuming one.
        if prev_azimuth is None:
            raise ValueError(
                "point-of-curvature curve has no radius bearing and no previous course to "
                "take its tangent from -- not guessed at"
            )
        return _chord(turn, delta_deg, radius_ft, arc_ft, prev_azimuth)
    else:
        r_az = radius_brg_deg if radius_ns.lower() == "north" else 180.0 - radius_brg_deg
        if re.match(_WEST, radius_ew, re.IGNORECASE):
            r_az = -r_az
        r_az %= 360.0
        candidates = ((r_az + 90.0) % 360.0, (r_az - 90.0) % 360.0)
        tangent = None
    if general_ns and general_ew:
        # Strongest signal: the deed names the direction outright.
        want_north = general_ns.lower() == "north"
        want_east = general_ew.lower() == "east"
        for cand in candidates:
            if (cand < 90.0 or cand > 270.0) == want_north and (cand < 180.0) == want_east:
                tangent = cand
                break
        if tangent is None:                   # deed's own words disagree with its geometry
            raise ValueError(
                f"curve's stated {general_ns}{general_ew}erly direction matches neither perpendicular "
                f"of a radius bearing {radius_ns} {radius_brg_deg} {radius_ew} -- not guessed at"
            )
    elif prev_azimuth is not None:
        # No direction word: a boundary curve continues the traverse rather than
        # reversing it, so take the perpendicular nearer the previous course.
        def sep(a: float) -> float:
            d = abs((a - prev_azimuth) % 360.0)
            return min(d, 360.0 - d)
        tangent = min(candidates, key=sep)
        if abs(sep(candidates[0]) - sep(candidates[1])) < 1.0:
            raise ValueError(
                "curve has no stated direction and both tangents are equally close to the previous "
                "course -- ambiguous, not guessed at"
            )
    else:
        raise ValueError("curve has neither a stated direction nor a previous course to resolve against")

    return _chord(turn, delta_deg, radius_ft, arc_ft, tangent)


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
            ew=ew, distance_ft=_feet(dist),
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
            ew=ew, distance_ft=_feet(dist),
        )))
    # Curves are resolved AFTER the straight courses are in document order,
    # because the tangent-form ones need the previous course's own azimuth to
    # pick between the radius's two perpendiculars. Positioned at the arc
    # THENCE (the match's end), not the curve's parameter block, so they sort
    # into true traversal order -- the parameters are recited in the PREVIOUS
    # course's own sentence.
    matches.sort(key=lambda pair: pair[0])
    pending: list[tuple[int, dict]] = []
    for m in _NON_TANGENTIAL_CURVE_RE.finditer(text):
        (turn, d_deg, d_min, d_sec, radius, r_ns, r_deg, r_min, r_sec, r_ew,
         gen_ns, gen_ew, arc) = m.groups()
        pending.append((m.end(), dict(
            turn=turn, delta_deg=float(d_deg) + float(d_min) / 60.0 + float(d_sec) / 3600.0,
            radius_ft=_feet(radius), radius_ns=r_ns,
            radius_brg_deg=float(r_deg) + float(r_min) / 60.0 + float(r_sec) / 3600.0,
            radius_ew=r_ew, general_ns=gen_ns, general_ew=gen_ew,
            arc_ft=_feet(arc))))
    for m in _TANGENT_CURVE_RE.finditer(text):
        (turn, r_ns, r_deg, r_min, r_sec, r_ew, d_deg, d_min, d_sec, radius, arc) = m.groups()
        r_deg, r_min, r_sec = _ocr_int(r_deg), _ocr_int(r_min), _ocr_int(r_sec)
        if any(abs(pos - m.end()) < 5 for pos, _ in pending):
            continue                       # already captured by the direction-word form
        pending.append((m.end(), dict(
            turn=turn, delta_deg=float(d_deg) + float(d_min) / 60.0 + float(d_sec) / 3600.0,
            radius_ft=_feet(radius), radius_ns=r_ns,
            radius_brg_deg=r_deg + r_min / 60.0 + r_sec / 3600.0,
            radius_ew=r_ew, arc_ft=_feet(arc))))
    # LAST, so the two forms that state a radius bearing get first claim: this
    # pattern also matches their text (they sit at a point of curvature too), and
    # reading a NON-tangential curve as tangent would take its chord off the
    # previous course instead of off its own stated radius.
    for m in _PC_TANGENT_CURVE_RE.finditer(text):
        turn, d_deg, d_min, d_sec, radius, arc = m.groups()
        if any(abs(pos - m.end()) < 5 for pos, _ in pending):
            continue                       # already read with its radius bearing
        pending.append((m.end(), dict(
            turn=turn, delta_deg=float(d_deg) + float(d_min) / 60.0 + float(d_sec) / 3600.0,
            radius_ft=_feet(radius), arc_ft=_feet(arc))))

    for pos, kw in sorted(pending, key=lambda pair: pair[0]):
        # Must be the NEAREST preceding course, taken by position -- not the last
        # element of `matches`. Resolved curves are appended as we go and the list
        # is only re-sorted afterwards, so `matches[-1]` is the previously-resolved
        # CURVE, which is generally not the course this one actually follows.
        # Confirmed real on covid 5838's SAVE AND EXCEPT TRACT 5: the R=1110 curve
        # took its predecessor's azimuth from the R=475 curve two courses back,
        # picked the opposite perpendicular, and ran the chord 180 degrees
        # backwards -- along with the R=200 curve behind it, turning a 432 ft
        # closure error into 944 ft.
        prev = max((pair for pair in matches if pair[0] < pos),
                   key=lambda pair: pair[0], default=None)
        try:
            course = _chord_from_curve(prev_azimuth=prev[1].azimuth_degrees if prev else None, **kw)
        except ValueError:
            # Direction genuinely unresolvable -- leave the course out so the gap
            # shows up as a bad closure, rather than inventing a bearing.
            continue
        matches.append((pos, course))
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


# Digit pairs OCR actually confuses, by glyph shape. Used only to generate
# CANDIDATE re-readings of a bearing that already failed to close -- never to
# alter a value that parsed cleanly.
_CONFUSABLE_DIGITS = {
    "0": "689", "1": "7", "2": "7", "3": "58", "4": "9",
    "5": "368", "6": "058", "7": "12", "8": "03569", "9": "048",
}


def _digit_rereadings(value: float, ceiling: int) -> list[int]:
    """Plausible OCR re-readings of a printed number: one digit swapped for a
    confusable one, or two adjacent digits transposed. covid 5838 supplied a
    real transposition -- a tract headed "3.828 acres" whose own terminator and
    traverse both say 3.282."""
    raw = f"{int(value)}"
    out = set()
    for i, ch in enumerate(raw):
        for sub in _CONFUSABLE_DIGITS.get(ch, ""):
            out.add(int(raw[:i] + sub + raw[i + 1:]))
    for i in range(len(raw) - 1):
        out.add(int(raw[:i] + raw[i + 1] + raw[i] + raw[i + 2:]))
    return [v for v in out if 0 <= v < ceiling and v != int(value)]


def repair_bearing_by_closure(
    courses: list[Course],
    stated_acres: float | None = None,
    max_closure_ft: float = 1.0,
    min_closure_ratio_denominator: float = 20_000.0,
    max_area_deviation: float = 0.01,
) -> tuple[list[Course], dict | None]:
    """Recover a misread DEGREES or MINUTES value in one bearing, when the
    traverse's own closure and area together prove which reading was meant.

    repair_quadrant_by_closure covers the case where only the N/S/E/W letter is
    wrong. This covers the other half: covid 5839's 43.354 acre tract has every
    course the deed calls, every distance matching, and still misses its own
    Point of Beginning by 1,294 feet -- so the defect is in a bearing's digits,
    which a quadrant flip cannot reach.

    The search space is deliberately NOT arbitrary numbers. Candidates are only
    plausible OCR re-readings of the digits actually printed: one digit swapped
    for a confusable one, or two transposed. A value nothing misread cannot be
    "repaired" into existence.

    Two independent conditions must BOTH hold, and only one candidate may
    satisfy them:
      * the traverse closes to survey tolerance, and
      * the area lands on the deed's own stated acreage.
    Requiring both is what makes this safe rather than curve-fitting. Closure
    alone can be hit by luck across several hundred candidates; closure AND an
    independently stated acreage, from a single-digit change, effectively cannot.
    With no stated acreage the area test cannot run, so the bar rises: the repair
    is refused outright rather than accepted on closure alone.

    Returns (courses, repair); repair is None when nothing was changed.
    """
    base = walk_traverse(courses)
    if base["closure_error_ft"] <= max_closure_ft:
        return courses, None
    if stated_acres is None:
        return courses, None

    candidates = []
    for i, course in enumerate(courses):
        if course.is_curve:
            # A curve's bearing is DERIVED, not printed -- a wrong chord means
            # its radius bearing or its tangent was misread, which is a
            # different defect with a different fix.
            continue
        for field, ceiling in (("degrees", 90), ("minutes", 60)):
            for value in _digit_rereadings(getattr(course, field), ceiling):
                trial = list(courses)
                trial[i] = replace(course, **{field: float(value)})
                result = walk_traverse(trial)
                if result["closure_error_ft"] > max_closure_ft:
                    continue
                if (result["closure_ratio"] or 1) > 1.0 / min_closure_ratio_denominator:
                    continue
                if abs(result["area_acres"] - stated_acres) / stated_acres > max_area_deviation:
                    continue
                candidates.append((i, field, value, trial, result))

    if len(candidates) != 1:
        return courses, None

    i, field, value, repaired, result = candidates[0]
    before, after = courses[i], repaired[i]
    return repaired, {
        "course_index": i, "field": field,
        "from": getattr(before, field), "to": float(value),
        "bearing_before": f"{before.ns} {before.degrees:.0f}°{before.minutes:02.0f}'"
                          f"{before.seconds:02.0f}\" {before.ew}",
        "bearing_after": f"{after.ns} {after.degrees:.0f}°{after.minutes:02.0f}'"
                         f"{after.seconds:02.0f}\" {after.ew}",
        "distance_ft": before.distance_ft,
        "closure_before_ft": base["closure_error_ft"],
        "closure_after_ft": result["closure_error_ft"],
        "area_before_acres": base["area_acres"],
        "area_after_acres": result["area_acres"],
        "stated_acres": stated_acres,
    }


def repair_quadrant_by_closure(
    courses: list[Course],
    max_closure_ft: float = 0.5,
    min_closure_ratio_denominator: float = 20_000.0,
) -> tuple[list[Course], dict | None]:
    """Recover a single wrong quadrant letter (North<->South or East<->West) when,
    and only when, the traverse's own closure proves which one it is.

    Confirmed real on covid 5838's SAVE AND EXCEPT TRACT 7, a 0.554 acre strip of
    Beach Access Road No. 1. Every course parses cleanly and the area comes out at
    0.554 ac, but the traverse misses its own Point of Beginning by 811.64 feet,
    because the closing call is recited as

        "Thence, North 56°37'37" EAST ... a distance of 485.94 feet
         to the Point of Beginning"

    which runs back out along the road instead of returning down it. Reading that
    one word as West -- and changing nothing else -- closes the tract to 0.02 ft.
    The three preceding courses independently demand a return of 485.92 ft on
    azimuth 303.39 degrees; the deed's own figures are 485.94 ft and N56°37'37"W
    is azimuth 303.373. The distance and the angle were both transcribed
    correctly. Only the quadrant letter is wrong.

    This is a correction, not a guess, and the distinction is enforced rather than
    asserted: every single-letter flip is tried, and the repair is accepted ONLY
    if exactly one of them closes the traverse to survey tolerance. Two competing
    answers, or none, means the closure does not identify the error and the
    courses are returned untouched -- the bad closure then stands as the signal it
    is meant to be. Derived curve chords are left alone: a wrong chord means the
    radius bearing was misread, which is a different defect with a different fix.

    Returns (courses, repair) -- `repair` is None when nothing was changed, and
    otherwise carries the full before/after for the provenance record.
    """
    base = walk_traverse(courses)
    if base["closure_error_ft"] <= max_closure_ft:
        return courses, None

    flip = {"North": "South", "South": "North", "East": "West", "West": "East"}
    candidates = []
    for i, c in enumerate(courses):
        if c.is_curve:
            continue
        for field in ("ns", "ew"):
            trial = list(courses)
            trial[i] = replace(c, **{field: flip[getattr(c, field).title()]})
            r = walk_traverse(trial)
            # closure_ratio is error/perimeter, so a GOOD closure is a SMALL
            # number -- 1:20,000 is 0.00005, not 20000.
            if (r["closure_error_ft"] <= max_closure_ft
                    and (r["closure_ratio"] or 0) <= 1.0 / min_closure_ratio_denominator):
                candidates.append((i, field, trial, r))

    if len(candidates) != 1:
        return courses, None

    i, field, repaired, r = candidates[0]
    before, after = courses[i], repaired[i]
    return repaired, {
        "course_index": i,
        "field": field,
        "from": getattr(before, field),
        "to": getattr(after, field),
        "bearing_before": f"{before.ns} {before.degrees:.0f}°{before.minutes:02.0f}'"
                          f"{before.seconds:02.0f}\" {before.ew}",
        "bearing_after": f"{after.ns} {after.degrees:.0f}°{after.minutes:02.0f}'"
                         f"{after.seconds:02.0f}\" {after.ew}",
        "distance_ft": before.distance_ft,
        "closure_before_ft": base["closure_error_ft"],
        "closure_after_ft": r["closure_error_ft"],
        "area_before_acres": base["area_acres"],
        "area_after_acres": r["area_acres"],
    }
