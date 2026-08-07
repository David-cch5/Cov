"""Pull the named subdivisions out of a metes-and-bounds deed's own field notes
and label each one by the GRAMMATICAL ROLE it plays -- an adjoining subdivision
the deed merely ties a boundary to, versus one the tract itself is derived from
or was platted into.

Why this exists (confirmed real, covid 8534 tract 1, Denton County): the
spatial-first parcel census correctly matched every parcel whose geometry
intersects the tract polygon, but a neighbouring subdivision's own platted lot
lines clip that polygon at ordinary digitization tolerance, so 15 Forman
Williamsburg Square lots came back as "boundary" parcels. The deed's own
Exhibit A names that subdivision exactly once -- "in the South line of Forman
Williamsburg Square as recorded in Cabinet R, Page 318" -- i.e. purely as a
boundary tie, never as land being conveyed. That single sentence is the
evidence that settles it, and nothing in the pipeline was reading it.

THE ROLE, NOT THE MERE MENTION, IS THE SIGNAL. "Any subdivision named in a
metes-and-bounds deed is an adjoiner" would be badly wrong, and this project
has the counter-examples in its own corpus:
  - covid 4781: "...the said 10.780 acres having been platted as WATERMARK
    SECTION ONE, PHASE ONE UNIT DEVELOPMENT..." -- Watermark was platted OUT OF
    this very tract; its lots are the encumbered land, not neighbours.
  - covid 5838: "...a 2.454 acre tract of land being all of Lot Three (3),
    Block One (1), Gulf Side Estates Subdivision..." -- the tract IS a lot in
    that subdivision (which ALSO appears in boundary-tie phrases for the
    neighbouring lots, so a mention-only rule would have excluded the subject
    land itself).
Whenever a name appears in a derivation phrase ANYWHERE in the deed, that
derivation wins over any number of boundary-tie mentions and the subdivision is
reported 'derivation' -- a veto, deliberately biased toward never suggesting
the exclusion of land that might genuinely be encumbered (CLAUDE.md's accuracy-
over-completeness rule; a wrong exclusion silently drops real lots).

This is a corroborating signal for classifier.py's own geometric sliver check,
never an autonomous exclusion trigger -- the actual removal still goes through
exclude_non_tract_parcels' documented human judgment call.
"""
import re

# The reliable anchor in this prose is the plat/deed recording citation that
# FOLLOWS a subdivision's name -- surveyors vary the wording endlessly but
# essentially always cite where the plat is recorded. Everything between the
# role cue and this citation is the name.
_CITATION_LEAD_RE = re.compile(
    r"""(?:,\s*)?(?:
            a\s+map\s+of\s+which\s+is\s+recorded\s+in
          | according\s+to\s+the\s+map\s+or\s+plat\s+thereof\s+recorded\s+in
          | (?:being\s+)?an\s+addition\s+to[^,]{0,60},?\s*as\s+(?:recorded|described)\s+in
          | a\s+subdivision\s+in[^,]{0,60},?\s*according\s+to
          | as\s+(?:recorded|described)\s+in
        )\s+(?:Cabinet|Volume|Vol\.?|Slide|Doc)""",
    re.IGNORECASE | re.VERBOSE,
)

# "the Northwest corner of X", "in the South line of X", "on the common boundary
# of X" -- X is land the deed is pointing AT to fix a boundary, not conveying.
_BOUNDARY_TIE_CUE_RE = re.compile(
    r"(?:corner|line|boundary|right-of-way)\s+(?:thereof\s+)?of\s+", re.IGNORECASE,
)

# "being a part of X", "out of X", "being all of X", "having been platted as X",
# "SAVE AND EXCEPT X" -- X is the tract's own parent, the tract itself, or a
# carve-out from it. Any of these vetoes an exclusion suggestion.
_DERIVATION_CUE_RE = re.compile(
    r"""(?:
            being\s+(?:a\s+)?part\s+of
          | being\s+all\s+of
          | being\s+out\s+of
          | \bpart\s+of
          | \bout\s+of
          | having\s+been\s+platted\s+as
          | save\s+and\s+except
        )\s+""",
    re.IGNORECASE | re.VERBOSE,
)

# A leading lot/block designation is part of the CITATION, not the subdivision's
# own name ("Lot Three (3), Block One (1), Gulf Side Estates Subdivision" ->
# "Gulf Side Estates Subdivision"). Repeated until it stops matching, since both
# a Lot and a Block clause can precede the name.
_LOT_BLOCK_PREFIX_RE = re.compile(
    r"^(?:Lot|Lots|Lotl|Block|Blk)\s*[A-Za-z0-9()\s.&,-]{0,24}?,\s*", re.IGNORECASE,
)

# Filler that survivies the cue match but isn't part of the name.
_LEADING_FILLER_RE = re.compile(r"^(?:that|the|a|an|said|of)\s+", re.IGNORECASE)

# How far back to look for a role cue. Long enough to clear a "Lot N, Block M,"
# prefix plus a multi-word name, short enough not to reach the previous sentence.
_LOOKBACK_CHARS = 150

_NOISE_TOKENS = re.compile(r"^[\W\d_]+$")

# An acreage/deed tract reference ("260 3 ACRE TRACT OF LAND CONVEYED TO LONE
# STAR DEVELOPMENT COMPANY", real on covid 3194) also sits in front of a
# "described in Volume ..." citation and gets captured the same way, but it is
# NOT a platted subdivision and could never legitimately match a parcel's own
# recited subdivision name. Likewise a capture long enough to be a sentence
# fragment rather than a name. Both are dropped -- but ONLY from the
# 'adjoining' role, which is the one that drives an exclusion suggestion.
# A 'derivation' entry can only ever VETO an exclusion, so a noisy one is
# harmless and is kept (confirmed real: covid 4780's own SAVE AND EXCEPT clause
# captures as a 78-character lot list, and losing that veto is the unsafe
# direction).
_NOT_A_SUBDIVISION_NAME_RE = re.compile(r"\bACRES?\b|\bSURVEY\b|\bCONVEYED\b", re.IGNORECASE)
_MAX_PLAUSIBLE_NAME_CHARS = 60


def _normalize(name: str) -> str:
    name = " ".join(name.split()).strip(" ,;:.")
    prev = None
    while prev != name:
        prev = name
        name = _LOT_BLOCK_PREFIX_RE.sub("", name).strip(" ,;:.")
        name = _LEADING_FILLER_RE.sub("", name).strip(" ,;:.")
    return name.upper()


def extract_adjoining_subdivisions(deed_text: str | None) -> list[dict]:
    """Every subdivision the deed names alongside a plat/deed recording
    citation, each with the role it plays:

        [{"subdivision": "FORMAN WILLIAMSBURG SQUARE",
          "role": "adjoining",            # or "derivation"
          "context": "...in the South line of Forman Williamsburg Square as..."}]

    'adjoining' means every mention was a boundary tie -- real evidence the land
    is NOT part of this tract. 'derivation' means at least one mention derives
    the tract from it (or plats it out of the tract), which always wins.

    A name whose role cue can't be read is omitted entirely rather than guessed
    at, per CLAUDE.md -- an empty list means "this deed's prose didn't clearly
    state any subdivision's role", never "there are no adjoiners".
    """
    if not deed_text:
        return []
    text = " ".join(deed_text.split())

    found: dict[str, dict] = {}
    for m in _CITATION_LEAD_RE.finditer(text):
        window = text[max(0, m.start() - _LOOKBACK_CHARS):m.start()]

        # The cue NEAREST the citation is the one that governs this name -- a
        # boundary tie and a derivation phrase can both appear in one sentence
        # ("...being part of a called 60 acre tract ... for the Northwest
        # corner of X ..."), and only the last one introduces X itself.
        role = None
        cue_end = None
        for cue_re, cue_role in ((_BOUNDARY_TIE_CUE_RE, "adjoining"), (_DERIVATION_CUE_RE, "derivation")):
            for cm in cue_re.finditer(window):
                if cue_end is None or cm.end() > cue_end:
                    cue_end, role = cm.end(), cue_role
        if role is None:
            continue

        name = _normalize(window[cue_end:])
        if not name or len(name) < 3 or _NOISE_TOKENS.match(name):
            continue
        if role == "adjoining" and (
            _NOT_A_SUBDIVISION_NAME_RE.search(name) or len(name) > _MAX_PLAUSIBLE_NAME_CHARS
        ):
            continue  # see _NOT_A_SUBDIVISION_NAME_RE -- dropped only in the role that can trigger

        prior = found.get(name)
        if prior is None:
            found[name] = {"subdivision": name, "role": role,
                           "context": text[max(0, m.start() - 90):m.start() + 40].strip()}
        elif role == "derivation":
            # Derivation always wins over any number of boundary-tie mentions.
            prior["role"] = "derivation"
            prior["context"] = text[max(0, m.start() - 90):m.start() + 40].strip()

    return sorted(found.values(), key=lambda d: d["subdivision"])
