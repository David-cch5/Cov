"""Classify a county parcel's own recited legal description as either a real
platted reference (tied to a specific subdivision + section, needing a plat
lookup) or a still-raw abstract-survey tract (a county-assigned APN with no
lot/block/reserve designation at all -- visible evidence of unplatted land,
never mistaken for a plat).

Confirmed real, every shape seen on covid 4440's own 4090 matched parcels
(Montgomery County):
  - "The Canopies 03 BLK 1 LOT 43"                             -> platted (lot/block, no code prefix)
  - "S573300 - Harrington Trails 01, BLOCK 1, Lot 44"          -> platted (lot/block, code prefix + comma)
  - "S573393 - Harrington Trails 06B, BLOCK 5, Lot 22"         -> platted (alphanumeric section, "06B")
  - "S573309 - Harrington Trails 09, RES A, ACRES 0.9732"      -> platted (reserve)
  - "A0494 - Walker Co Sch L, TRACT 1C-1, ACRES 27.2696"       -> NOT platted
  - "A0494 WALKER CO SCH L, TR 1C1-A, 7.0494 ACRES"            -> NOT platted (no dash)

Deterministic regex, not an LLM call -- this is a fixed county-recorder
formatting convention, not free prose (same reasoning as
app/gis/adapters/montgomery_tx.py's own _ACRES_RE). Per CLAUDE.md, anything
that doesn't clearly match either shape is reported ambiguous rather than
guessed either way.
"""
import re
from dataclasses import dataclass

# "[<code> - ]<name...> <section>[, ]RES <letter>" and "... TRACT ..." descriptions
# all share this same optional leading vendor reference code (e.g. "S573309 - ",
# "S573300 - ") -- not part of the subdivision's own name, so always stripped
# first. A no-op (no match) when absent, e.g. "The Canopies 03 BLK 1 LOT 43".
_LEADING_CODE_RE = re.compile(r"^\S+\s*-\s*")

# "<name...> <section>[,] (BLK|BLOCK) <x> LOT <y>" -- comma before BLK/BLOCK is
# optional (both "The Canopies 03 BLK 1" and "Harrington Trails 01, BLOCK 1" are
# real), and the section itself can carry a trailing letter ("05A", "06B").
_LOT_BLOCK_RE = re.compile(r"^(.*?)\s+(\d+[A-Z]?)\s*,?\s*(?:BLK|BLOCK)\b", re.IGNORECASE)

# "<name...> <section>[,] RES <letter>" -- comma optional here too (confirmed real:
# "TIMBERS EDGE 01 RES D 1.453 ACRES" has none, "Harrington Trails 09, RES A" does).
_RESERVE_RE = re.compile(r"^(.*?)\s+(\d+[A-Z]?)\s*,?\s*RES\b", re.IGNORECASE)

# "<name, no section number>[,] (LT|LOT) <unit>" -- a subdivision with no numbered
# phase at all (confirmed real: "DUSTY TRAILS LT 9, ACRES 3.000", "Dusty Trails,
# Lot 2-C, ..."), so there's no section digit to capture -- stored as "" (this
# project's own convention for "one implicit, unnumbered phase").
_LOT_ONLY_RE = re.compile(r"^(.*?)\s*,?\s*(?:LT|LOT)\s+\S", re.IGNORECASE)

# "A#### - <survey name>, ..." (e.g. "TRACT 1C-1", "TR 1C1-A", or even the odd
# one-off "DIRECTOR LOT 1") -- a raw abstract-survey reference, never a real
# platted lot. Confirmed real and load-bearing: a plat's own recited
# description always leads with the SUBDIVISION NAME first ("The Canopies 03
# BLK 1 LOT 43"); only an unplatted abstract-tract reference leads with the
# bare survey abstract number -- that ordering alone is the reliable tell,
# not the specific word (TRACT/TR) that happens to follow, which varies. Any
# text starting this way is NEVER passed to the platted-lot fallback patterns
# below (confirmed necessary: "A0494 - Walker Co Sch L, DIRECTOR LOT 1" was
# being misread as a real subdivision named "WALKER CO SCH L, DIRECTOR"
# before this check ran first).
_ABSTRACT_TRACT_RE = re.compile(r"^A\d+\b")


@dataclass
class PlatReference:
    platted: bool
    subdivision_name: str | None = None  # normalized upper-case, e.g. "THE CANOPIES"
    section: str | None = None           # e.g. "03" -- kept as the string as recited (leading
                                          # zeros matter for matching plat.section, never re-cast to int)


def normalize_section(section: str | None) -> str:
    """Confirmed real (covid 4440): a parcel's own recited legal description
    pads its section with a leading zero ("The Canopies 03", "Harrington
    Trails 06B"), but the recorder portal's own Plats-department SECTION
    column never does ("3", "6B") -- comparing the two directly missed
    almost every real match (2360 of 2419 parsed-platted parcels came back
    "unresolved" before this existed). Strips a leading zero only when
    followed by another digit, so "18" (a real section number, no leading
    zero to begin with) is untouched."""
    return re.sub(r"^0+(?=\d)", "", (section or "").strip().upper())


def parse_plat_reference(recited_legal_description: str | None) -> PlatReference | None:
    """Returns None (ambiguous -- needs manual review, never guessed) if the
    text doesn't clearly match a recognized platted or raw-tract shape."""
    if not recited_legal_description:
        return None
    text = recited_legal_description.strip()

    if _ABSTRACT_TRACT_RE.match(text):
        return PlatReference(platted=False)

    stripped = _LEADING_CODE_RE.sub("", text)

    m = _LOT_BLOCK_RE.match(stripped)
    if m:
        return PlatReference(platted=True, subdivision_name=m.group(1).strip().upper(), section=m.group(2))

    m = _RESERVE_RE.match(stripped)
    if m:
        return PlatReference(platted=True, subdivision_name=m.group(1).strip().upper(), section=m.group(2))

    m = _LOT_ONLY_RE.match(stripped)
    if m and m.group(1).strip():
        return PlatReference(platted=True, subdivision_name=m.group(1).strip().upper(), section="")

    return None
