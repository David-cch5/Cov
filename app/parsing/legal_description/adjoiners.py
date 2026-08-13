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


# --- which courses run WITH an adjoiner's line -----------------------------
#
# `app/gis/state_plane_anchor.py`'s adjoining-plat anchor needs to know which
# traverse vertices the deed puts ON the adjoining plat. That is stated, not
# inferred -- "THENCE ... with a North line of said The Heights at West ridge
# Phase I", then "THENCE, continuing with said North line and said curve" --
# and four attempts to infer it from geometry instead each produced a confident
# answer over 1,000 ft wrong (see that module's own comment).

_CONTACT_NAMED_RE = re.compile(
    # "with a North line of said X" is the common form, but a deed may establish
    # the line it is running along with IN or ALONG instead and only then switch
    # to "continuing with said west line" -- covid 4981's 55.73 ac tract does
    # exactly that ("being an exterior ell corner IN the west line of said
    # Heights At Westridge Phase III"), and requiring "with" made Tier 0d decline
    # a tract whose POB names a published plat corner outright.
    r"\b(?:with|in|along)\s+(?:a|an|the|said)\s+[\w\- ]{0,24}?lines?\s+of\s+(?:said\s+)?",
    re.IGNORECASE)
# TRIED AND REJECTED: accepting "along said west line" (no "with") as a
# continuation, to extend a run past the point where the deed drops the word.
# The reasoning was sound and the measurement refuted it. On covid 4981's 55.73
# ac tract it grew the Phase III run from 3 courses / 414 ft to 31 courses /
# 2,694 ft -- 39% of the perimeter instead of 6%, which should have been a much
# stronger tie -- and the fit went from 10.78 ft to 49.38 ft, failing outright.
# "Said west line" does not keep meaning the same plat's line for 28 courses; the
# phrase survives while its referent changes, so a longer run is not a better
# constrained one. Requiring "with" keeps the chain to where the deed is
# explicit.
_CONTACT_CONTINUING_RE = re.compile(
    r"\b(?:continuing|along)\s+with\s+said\b|\bwith\s+said\s+[\w\- ]{0,24}?lines?\b",
    re.IGNORECASE)
# The name that follows "line of said ...", so a run can be attributed to the
# plat it belongs to rather than only to the one the POB names. Handles both a
# proper name ("Heights At Westridge Phase III;") and a parcel-list plat ("Parcel
# 1201-1209, 1216 & 1217 Plat"), stopping at whatever ends the citation.
_LINE_OWNER_RE = re.compile(
    r"(?P<name>[A-Z0-9][\w'’&.\-]*(?:[ ,]+[A-Za-z0-9][\w'’&.\-]*){0,8}?)"
    r"(?=\s+Plat\b|,\s*(?:an?\s+addition|according|as\s+recorded|for\s|and\s)|[;.]|$)")

_POB_ADJOINER_RE = re.compile(
    r"corner\s+of\s+(?:the\s+)?(?P<name>[A-Z][\w'’]*(?:\s+[A-Za-z][\w'’]*){0,7}?)\s*,\s*"
    r"(?:an?\s+addition|according|as\s+recorded)", re.IGNORECASE)
_THENCE_SPLIT_RE = re.compile(r"(?=\bTHENCE\b)", re.IGNORECASE)
_NAME_NOISE_TOKENS = {"THE", "AT", "OF", "AND", "A", "AN"}


def adjoiner_name_key(name: str) -> str:
    """A comparison key for a subdivision name: significant tokens only, joined.

    Two spellings of one subdivision have to compare equal across three
    independent corruptions at once. The deed says "The Heights at West ridge
    Phase I" (OCR split WESTRIDGE in two); the CAD says "HEIGHTS AT WESTRIDGE
    PHASE I THE" (the article moved to the end, the house style for a name
    beginning with "The"). Dropping the connectors and joining what is left
    reduces both to HEIGHTSWESTRIDGEPHASEI, which is the same subdivision --
    and does so without a fuzzy score that could quietly equate two different
    phases of one development.
    """
    tokens = re.findall(r"[A-Za-z0-9]+", (name or "").upper())
    return "".join(t for t in tokens if t not in _NAME_NOISE_TOKENS)


def point_of_beginning_adjoiner(deed_text: str) -> str | None:
    """The plat whose corner the deed's POB sits on, if it names one."""
    if not deed_text:
        return None
    head = _THENCE_SPLIT_RE.split(deed_text, maxsplit=1)[0]
    matches = _POB_ADJOINER_RE.findall(head)
    return matches[-1].strip() if matches else None


def _line_owner_after(segment: str, match_end: int) -> str | None:
    owner = _LINE_OWNER_RE.match(segment[match_end:match_end + 90].lstrip())
    return " ".join(owner.group("name").split()) if owner else None


def adjoiner_contact_runs(deed_text: str, total_courses: int) -> list[dict]:
    """Every adjoining plat this deed runs WITH, and which courses run with it.

    The generalisation of reading only the POB's adjoiner. A deed commonly runs
    with more than one plat, and the useful one is whichever it runs with FURTHEST
    -- an adjoining-plat tie is only as good as its frontage, since a short
    contact run leaves the far end of the tract unconstrained and lets a fraction
    of a degree swing it many feet. Covid 4981's 55.73 ac tract runs 414 ft with
    Heights at Westridge Phase III and some 3,000 ft with the Parcels 1201-1209
    plat it was carved out of; only the second is worth fitting against.

    Runs are returned longest-frontage first. Refuses the same way as before: a
    course count that cannot be reconciled with the caller's returns nothing,
    because indices that do not line up anchor a tract to the wrong corner while
    reporting a residual that looks fine.
    """
    from app.parsing.legal_description.metes_bounds import (
        extract_courses, repair_ocr_decimals, repair_ocr_survey_words)

    if not deed_text:
        return []
    repaired = repair_ocr_survey_words(repair_ocr_decimals(deed_text))
    segments = _THENCE_SPLIT_RE.split(repaired)
    preamble, segments = segments[0], segments[1:]
    if not segments:
        return []

    # The POB may itself name the line the first course will run with.
    current = None
    for m in _CONTACT_NAMED_RE.finditer(preamble):
        named = _line_owner_after(preamble, m.end())
        if named:
            current = named
    # A RUN IS CONTIGUOUS, and that is load-bearing rather than tidy. A segment
    # that neither names a plat nor continues one ENDS the run: the phrase "said
    # west line" outlives its referent, so letting it rejoin a run after a break
    # collects courses that no longer touch that plat. Measured on covid 4981's
    # 55.73 ac tract: contiguous gives 3 courses fitting to 10.78 ft, while
    # rejoining after breaks gives 28 courses fitting to 49.38 ft and failing.
    blocks: list[tuple[str, list[int]]] = []
    open_block: list[int] | None = None
    index, counted = 0, 0
    for segment in segments:
        here = len(extract_courses(segment))
        counted += here
        if here == 0:
            continue
        owner = None
        for m in _CONTACT_NAMED_RE.finditer(segment):
            named = _line_owner_after(segment, m.end())
            if named:
                owner = named
        if owner is None and current and _CONTACT_CONTINUING_RE.search(segment):
            owner = current            # "continuing with said west line" inherits
        if owner:
            if open_block is not None and current and adjoiner_name_key(owner) == \
                    adjoiner_name_key(current):
                open_block.extend(range(index, index + here))
            else:
                open_block = list(range(index, index + here))
                blocks.append((adjoiner_name_key(owner), open_block))
            current = owner
        else:
            open_block, current = None, None
        index += here

    if counted != total_courses:
        return []
    names = {}
    for segment in [preamble] + segments:
        for m in _CONTACT_NAMED_RE.finditer(segment):
            named = _line_owner_after(segment, m.end())
            if named:
                names.setdefault(adjoiner_name_key(named), named)
    out = []
    for key, course_list in blocks:
        if len(course_list) < 2:
            continue
        out.append({"adjoiner": names.get(key, key), "adjoiner_key": key,
                    "contact_courses": sorted(set(course_list)),
                    "contact_indices": sorted({v for c in course_list for v in (c, c + 1)})})
    out.sort(key=lambda r: -len(r["contact_courses"]))
    return out


def courses_running_with_adjoiner(deed_text: str, total_courses: int) -> dict | None:
    """The run belonging to the plat the POB names, or the longest run if the POB
    names none. Kept as the single-run entry point; adjoiner_contact_runs is the
    general form."""
    runs = adjoiner_contact_runs(deed_text, total_courses)
    if not runs:
        return None
    pob = point_of_beginning_adjoiner(deed_text)
    if pob:
        key = adjoiner_name_key(pob)
        for run in runs:
            if run["adjoiner_key"] == key:
                return run
    return runs[0]
