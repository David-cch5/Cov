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

# "<name, no trailing section number>, (BLK|BLOCK) <x>, (LT|LOT) <y>" -- Collin
# County's own shape (e.g. "STAR TRAIL PHASE ONE B, BLK A, LOT 1"), where the
# phase/section is already baked into the subdivision's own official plat name
# rather than recited as a separate trailing digit the way Montgomery's is --
# _LOT_BLOCK_RE never matches these (it requires a digit group right before
# BLK/BLOCK) and they'd otherwise fall through to _LOT_ONLY_RE, which greedily
# swallows the ", BLK A" portion into the subdivision name. Comma before BLK is
# required here (unlike _LOT_BLOCK_RE) since without a section digit to anchor
# on, an optional comma would make the name capture ambiguous.
_BLOCK_LOT_NO_SECTION_RE = re.compile(r"^(.*?),\s*(?:BLK|BLOCK)\s+\S+\s*,\s*(?:LT|LOT)\b", re.IGNORECASE)

# A trailing "PHASE <spelled-out-or-numeric>[<letter>]" on a name captured by
# _BLOCK_LOT_NO_SECTION_RE (e.g. "STAR TRAIL PHASE ONE B" -> base "STAR TRAIL",
# phase "ONE B") -- confirmed real and load-bearing (covid 3028, Collin
# County): the recorder's own Plats-department index files each phase as its
# own distinct subdivision ("STAR TRAIL #1B PROSPER", "STAR TRAIL PHASE 8",
# etc, inconsistently even with each other) rather than "one base subdivision,
# many numbered sections" the way Montgomery's own Harrington Trails/The
# Canopies do -- but splitting the CAD's own "PHASE <x>" suffix off the base
# name here lets resolve_plats_for_tract search the shared base name ONCE
# (its own stated design) instead of once per phase, most of which never
# match the recorder's differently-formatted phase index at all.
_PHASE_SUFFIX_RE = re.compile(r"^(.*?)\s+PHASE\s+([A-Z0-9]+(?:\s+[A-Z])?)\s*$", re.IGNORECASE)

_ORDINAL_WORDS = {
    "ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5",
    "SIX": "6", "SEVEN": "7", "EIGHT": "8", "NINE": "9", "TEN": "10",
}


def normalize_phase(phase: str) -> str:
    """'ONE B' -> '1B', 'TWO' -> '2', '8' -> '8' -- spelled-out ordinal words
    map to digits (matching however the recorder's own index happens to spell
    the same phase), a trailing bare letter stays attached with no space, and
    an already-numeric phase passes through unchanged."""
    tokens = phase.strip().upper().split()
    if not tokens:
        return ""
    number = _ORDINAL_WORDS.get(tokens[0], tokens[0])
    suffix = "".join(tokens[1:])
    return number + suffix

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


_HASH_PHASE_RE = re.compile(r"#\s*(\d+[A-Z]?)\b")
_WORD_PHASE_RE = re.compile(r"\bPHASE\s+([A-Z]+(?:\s+[A-Z])?|\d+[A-Z]?)\b", re.IGNORECASE)


def extract_phase_key_from_text(*texts: str | None) -> str | None:
    """For a recorder-portal plats-department result row that has no
    dedicated SECTION column at all (confirmed real: Collin County's own
    Plats department -- unlike Montgomery's, whose rows always carry a
    populated SECTION), pull a normalized phase identifier out of whichever
    free-text field actually states it. Checked across every field a caller
    passes, in order, since which field carries it is inconsistent even
    within the same county's own index (confirmed real: Collin's "Star
    Trail" plats variously carry it in GRANTOR as "STAR TRAIL #1B PROSPER",
    in GRANTEE as "STAR TRAIL PHASE 8", or nowhere but the LEGAL DESCRIPTION).
    Returns None (never guessed) if no field states one."""
    for text in texts:
        if not text:
            continue
        m = _HASH_PHASE_RE.search(text)
        if m:
            return normalize_phase(m.group(1))
        m = _WORD_PHASE_RE.search(text)
        if m:
            return normalize_phase(m.group(1))
    return None


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

    m = _BLOCK_LOT_NO_SECTION_RE.match(stripped)
    if m and m.group(1).strip():
        name = m.group(1).strip().upper()
        phase_m = _PHASE_SUFFIX_RE.match(name)
        if phase_m:
            return PlatReference(
                platted=True, subdivision_name=phase_m.group(1).strip(),
                section=normalize_phase(phase_m.group(2)),
            )
        return PlatReference(platted=True, subdivision_name=name, section="")

    m = _LOT_ONLY_RE.match(stripped)
    if m and m.group(1).strip():
        return PlatReference(platted=True, subdivision_name=m.group(1).strip().upper(), section="")

    return None
