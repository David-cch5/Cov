"""Recover a curve recital's missing chord from its SIBLINGS in the same deed.

Twice now the answer to an unreadable call has been sitting in the same
paragraph, and twice it took an LLM to notice:

  covid 5838's curve recites no bearing at all -- "THENCE in a northwesterly
  direction, an arc distance of 31.95 feet" -- and its direction is recoverable
  from the radius bearing given in the PREVIOUS sentence.

  covid 4981's 55.73 ac tract loses a whole "THENCE along said curve ... (Chord
  Bearing ...)" line in retyping, keeping only the curve's setup data. The
  direction is recoverable from the three intact street crossings along the same
  west line, each reciting a chord east and near-perpendicular to travel.

A platted figure repeated along a street produces the SAME RELATIVE GEOMETRY
every time, so what transfers between siblings is not the absolute bearing but
the angle from the preceding course to the chord. That is why this works on
covid 4981 where a tangency assumption fails: the crossings sit at slightly
different bearings along the west line, and the relative turn is what they
share.

The discipline is the same as every other repair here. At least two siblings
must agree, so one anomalous recital cannot drive a reconstruction; their spread
is reported so a caller can see how well they agree; and nothing is applied
silently -- a reconstruction is returned with its evidence for the closure test
to accept or reject.
"""
import math
import re
import statistics

_TURN_RE = re.compile(r"curve to the (right|left)", re.IGNORECASE)
_RADIUS_RE = re.compile(r"radius (?:of|point)?\s*(?:of\s*)?([\d,]+\.?\d*)\s*f", re.IGNORECASE)
_DELTA_RE = re.compile(r"central angle of\s*(\d{1,3})\s*[°º\"]\s*(\d{1,2})'\s*(\d{1,2})",
                       re.IGNORECASE)
_CHORD_RE = re.compile(
    r"Chord Bearing\s*(North|South)\s*(\d{1,3})\s*[°º\"]\s*(\d{1,2})'\s*(\d{1,2})\"?\s*"
    r"(East|West)\s*,?\s*([\d,]+\.?\d*)", re.IGNORECASE)
# Any plain bearing, used to find the course a recital follows.
_BEARING_RE = re.compile(
    r"\b(North|South|N|S)\.?\s*(\d{1,3})\s*(?:degrees?|deg\.?|[°º\"])\s*"
    r"(\d{1,2})\s*(?:minutes?|min\.?|['’])\s*(\d{1,2})\s*(?:seconds?|sec\.?|[\"”'’]+)?\s*"
    r"(East|West|E|W)\.?", re.IGNORECASE)

_RECITAL_WINDOW = 320          # chars of a recital's own setup data
_MIN_SIBLINGS = 2
_MAX_SIBLING_SPREAD_DEG = 5.0
# A sibling must be the SAME FIGURE, not merely another curve. Central angle is
# what identifies it: covid 4981's street crossings all turn well under two
# degrees, while the same description carries a 40-degree curve on a different
# alignment entirely. Without this the crossing motif's -89.67 deg offset gets
# borrowed by that 40-degree curve, producing a confident reconstruction that
# takes the traverse from 56 ft out to 112 ft out.
_MAX_DELTA_RATIO = 4.0


def _azimuth(ns: str, deg: float, minute: float, sec: float, ew: str) -> float:
    angle = deg + minute / 60.0 + sec / 3600.0
    south = (ns or "").strip(".").upper().startswith("S")
    west = (ew or "").strip(".").upper().startswith("W")
    if south and west:
        return 180.0 + angle
    if south:
        return 180.0 - angle
    if west:
        return 360.0 - angle
    return angle


def _signed_offset(chord_az: float, previous_az: float) -> float:
    return (chord_az - previous_az + 180.0) % 360.0 - 180.0


def _comparable_delta(sibling: float, target: float) -> bool:
    """Same figure, judged by central angle."""
    if sibling <= 0 or target <= 0:
        return False
    ratio = sibling / target
    return 1.0 / _MAX_DELTA_RATIO <= ratio <= _MAX_DELTA_RATIO


def _last_bearing_azimuth_before(text: str, position: int) -> float | None:
    last = None
    for m in _BEARING_RE.finditer(text, 0, position):
        last = m
    if last is None:
        return None
    ns, deg, minute, sec, ew = last.groups()
    return _azimuth(ns, float(deg), float(minute), float(sec), ew)


def curve_recitals(text: str) -> list[dict]:
    """Every "curve to the right/left" recital, with whatever it states."""
    out = []
    for turn in _TURN_RE.finditer(text):
        window = text[turn.start():turn.start() + _RECITAL_WINDOW]
        radius = _RADIUS_RE.search(window)
        delta = _DELTA_RE.search(window)
        chord = _CHORD_RE.search(window)
        previous = _last_bearing_azimuth_before(text, turn.start())
        entry = {
            "position": turn.start(),
            "turn": turn.group(1).lower(),
            "radius_ft": float(radius.group(1).replace(",", "")) if radius else None,
            "delta_deg": (float(delta.group(1)) + float(delta.group(2)) / 60
                          + float(delta.group(3)) / 3600) if delta else None,
            "previous_course_azimuth": previous,
            "chord_azimuth": None, "chord_ft": None,
        }
        if chord:
            entry["chord_azimuth"] = _azimuth(chord.group(1), float(chord.group(2)),
                                              float(chord.group(3)), float(chord.group(4)),
                                              chord.group(5))
            entry["chord_ft"] = float(chord.group(6).replace(",", ""))
        out.append(entry)
    return out


def reconstruct_missing_chords(text: str) -> list[dict]:
    """Reconstruct the chord of every curve recital that states R and delta but
    no chord bearing, using its siblings' relative geometry.

    Returns one entry per reconstructable recital, each carrying the siblings it
    was derived from and their spread, so a caller can weigh it. Never mutates
    anything: the closure test is what decides whether a reconstruction is right.
    """
    recitals = curve_recitals(text)
    out = []
    for target in recitals:
        if target["chord_azimuth"] is not None:
            continue
        if not target["radius_ft"] or not target["delta_deg"]:
            continue
        if target["previous_course_azimuth"] is None:
            continue
        offsets = [
            _signed_offset(s["chord_azimuth"], s["previous_course_azimuth"])
            for s in recitals
            if s["chord_azimuth"] is not None and s["previous_course_azimuth"] is not None
            and s["turn"] == target["turn"]
            and s["delta_deg"] and _comparable_delta(s["delta_deg"], target["delta_deg"])
        ]
        if len(offsets) < _MIN_SIBLINGS:
            continue
        spread = max(offsets) - min(offsets)
        if spread > _MAX_SIBLING_SPREAD_DEG:
            # The siblings disagree about the figure, so there is no single
            # repeated geometry to borrow. Reported as a skip rather than
            # resolved by averaging away the disagreement.
            out.append({"position": target["position"], "reconstructed": False,
                        "reason": f"{len(offsets)} siblings disagree by {spread:.1f} deg",
                        "sibling_offsets_deg": offsets})
            continue
        offset = statistics.median(offsets)
        radius, delta = target["radius_ft"], target["delta_deg"]
        out.append({
            "position": target["position"], "reconstructed": True,
            "turn": target["turn"], "radius_ft": radius, "delta_deg": delta,
            "chord_azimuth": (target["previous_course_azimuth"] + offset) % 360.0,
            "chord_ft": 2 * radius * math.sin(math.radians(delta) / 2),
            "arc_ft": radius * math.radians(delta),
            "tangent_ft": radius * math.tan(math.radians(delta) / 2),
            "from_siblings": len(offsets),
            "sibling_spread_deg": spread,
            "offset_from_previous_course_deg": offset,
            "previous_course_azimuth": target["previous_course_azimuth"],
        })
    return out


def as_chord_recital(reconstruction: dict) -> str:
    """The reconstruction written as the deed would have written it, so a
    corrected text goes through the ordinary parser rather than a special path."""
    az = reconstruction["chord_azimuth"] % 360.0
    north = az <= 90.0 or az >= 270.0
    angle = az if az <= 90 else (180 - az if az < 180 else (az - 180 if az < 270 else 360 - az))
    east = az <= 180.0
    degrees = int(angle)
    minutes = int((angle - degrees) * 60)
    seconds = round(((angle - degrees) * 60 - minutes) * 60)
    if seconds == 60:
        seconds, minutes = 0, minutes + 1
    if minutes == 60:
        minutes, degrees = 0, degrees + 1
    return (f"THENCE along said curve to the {reconstruction['turn']}, for an arc distance "
            f"of {reconstruction['arc_ft']:.2f} feet, (Chord Bearing "
            f"{'North' if north else 'South'} {degrees:02d}°{minutes:02d}'{seconds:02d}\" "
            f"{'East' if east else 'West'}, {reconstruction['chord_ft']:.2f} feet);")
