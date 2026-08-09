"""Extract NGS control-monument ties from a deed's field notes.

A tie is a bearing and distance FROM a named corner of the tract TO a published
survey monument:

    "... for the north corner of this tract and from which north corner of this
     tract, a National Geodetic Survey monument stamped \"SF-010\" bears North
     14°01'24\" East 4708.73 feet and another National Geodetic Survey monument
     stamped \"Knoll\" bears South 20°44'36\" West 1033.98 feet;"

Two ties to two different monuments are not redundancy for its own sake -- they
are the surveyor's own check, and they let this project verify a placement
without trusting any single reading: reconstruct the monument-to-monument vector
from the deed's two ties and compare it against the monuments' own published
coordinates (see anchor_by_ngs_monument_tie).

The direction is FROM the corner TO the monument, so placing the corner means
REVERSING the tie -- a sign error here puts the tract twice the tie distance
away, which on covid 5838 would be over 9,000 feet.

Reuses metes_bounds' OCR-tolerant quadrant and seconds-mark tokens rather than
assuming clean text -- these ties suffer the same "Kast"/"Wast" substitutions as
the courses -- and adds its own digit class for the one substitution the shared
class cannot safely carry (see _TIE_D).
"""
import re
from dataclasses import dataclass

from app.parsing.legal_description.metes_bounds import _EAST, _SECONDS_MARK, _SEP, _WEST

# metes_bounds' own _D deliberately excludes S, because there it would sit next
# to the North/South quadrant word and could swallow it. Here it cannot: this
# pattern captures North|South explicitly BEFORE these digits and requires the
# East|West word and a distance after them, so the degrees/minutes/seconds are
# fully fenced. That matters -- covid 5838 renders one SF-010 tie's seconds as
# "18°06'S5\"", and without tolerating it that tract falls back to a single
# uncheckable tie. Kept local rather than widening the shared class, so the
# course parser's blast radius is untouched.
_TIE_D = r"[\dOolIB$Ss]"
_TIE_OCR_DIGITS = str.maketrans(
    {"$": "5", "O": "0", "o": "0", "l": "1", "I": "1", "B": "8", "S": "5", "s": "5"})


def _tie_int(raw: str) -> float:
    return float(raw.translate(_TIE_OCR_DIGITS))


# The corner the tie runs from, when the deed names it ("from which north corner
# of this tract, ..."). Optional: some tracts tie without naming a corner, in
# which case the tie belongs to the Point of Beginning by position.
_CORNER_RE = re.compile(
    r"from\s+which\s+(?:(?:the)\s+)?([a-z]+(?:\s+[a-z]+)?\s+corner)\b", re.IGNORECASE)

_TIE_RE = re.compile(
    r"(?:National\s+Geodetic\s+Survey|N\.?G\.?S\.?)\s+monument\s+stamped\s*"
    r"[\"“”'’]{0,2}\s*([A-Za-z0-9][A-Za-z0-9 _.-]{0,20}?)\s*[\"“”'’]{0,2}\s+bears\s+"
    r"(North|South)\s*(" + _TIE_D + r"{1,3})\s*(?:degrees?|deg\.?|°)" + _SEP + r"(" + _TIE_D + r"{1,2})\s*"
    r"(?:minutes?|min\.?|['’])" + _SEP + r"(" + _TIE_D + r"{1,2})\s*" + _SECONDS_MARK + _SEP +
    r"(" + _EAST + r"|" + _WEST + r")\s*([\d,]+\.?\d*)\s*(?:feet|ft\.?)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MonumentTie:
    designation: str        # the deed's own stamping, e.g. "SF-010"
    ns: str                 # 'North' or 'South'
    ew: str                 # 'East' or 'West', normalised past OCR
    degrees: float
    minutes: float
    seconds: float
    distance_ft: float
    corner: str | None      # the tract corner it runs from, when the deed names it
    position: int           # character offset, for pairing a tie with its tract

    @property
    def azimuth_degrees(self) -> float:
        """Azimuth FROM the tract corner TO the monument, clockwise from North."""
        a = self.degrees + self.minutes / 60 + self.seconds / 3600
        ns, ew = self.ns.upper(), self.ew.upper()
        if ns == "NORTH" and ew == "EAST":
            return a
        if ns == "SOUTH" and ew == "EAST":
            return 180 - a
        if ns == "SOUTH" and ew == "WEST":
            return 180 + a
        return 360 - a

    def flipped(self, field: str) -> "MonumentTie":
        """The same tie with one quadrant letter reversed. covid 5838 recites a
        KNOLL tie as South ... WEST where its own geometry requires East -- the
        identical defect repair_quadrant_by_closure recovers in the courses --
        so a cross-check has to be able to test that hypothesis explicitly
        rather than simply failing."""
        flip = {"North": "South", "South": "North", "East": "West", "West": "East"}
        return MonumentTie(**{**self.__dict__, field: flip[getattr(self, field).title()]})


def _quadrant(raw: str) -> str:
    return "East" if raw.upper().rstrip(".").endswith(("AST", "E")) else "West"


def extract_ngs_monument_ties(text: str) -> list[MonumentTie]:
    """Every NGS monument tie in `text`, in document order."""
    ties = []
    for m in _TIE_RE.finditer(text):
        designation, ns, deg, minute, sec, ew, dist = m.groups()
        corner = None
        # Look back a short way for the corner this tie runs from; the phrase
        # always precedes the monument clause in the deeds seen so far.
        if (cm := list(_CORNER_RE.finditer(text[max(0, m.start() - 200):m.start()]))):
            corner = cm[-1].group(1).lower()
        ties.append(MonumentTie(
            designation=designation.strip().strip('"“”\'’'),
            ns=ns.title(), ew=_quadrant(ew),
            degrees=_tie_int(deg), minutes=_tie_int(minute), seconds=_tie_int(sec),
            distance_ft=float(dist.replace(",", "")),
            corner=corner, position=m.start(),
        ))
    return ties
