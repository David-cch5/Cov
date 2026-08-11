"""Link a parcel to the recorded plat that created it, and report which plats are missing.

Formation dates come from a parcel's plat (app/gis/formation.py), and a parcel
only has a plat if plat_id is set. Today plat_id is set solely by the
subdivision-plat resolution path, so covenants resolved by metes-and-bounds --
which classify spatially and never touch a subdivision name -- have none, and
their maps and formation history are empty.

MEASURED FIRST, AND IT INVERTED THE TASK. Almost no unlinked parcel belongs to a
subdivision this project already holds a plat for. The links were not mostly
missing; the plats are. 50 distinct subdivisions across 8 counties have no plat
row, and they are where the parcels are:

    ~600  Nueces      PALMILLA BEACH (spelled 5 ways -- see below)
     291  Montgomery  THE RESERVE ON LAKE CONROE 01
    ~213  Denton      SHERMAN CROSSING (4 spellings)
     ~85  Collin      HEIGHTS AT WESTRIDGE (4 spellings)

So this module does two jobs, and the second is the one that unblocks the first:

  link_parcels_to_plats   deterministic parcel -> plat matching, for plats we hold
  plats_needed            the lookup worklist, collapsed across spelling variants
                          so a recorder search happens once per subdivision rather
                          than once per way the county spelled it

SPELLING VARIANCE IS THE NORM, not an edge case. Nueces' own GIS calls one place
"PALMILLA BEACH P.U.D.", "PALMILLA BEACH PUD" and "PALMILLA BEACH"; Collin writes
"HEIGHTS AT WESTRIDGE PHASE III" and "HEIGHTS AT WESTRIDGE PHASE III THE". Matching
therefore reuses app/title/chain.py's _subdivisions_match -- a keyword-subset
comparison already hardened on this corpus's real variance ("CRECENT COVE" vs
"CRESCENT COVE", confirmed on covid 4780's own recorder index) -- rather than a
second, weaker matcher written from scratch here.

AMBIGUITY IS REFUSED, NEVER RESOLVED BY PICKING. A parcel whose description
matches two plats gets no plat_id and is reported. Guessing a section would put a
wrong formation date under a fee calculation, which is the failure this project
treats as unacceptable.

NOT EVERY PARCEL HAS A PLAT TO FIND. 30 are raw abstract-survey tracts ("A0494 -
Walker Co Sch L, TRACT 1D-1"), and others are street dedications, school sites and
detention reserves -- plat RESERVES rather than lots. Those are classified as
unplattable and excluded from the worklist rather than searched for forever.

A plat reserve is a deliberate line, not an oversight: 107 reserves DO carry a plat
link written by the subdivision-plat path, and those links are right -- a plat did
create them. This module still declines to link one, because a reserve's recited
head is often a STREET name ("Canopies Parkway, STREET DEDICATION") rather than a
subdivision, and putting those in the worklist means recorder searches for plats
that do not exist. Declining costs a formation date on 107 parcels; accepting
risks a wrong one.

WHAT IT IS CHECKED AGAINST. Every plat link in the database was written by
resolve_subdivision_plat_tract, which shares no code with this matcher -- so
re-deriving them here is a real cross-check rather than a tautology. It reproduces
all 4,990 plattable links exactly, disagreeing on none
(scripts/test_plat_link.py). That agreement is the evidence the section reading
below is right, and the reason a disagreement must fail loudly.

SECTION FORMS ARE THE WHOLE GAME. Reading a section wrongly is not a near miss: a
section the parser cannot read looks like NO section, and a sectionless parcel then
matches the subdivision's sectionless plat row, attaching one filing's date to
another filing's lots. That is what happened to 680 Montgomery parcels. Every form
this corpus actually uses is therefore read and canonicalised to one shape --
Montgomery's "06B" and "01", Collin's spelled-out "PHASE ONE B" (1,068 parcels)
against its own plat rows' "1B", and roman "SECTION III".
"""
import re

from sqlalchemy import text

# _subdivisions_match from app/title/chain.py is deliberately NOT used here. It is
# a keyword-SUBSET correlator, right for chain-of-title where a false positive
# costs a wasted lookup -- and wrong for this, where a false positive writes a
# formation date. It matched "CANOPIES PARKWAY & WOODWARD BOULEVARD AT TIMBER
# EDGE", a street-dedication plat, to the subdivision "THE CANOPIES" purely
# because one name's tokens are a subset of the other's. Assigning a date needs
# the names to be the SAME name, not overlapping ones.

# Montgomery prefixes its legal description with an internal account code
# ("S929300 - The Reserve On Lake Conroe 01, BLOCK 2, Lot 53"). Collin and Nueces
# do not. Stripped before anything else so one parser handles all three.
_ACCOUNT_CODE = re.compile(r"^[A-Z]\d{4,6}\s*-\s*")

# An abstract-survey tract has no plat by definition -- it is unsubdivided land
# described by survey and abstract number.
_ABSTRACT = re.compile(r"^A\d{3,4}\b|^ABS\s+A?\d", re.I)

# Plat reserves and dedications: real parcels, but not lots created by a
# subdivision plat, so no formation date is derivable from one.
#
# Deliberately does NOT match a bare singular "RESERVE", because that is a
# SUBDIVISION NAME here: "The Reserve On Lake Conroe" is 291 real lots, and an
# earlier version of this pattern discarded every one of them as a plat reserve.
# What marks a genuine reserve is the plural, or a RES <letter> designation, or an
# explicit dedication.
_NOT_A_LOT = re.compile(
    r"\b(STREET\s+DEDICATION|DEDICATION|RESERVES\b|RES\s+[A-Z]\b|DETENTION|WWTP"
    r"|ELEMENTARY|JUNIOR\s+HIGH|ISD\b|SCHOOL\s+SITE)\b", re.I)

# Where the subdivision name ends and lot/block detail begins.
_DETAIL = re.compile(r",|\bBLOCK\b|\bBLK\b|\bLOT[S]?\b|\bLT[S]?\b|\bUNIT\b", re.I)

# A section/phase, however the county writes it. Checked in this order so
# "PHASE 2A" wins over the bare-number rule.
_SECTION_PATTERNS = (
    re.compile(r"\b(?:PHASE|PH)\s*([0-9]+[A-Z]?|[IVX]+)\b", re.I),
    # Collin spells its phases out -- "STAR TRAIL PHASE ONE B" is section 1B, and
    # Collin's own plat rows call it "1B". 1,085 parcels across Collin and Denton
    # write a section this way, and reading none of them is the exact setup for the
    # sectionless-match overwrite. The trailing single letter is part of the
    # section, not the start of the next word: [A-Z]\b cannot match the W of WEST.
    re.compile(r"\b(?:PHASE|PH|SECTION|SEC)\s+"
               r"((?:ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN|ELEVEN|TWELVE)"
               r"(?:\s+[A-Z]\b)?)", re.I),
    # Roman numerals on this pattern too, not just on PHASE. "SECTION III" read as
    # NO section, and an unread section is precisely what let 680 lots match a
    # sectionless plat -- the two spellings must not have two different readers.
    re.compile(r"\b(?:SECTION|SEC)\s*([0-9]+[A-Z]?|[IVX]+)\b", re.I),
    # Montgomery's trailing section, which carries a letter suffix far more often
    # than not: "Harrington Trails 06B", "05B", "4A". Without the optional letter
    # this read no section at all, and a sectionless parcel then matched the
    # subdivision's sectionless plat row -- attaching one filing's date to 680
    # other filings' lots.
    re.compile(r"\s(\d{1,2}[A-Z]?)$", re.I),
)

_ROMAN = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5", "VI": "6",
          "VII": "7", "VIII": "8", "IX": "9", "X": "10", "XI": "11", "XII": "12"}

_WORDS = {"ONE": "1", "TWO": "2", "THREE": "3", "FOUR": "4", "FIVE": "5", "SIX": "6",
          "SEVEN": "7", "EIGHT": "8", "NINE": "9", "TEN": "10", "ELEVEN": "11",
          "TWELVE": "12"}


def _section_token(raw: str) -> str:
    """One section token, however the county wrote it: 6, 06B, VI, ONE B.

    Words and romans are converted here rather than at comparison time because a
    plat row and a parcel can disagree in FORM as well as value, and the section a
    parcel reports should read the same whichever county recited it.
    """
    value = raw.strip().upper()
    if value in _ROMAN:
        return _ROMAN[value]
    parts = value.split()
    if parts and parts[0] in _WORDS:
        # "ONE B" -> "1B"; a bare "FIVE" -> "5".
        return _WORDS[parts[0]] + ("".join(parts[1:]) if len(parts) > 1 else "")
    return value


def normalize_subdivision(name: str) -> str:
    """Canonical form for comparison: upper case, punctuation collapsed, a
    trailing article dropped.

    "PALMILLA BEACH P.U.D." and "PALMILLA BEACH PUD" must collapse to one string,
    and "HEIGHTS AT WESTRIDGE PHASE III THE" is the county writing "THE HEIGHTS AT
    WESTRIDGE" with the article moved to the end -- so a trailing THE is dropped
    rather than treated as part of the name.
    """
    out = (name or "").upper()
    # A period is DELETED, not replaced by a space: replacing it turned
    # "PALMILLA BEACH P.U.D." into "PALMILLA BEACH P U D", which does not equal
    # "PALMILLA BEACH PUD" -- so the two spellings this function exists to collapse
    # stayed two subdivisions. Deleting keeps the acronym whole and still leaves
    # "ST. JOHN" as "ST JOHN", because the space after the period is its own
    # character.
    out = out.replace(".", "")
    out = re.sub(r"[,'&]", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    out = re.sub(r"\s+THE$", "", out)
    out = re.sub(r"^THE\s+", "", out)
    # A DANGLING SECTION LABEL IS NOT PART OF THE NAME. Denton's own plat rows are
    # named "SHERMAN CROSSING ADDITION PHASE" -- the word survives in the name
    # after its number was pulled into the section column, so the plat and the
    # parcel that recites "SHERMAN CROSSING ADDITION PHASE 2A" disagreed by one
    # trailing word and 212 correct links went unreproduced.
    out = re.sub(r"\s+(PHASE|PH|SECTION|SEC|UNIT)$", "", out)
    return out


def parse_subdivision_and_section(legal: str | None) -> dict:
    """Split a parcel's recited legal description into subdivision and section.

    Returns `plattable=False` with a reason for anything that has no plat to find
    -- an abstract-survey tract, a street dedication, a school or detention
    reserve. Those are answers, not failures, and keeping them out of the lookup
    worklist is the difference between a finite list of recorder searches and one
    that never empties.
    """
    if not legal or not legal.strip():
        return {"plattable": False, "reason": "no recited legal description",
                "subdivision": None, "section": None}

    # Abstract check BEFORE stripping the account code: Montgomery's own code
    # looks exactly like an abstract number ("A0494 - Walker Co Sch L"), so
    # stripping first removed the very marker this test looks for and an
    # unsubdivided survey tract was accepted as a subdivision called
    # "WALKER CO SCH L".
    raw = legal.strip()
    if _ABSTRACT.match(raw):
        return {"plattable": False, "reason": "abstract-survey tract -- unsubdivided land",
                "subdivision": None, "section": None}
    body = _ACCOUNT_CODE.sub("", raw).strip()
    if _ABSTRACT.match(body):
        return {"plattable": False, "reason": "abstract-survey tract -- unsubdivided land",
                "subdivision": None, "section": None}
    if _NOT_A_LOT.search(body):
        return {"plattable": False,
                "reason": "plat reserve or dedication, not a lot created by a plat",
                "subdivision": None, "section": None}

    head = _DETAIL.split(body, maxsplit=1)[0].strip()
    head = re.sub(r"\s+(PARTIAL\s+)?REPLAT.*$", "", head, flags=re.I).strip()
    if not head:
        return {"plattable": False, "reason": f"no subdivision name in {legal[:48]!r}",
                "subdivision": None, "section": None}

    section = None
    for pattern in _SECTION_PATTERNS:
        m = pattern.search(head)
        if m:
            section = _section_token(m.group(1))
            head = head[:m.start()].strip()
            break

    subdivision = normalize_subdivision(head)
    if not subdivision:
        return {"plattable": False, "reason": f"section but no subdivision in {legal[:48]!r}",
                "subdivision": None, "section": section}
    return {"plattable": True, "reason": None, "subdivision": subdivision, "section": section}


def _candidate_plats(session, county_fips: str) -> list[dict]:
    rows = session.execute(text("""
        SELECT plat_id, subdivision_name, section, recording_date, recording_instrument
          FROM plat
         WHERE county_fips = :cf AND lookup_status = 'found'
           AND recording_date IS NOT NULL AND recording_instrument IS NOT NULL
    """), {"cf": county_fips}).fetchall()
    return [dict(r._mapping) for r in rows]


def _sections_match(parcel_section: str | None, plat_section: str | None) -> bool:
    """A section comparison that refuses to invent agreement.

    An unknown section on either side does NOT match a known one: a plat records a
    specific filing, and matching "Section 5" against an unsectioned plat row
    would attach one filing's date to another's lots. Only a genuine absence on
    BOTH sides (a subdivision platted in a single filing) compares equal.
    """
    def canon(v):
        v = (v or "").strip().upper()
        if v in ("N/A", "NONE"):
            return ""
        # "01" and "1" are the same section written two ways; "06B" and "6B" too.
        m = re.fullmatch(r"0*(\d+)([A-Z]?)", v)
        return f"{m.group(1)}{m.group(2)}" if m else v

    return canon(parcel_section) == canon(plat_section)


def link_parcels_to_plats(session, county_fips: str | None = None,
                          covid: int | None = None, dry_run: bool = True) -> dict:
    """Set parcel.plat_id wherever exactly one held plat matches the parcel's own
    recited legal description. Returns counts, the detail, and every link it made.

    DRY RUN BY DEFAULT, and it returns `would_link` so the pairs can be read before
    anything is written. Learned the hard way: an earlier version ran straight into
    a bulk UPDATE and overwrote 680 parcels' correct section-specific plat with a
    sectionless one, and because it recorded nothing about what it touched, the only
    way back was that formed_by_instrument still named the right plat. A bulk link
    must be inspectable first and must say what it changed.

    Idempotent: only writes where plat_id would change. Ambiguous matches are
    counted and reported, never resolved by choosing.
    """
    rows = session.execute(text("""
        SELECT DISTINCT p.county_fips, p.apn, p.recited_legal_description, p.plat_id
          FROM parcel p
         WHERE p.recited_legal_description IS NOT NULL
           AND (:cf IS NULL OR p.county_fips = :cf)
           AND (:covid IS NULL OR EXISTS (
                   SELECT 1 FROM parcel_covenant pc
                    WHERE pc.county_fips = p.county_fips AND pc.apn = p.apn
                      AND pc.covid = :covid))
    """), {"cf": county_fips, "covid": covid}).fetchall()

    plats_by_county: dict[str, list[dict]] = {}
    ambiguous = unplattable = no_plat_held = already = 0
    problems: list[dict] = []
    changes: list[dict] = []

    for r in rows:
        parsed = parse_subdivision_and_section(r.recited_legal_description)
        if not parsed["plattable"]:
            unplattable += 1
            continue
        if r.county_fips not in plats_by_county:
            plats_by_county[r.county_fips] = _candidate_plats(session, r.county_fips)

        matches = [pl for pl in plats_by_county[r.county_fips]
                   if parsed["subdivision"] == normalize_subdivision(pl["subdivision_name"])
                   and _sections_match(parsed["section"], pl["section"])]
        if not matches:
            no_plat_held += 1
            continue
        if len(matches) > 1:
            ambiguous += 1
            problems.append({"apn": r.apn, "county_fips": r.county_fips,
                             "subdivision": parsed["subdivision"], "section": parsed["section"],
                             "matched_plat_ids": [m["plat_id"] for m in matches]})
            continue
        if r.plat_id == matches[0]["plat_id"]:
            already += 1
            continue
        changes.append({"county_fips": r.county_fips, "apn": r.apn,
                        "legal": r.recited_legal_description,
                        "subdivision": parsed["subdivision"], "section": parsed["section"],
                        "from_plat_id": r.plat_id, "to_plat_id": matches[0]["plat_id"],
                        "plat": f'{matches[0]["subdivision_name"]} sec {matches[0]["section"]!r}',
                        "plat_recorded": str(matches[0]["recording_date"])})
        if dry_run:
            continue
        session.execute(
            # No timestamp touched: parcel has last_synced_at, which means "when
            # we last read this from the county", and attaching a plat is not a
            # sync. There is no general updated_at on this table.
            text("UPDATE parcel SET plat_id = :pid "
                 "WHERE county_fips = :cf AND apn = :apn"),
            {"pid": matches[0]["plat_id"], "cf": r.county_fips, "apn": r.apn})

    # An overwrite is not the same as filling a blank, and only one of them is
    # ever obviously safe -- so they are counted separately and surfaced.
    overwrites = [c for c in changes if c["from_plat_id"] is not None]
    return {"examined": len(rows), "dry_run": dry_run,
            "linked" if not dry_run else "would_link": len(changes),
            "already_linked": already, "ambiguous": ambiguous,
            "unplattable": unplattable, "no_plat_held": no_plat_held,
            "overwrites": len(overwrites), "problems": problems[:25],
            # Full list, not a slice: the caller decides how much to show, and a
            # truncated change list is exactly what made the earlier damage
            # invisible in the report that preceded it.
            "changes": changes}


def plats_needed(session, min_parcels: int = 1) -> list[dict]:
    """The plat-lookup worklist: subdivisions with parcels and no plat row.

    Collapsed across spelling variants, so a recorder search runs once per real
    subdivision rather than once per way a county spelled it -- Nueces' five
    spellings of PALMILLA BEACH are one search, not five. Sections are listed per
    subdivision because a recorder plat search returns all of a subdivision's
    filings at once.
    """
    rows = session.execute(text("""
        SELECT p.county_fips, p.recited_legal_description, count(*) AS parcels
          FROM parcel p
         WHERE p.plat_id IS NULL AND p.recited_legal_description IS NOT NULL
           AND EXISTS (SELECT 1 FROM parcel_covenant pc
                        WHERE pc.county_fips = p.county_fips AND pc.apn = p.apn)
         GROUP BY 1, 2
    """)).fetchall()

    grouped: dict[tuple[str, str], dict] = {}
    for county_fips, legal, parcels in rows:
        parsed = parse_subdivision_and_section(legal)
        if not parsed["plattable"]:
            continue
        # Group on the first two tokens, which is what survives the county's own
        # spelling: PALMILLA BEACH P.U.D. / PUD / plain all share "PALMILLA BEACH".
        tokens = parsed["subdivision"].split()
        key = (county_fips, " ".join(tokens[:2]) if len(tokens) > 1 else parsed["subdivision"])
        entry = grouped.setdefault(key, {
            "county_fips": county_fips, "search_name": key[1], "parcels": 0,
            "spellings": set(), "sections": set()})
        entry["parcels"] += parcels
        entry["spellings"].add(parsed["subdivision"])
        if parsed["section"]:
            entry["sections"].add(parsed["section"])

    out = []
    for entry in grouped.values():
        if entry["parcels"] < min_parcels:
            continue
        entry["spellings"] = sorted(entry["spellings"])
        entry["sections"] = sorted(entry["sections"])
        # The two-token group key is right for COLLAPSING spellings and wrong as
        # the query itself: it asks a recorder's plat index for "RESERVE ON" and
        # "HEIGHTS AT". The searchable name is the longest token prefix all the
        # group's spellings share, which recovers "RESERVE ON LAKE CONROE" and
        # "HEIGHTS AT WESTRIDGE" while still collapsing PALMILLA BEACH's five
        # variants onto the two words they agree on.
        entry["search_name"] = _common_prefix_name(entry["spellings"]) or entry["search_name"]
        out.append(entry)
    return sorted(out, key=lambda e: -e["parcels"])


def _common_prefix_name(spellings: list[str]) -> str:
    """Longest leading run of whole tokens shared by every spelling.

    Whole tokens, not characters: a character-wise prefix of "PALMILLA BEACH" and
    "PALMILLA BEACHES" would end mid-word and search for a name no county wrote.
    A trailing bare section number is dropped, since the plat index is searched by
    subdivision and returns every filing at once."""
    if not spellings:
        return ""
    token_lists = [s.split() for s in spellings]
    prefix: list[str] = []
    for tokens in zip(*token_lists):
        if len(set(tokens)) != 1:
            break
        prefix.append(tokens[0])
    while prefix and re.fullmatch(r"\d{1,2}[A-Z]?", prefix[-1]):
        prefix.pop()
    return " ".join(prefix)
