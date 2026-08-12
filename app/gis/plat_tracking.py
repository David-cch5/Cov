"""Track when a tract's own raw acreage actually got subdivided into real,
dated lots -- as opposed to when this project's software happened to notice
(parcel_lineage.effective_date, which is stamped with the detection date,
not the plat's own recording date).

Confirmed real (covid 4440, Montgomery): "THE CANOPIES" section 3 (covering
this tract's own matched lot APN 815074) was recorded as a real plat on
2024-08-20, File# 2024082483 -- 15 years after this covenant's own 2009
recording. Montgomery's recorder portal exposes this directly: switching
its Department selector from "Public Records" to "Plats" and searching a
subdivision's base name returns every section's own recording date at once
(app/recorder/adapters/publicsearch.py's search_plats_by_subdivision).

Cost discipline: one live search per DISTINCT subdivision name found among
a tract's own matched parcels (never one per lot, never one per section --
a single search already returns every section), and never re-searched once
this project has an answer (plat.lookup_status handles both "found" and a
real "not_found" -- both are terminal, not retried every run).
"""
import re
from datetime import date, datetime

from sqlalchemy import text

from app.db.repository import insert_source, upsert_plat
from app.gis.plat_parser import extract_phase_key_from_text, normalize_section, parse_plat_reference
from app.gis.plat_link import (
    _comparison_tokens,
    parse_lot_block,
    parse_subdivision_and_section,
    link_parcels_to_plats,
    plat_row_matches_query,
    plat_search_name,
)
from app.queue.job_queue import run_with_job_queue
from app.recorder.adapters import publicsearch
from app.recorder.session import recorder_context


# A recorded date before this is a placeholder, not a date. Nueces' own index
# stamps 1/1/1800 on plat rows whose real recording date it does not carry
# (confirmed live: six PALMILLA BEACH REPLAT rows). That matters more here than
# anywhere else, because this module deliberately takes the EARLIEST date per
# section -- so a placeholder is not merely wrong, it WINS, and 1800-01-01 would
# have become the formation date of lots platted in 2013. Texas counties did not
# record subdivision plats before statehood; anything this old is the index
# saying "unknown", and unknown belongs in the date column as NULL.
_EARLIEST_PLAUSIBLE_PLAT_YEAR = 1850
_PLACEHOLDER_DATES = {date(1800, 1, 1), date(1900, 1, 1), date(1899, 12, 31)}


def _parse_slash_date(s: str | None):
    try:
        parsed = datetime.strptime((s or "").strip(), "%m/%d/%Y").date()
    except ValueError:
        return None
    # Exact sentinels too, not just the year floor: Collin stamps 1900-01-01 on
    # HEIGHTS WESTRIDGE #1, a subdivision platted in 2004. A sentinel that clears
    # the floor still wins the earliest-date pick, which is the whole danger.
    if parsed in _PLACEHOLDER_DATES or parsed.year < _EARLIEST_PLAUSIBLE_PLAT_YEAR:
        return None
    return parsed


def _flag_plat_lookup_note(session, covid: int, tract_no: int, note: str) -> None:
    """Same tagged-note convention as every other stage in this project
    (RECONCILIATION-STAGE, MONITOR-STAGE, CHAIN-OF-TITLE GAP, ...), but
    scoped per-tract in the tag itself (confirmed necessary: covid 4440
    has 2 tracts, each with its own genuinely different unresolved
    parcels -- an unscoped "PLAT LOOKUP (automated): ..." tag would have
    the second tract's own call blindly strip-and-replace the first
    tract's already-written finding, losing it). Only this exact tract's
    own prior note is ever replaced; a sibling tract's own note (or any
    other stage's) is matched up to the next recognized "; TAG (automated"
    boundary, not just the next semicolon, since a single note's own body
    can itself contain "; " between multiple findings."""
    existing = session.execute(
        text("SELECT status, review_reason FROM covenant WHERE covid = :covid"), {"covid": covid},
    ).fetchone()
    reason = existing.review_reason or ""
    reason = re.sub(
        rf";?\s*PLAT LOOKUP \(automated, tract {tract_no}\b[^)]*\):.*?(?=;\s*[A-Z][A-Z ]*\(automated|$)",
        "", reason, flags=re.DOTALL,
    ).strip("; ").strip()
    tagged = f"PLAT LOOKUP (automated, tract {tract_no}, {date.today().isoformat()}): {note}"
    reason = f"{reason}; {tagged}" if reason else tagged
    status = existing.status if existing.status in ("title_in_progress", "done") else "needs_review"
    session.execute(
        text("UPDATE covenant SET status = :status, review_reason = :reason, updated_at = now() WHERE covid = :covid"),
        {"status": status, "reason": reason, "covid": covid},
    )


def _canon_section(value: str | None) -> str:
    """One spelling for a section, so '06B' and '6B' name the same filing.

    Deliberately the same canonicalisation plat_link._sections_match applies, because
    comparing a plat's verbatim '6B' against the parcel side's '06B' as raw strings
    declared every zero-padded section still-missing and re-searched it live on every
    run."""
    v = (value or "").strip().upper()
    if v in ("N/A", "NONE"):
        return ""
    m = re.fullmatch(r"0*(\d+)([A-Z]?)", v)
    return f"{m.group(1)}{m.group(2)}" if m else v


def _sections_still_missing(session, county_fips: str,
                            recited_legals: list[str]) -> list[tuple[str, str]]:
    """(searchable name, section) pairs this tract needs and no held plat covers.

    Reads the parcels' OWN RECITED legal descriptions, not parse_plat_reference's
    output: that parser had already rewritten "PALMILLA BEACH P.U.D. UNIT 7 BLK 2
    LOT 9" to "PALMILLA BEACH BLK 2", dropping the unit -- so this asked for nothing
    at all and every unit went unsearched. The recitation is the evidence; a
    derived field that has lost part of it is not.

    Deduplicated, so one search per section however many lots recite it, and only
    for sections that are actually named -- a parcel reciting no section at all has
    nothing to search for and is left to the lot/block match instead."""
    # Sections compared through plat_link's own canonical form, not raw .upper():
    # 'held' carries the plat's verbatim '6B'/'1' while the parcel side yields
    # Montgomery's '06B' and '01', so a string compare declared every zero-padded
    # section still-missing and fired two live recorder searches for it on every run.
    held = {(tuple(_comparison_tokens(r.subdivision_name)), _canon_section(r.section))
            for r in session.execute(
                text("SELECT subdivision_name, section FROM plat "
                     "WHERE county_fips = :cf AND lookup_status = 'found'"),
                {"cf": county_fips}).fetchall()}
    wanted: dict[tuple[str, str], None] = {}
    for legal in recited_legals:
        own = parse_subdivision_and_section(legal)
        if not own["plattable"]:
            continue
        query, section = plat_search_name(legal), own["section"]
        if not query or not section:
            continue
        if (tuple(_comparison_tokens(query)), _canon_section(section)) in held:
            continue
        wanted.setdefault((query, section), None)
    return list(wanted)


def _plat_row_identity(row: dict) -> str:
    """The text on an index row that names the subdivision it plats.

    A PLAT is a dedication: its GRANTOR is the subdivision and its GRANTEE is
    "PUBLIC". So for the filing that platted PALMILLA BEACH PUD UNIT 7 (doc
    2024040337, recorded 2024-11-25) the index carries GRANTOR "PALMILLA BEACH PUD
    UNIT 7", no subdivision column at all, and LEGAL DESCRIPTION "READ GENERAL
    NOTES" -- a pointer to the plat sheet rather than a description.

    Every filter here used to read SUBDIVISION and LEGAL DESCRIPTION only, so that
    row looked nameless and was discarded, and this project then reported the plat
    as not existing. GRANTOR is consulted last, so a county that populates a real
    subdivision column still wins."""
    for key in ("SUBDIVISION", "LEGAL DESCRIPTION", "GRANTOR"):
        value = (row.get(key) or "").strip()
        if value and value.upper() not in ("N/A", "NONE", "READ GENERAL NOTES"):
            return value
    return ""


# A full document number, as opposed to a short plat file number. Only these are
# safe to look up: Nueces' plat file numbers (46201, 55219, 30286) collide with
# other book series, and looking up "46201" returns a BACKFILE OIL GAS LEASE.
_FULL_DOC_NUMBER = re.compile(r"^(19|20)\d{2}\d{5,}$")

# doc number -> the section its own record names, or None. A document's identity does
# not change between searches, so this is safe to keep for the life of the process.
_DOC_SECTION_CACHE: dict[str, str | None] = {}
_MAX_DOC_DETAIL_FETCHES = 60


def _row_section(base_url: str, county_fips: str, covid: int, row: dict) -> str | None:
    """The section this index row plats, re-reading the document itself if the row
    does not say.

    THE RESULTS LIST TRUNCATES PARTY NAMES. Doc 2024040337 is the plat of PALMILLA
    BEACH PUD UNIT 7 -- its GRANTOR reads exactly that when the document is fetched
    on its own, and reads "PALMILLA BEACH PUD" inside a multi-row result list. The
    unit, the only thing identifying which phase the filing platted, is what gets
    cut. So a candidate row whose own text names no section is re-read by document
    number before being dismissed; that one lookup is the difference between dating
    143 parcels and reporting their plat as nonexistent.

    Bounded: only for rows that lack a section, and only for full document numbers."""
    section = parse_subdivision_and_section(_plat_row_identity(row))["section"]
    if section is not None:
        return section
    doc = str(row.get("FILE NUMBER") or row.get("DOC NUMBER") or "").strip()
    if not _FULL_DOC_NUMBER.match(doc):
        return None
    # MEMOISED, AND CAPPED. This runs inside a comprehension over every row of every
    # phrasing of every missing section: a subdivision with 12 unaccounted sections
    # and 50 rows a search would have re-fetched the same documents up to 1,200 times.
    # The cache is process-wide because the answer is a property of the document, and
    # the cap keeps one tract's worth of lookups bounded however many sections miss.
    if doc in _DOC_SECTION_CACHE:
        return _DOC_SECTION_CACHE[doc]
    if len(_DOC_SECTION_CACHE) >= _MAX_DOC_DETAIL_FETCHES:
        return None
    def _call():
        with recorder_context() as context:
            return publicsearch.search_by_document_number(context, base_url, doc)
    try:
        detail = run_with_job_queue(
            _call, job_type="title_plat_doc_detail", county_fips=county_fips, covid=covid,
            payload={"base_url": base_url, "doc_number": doc})
    except Exception:
        return None  # the row stays unidentified rather than guessed at
    resolved = (parse_subdivision_and_section(_plat_row_identity(detail))["section"]
                if detail else None)
    _DOC_SECTION_CACHE[doc] = resolved
    return resolved


def _row_is_for(row: dict, query: str) -> bool:
    """Keep this index row for the subdivision we searched?

    A row that carries NO subdivision name is KEPT. Montgomery's Plats department
    returns exactly that -- SECTION, dates and file numbers, no subdivision column
    at all -- because the department search is itself the scoping: it was asked for
    one subdivision and answers with that subdivision's filings. Requiring a name
    to match discarded all 9 real GLENEAGLES plats.

    A row that DOES carry a name must match it, which is what stops a broadened
    query from dragging in a neighbour (see plat_row_matches_query).
    """
    name = _plat_row_identity(row)
    if not name:
        return True
    return plat_row_matches_query(name, query)


def resolve_plats_for_tract(session, covid: int, tract_no: int) -> dict:
    """For every matched parcel in this tract not yet tied to a plat: parse
    its recited legal description (platted vs. still-raw abstract tract vs.
    ambiguous), look up any not-yet-searched subdivision's real plat dates
    live (once per subdivision name), and assign parcel.plat_id wherever a
    (subdivision, section) match is found. Never guesses a match -- an
    ambiguous description or an unresolved section is flagged on the
    covenant, not silently skipped or silently assumed.

    ASKS FOR THE PLAT'S NAME, RECORDS UNDER THE PARCEL'S. A CAD's recited string
    is frequently not a name any plat index holds, so searching it verbatim found
    nothing at all for whole counties:

        recited by the CAD                                  searched as
        PALMILLA BEACH PUD UNIT 4C 2175 SQFT OUT OF BLK 10  PALMILLA BEACH
        HEIGHTS AT WESTRIDGE PHASE III                      HEIGHTS AT WESTRIDGE
        GLENEAGLES 04                                       GLENEAGLES
        WATERMARK 01 PHASE                                  WATERMARK

    The collapse is plat_link.plat_search_name, shared rather than reimplemented
    here. Results are narrowed back with plat_link.plat_row_matches_query, which
    accepts only rows that EXTEND the query -- so a broader search cannot drag in
    a neighbouring subdivision, and THE CANOPIES can never answer a search for
    the Canopies Parkway & Woodward Boulevard at Timber Edge plat (both are real
    plats with real lots).

    Plat rows are keyed on the collapsed name -- the plat's own identity -- and
    parcel assignment is delegated to plat_link.link_parcels_to_plats rather than
    re-implemented here.

    A failed search still writes lookup_status='not_found', and because
    `already_known` is keyed on the recited name, that row SUPPRESSES the next
    search for it. So a 'not_found' written by an OLD verbatim-string search is an
    artifact of the query rather than evidence -- 33 such rows were deleted by
    hand on 2026-08-11. Delete, don't trust, any that predate this change."""
    row = session.execute(
        text("""
            SELECT c.county_fips, r.base_url
            FROM tract t JOIN covenant c ON c.covid = t.covid
            JOIN county_recorder_registry r ON r.county_fips = c.county_fips
            WHERE t.covid = :covid AND t.tract_no = :tract_no
        """),
        {"covid": covid, "tract_no": tract_no},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} tract {tract_no}: no county_recorder_registry entry for this county")
    county_fips, base_url = row.county_fips, row.base_url

    # DISTINCT on (county_fips, apn), not a plain join: parcel_covenant carries one row
    # per (apn, run_seq) and a tract can accumulate several run_seq batches over its
    # life (each classify_metes_and_bounds_tract re-run inserts a fresh one, by design
    # -- monitor_run is an audit trail, not a cache) -- a plain join would process, and
    # report stats on, the same real parcel once per historical batch.
    parcels = session.execute(
        text("""
            SELECT DISTINCT p.apn, p.recited_legal_description
            FROM parcel_covenant pc JOIN parcel p ON p.county_fips = pc.county_fips AND p.apn = pc.apn
            WHERE pc.covid = :covid AND pc.tract_no = :tract_no AND p.plat_id IS NULL
              AND p.recited_legal_description IS NOT NULL
        """),
        {"covid": covid, "tract_no": tract_no},
    ).fetchall()

    parsed, ambiguous_apns = {}, []
    for p in parcels:
        ref = parse_plat_reference(p.recited_legal_description)
        if ref is None:
            ambiguous_apns.append(p.apn)
        else:
            parsed[p.apn] = ref

    # THE SEARCH SET COMES FROM THE RECITATION, through the same parser that does
    # the matching. It used to come from parse_plat_reference's derived
    # subdivision_name, which is a chain of shape-anchored regexes fitted to
    # Montgomery's CAD -- it rewrote "PALMILLA BEACH P.U.D. UNIT 7 BLK 2 LOT 9" to
    # "PALMILLA BEACH BLK 2" and returned nothing at all for shapes it did not
    # recognise. Whole subdivisions inside a tract were therefore never searched:
    # SUNFLOWER BEACH, LAGUNA ISLES, LA JOYA DE ISLAND MOORINGS and four
    # condominium projects, ~200 parcels, silently absent from the worklist rather
    # than reported as unfound.
    #
    # One parser for searching and matching is the point. Two parsers drift, and
    # when they do the failure is invisible: the search asks for a name the matcher
    # will never produce, so nothing matches and nothing looks broken.
    queries: dict[str, list[str]] = {}
    for p in parcels:
        own = parse_subdivision_and_section(p.recited_legal_description)
        if not own["plattable"]:
            continue
        query = plat_search_name(p.recited_legal_description)
        if query:
            queries.setdefault(query, []).append(p.apn)

    # Compared on collapsed names as well, or a plat already held under its
    # collapsed name would be re-searched on every single run.
    held = {r.subdivision_name for r in session.execute(
        text("SELECT DISTINCT subdivision_name FROM plat WHERE county_fips = :cf"),
        {"cf": county_fips}).fetchall()}
    held_tokens = {tuple(_comparison_tokens(name)) for name in held}
    # A held name SATISFIES a longer query it is a prefix of. When the two-word retry
    # succeeds, plats are stored under the short name ("CANOPIES PARKWAY") while the
    # recitation keeps yielding the long one, so an equality test never suppressed it
    # and both searches re-fired on every single run -- permanently, for exactly the
    # subdivisions the retry exists to rescue.
    def _already_held(query: str) -> bool:
        q = tuple(_comparison_tokens(query))
        return any(h == q[:len(h)] for h in held_tokens if h)

    to_search = sorted(q for q in queries if not _already_held(q))

    plats_found, plats_not_found = 0, 0
    def _search(name):
        with recorder_context() as context:
            return publicsearch.search_plats_by_subdivision(context, base_url, name)

    for query in to_search:
        subdivision_name = query  # what a 'not_found' row is recorded against
        rows = run_with_job_queue(
            lambda name=query: _search(name), job_type="title_plat_lookup",
            county_fips=county_fips, covid=covid,
            payload={"base_url": base_url, "subdivision_name": subdivision_name,
                     "searched_as": query},
        )
        # A LONGER NAME MAY SIMPLY NOT BE INDEXED. Montgomery answers "CANOPIES
        # PARKWAY" with the two real Canopies Parkway & Woodward Boulevard at Timber
        # Edge plats and answers the full name with nothing at all. So one retry on
        # the first two words, and only when the full query found nothing -- never
        # as the first attempt, because the shorter the query the more it can drag
        # in.
        if not rows and len(query.split()) > 2:
            short = " ".join(query.split()[:2])
            rows = run_with_job_queue(
                lambda name=short: _search(name), job_type="title_plat_lookup",
                county_fips=county_fips, covid=covid,
                payload={"base_url": base_url, "subdivision_name": subdivision_name,
                         "searched_as": short, "after_no_hit_for": query})
            query = short if rows else query

        rows = [r for r in rows if _row_is_for(r, query)]
        plat_source_id = insert_source(
            session, source_type="recorder_portal", reference=f"{base_url} (plats department)", confidence=None,
        )
        if not rows:
            upsert_plat(
                session, county_fips=county_fips, subdivision_name=subdivision_name, section="",
                lookup_status="not_found", recording_instrument=None, recording_date=None,
                book_volume_page=None, abstract_name=None, source_id=plat_source_id,
            )
            plats_not_found += 1
            continue

        # Montgomery's own rows always carry a populated SECTION column; Collin's Plats
        # department has no such column at all (confirmed real, covid 3028) -- one search
        # of the shared base name ("STAR TRAIL") returns every phase's own row, but each
        # row's own phase has to be pulled out of whichever free-text field states it
        # (see extract_phase_key_from_text's own docstring for why that varies even
        # within Collin's own index). A genuinely un-derivable row (no SECTION column and
        # no phase-shaped text in any field) is skipped here, not guessed at -- it never
        # reaches a real parcel's own assignment lookup below since that's keyed on the
        # SAME extraction, so nothing is silently mismatched.
        # A FILING THAT REPLATTED ONE LOT is keyed by that lot, not by a section it
        # does not have. "PALMILLA BEACH Lot: 14C Block: 3" recorded 2018-06-12 is
        # the instrument that created lot 14C of block 3, and for a subdivision
        # whose parcels recite a phase no phase-plat covers, it is the only evidence
        # there is. Recorded separately from the section rows below because a row
        # naming several lots ("5A ET AL") states no single lot and is skipped by
        # parse_lot_block rather than attached to whichever lot is asked about.
        for r in rows:
            lb = parse_lot_block(f'Lot: {r.get("LOT") or ""} Block: {r.get("BLOCK") or ""}')
            if lb["indeterminate"] or not lb["block"] or len(lb["lots"]) != 1:
                continue
            recorded = _parse_slash_date(r.get("RECORDED DATE"))
            instrument = r.get("FILE NUMBER") or r.get("DOC NUMBER")
            if recorded is None or not instrument:
                continue
            upsert_plat(
                session, county_fips=county_fips, subdivision_name=query, section="",
                lot=lb["lots"][0], block=lb["block"], lookup_status="found",
                recording_instrument=instrument, recording_date=recorded,
                book_volume_page=r.get("VOL/BK/PG") or None,
                abstract_name=r.get("ABSTRACT NAME") or None, source_id=plat_source_id)
            plats_found += 1

        by_section: dict[str, list[dict]] = {}
        for r in rows:
            section = (
                normalize_section(r["SECTION"]) if r.get("SECTION")
                else extract_phase_key_from_text(r.get("GRANTOR"), r.get("GRANTEE"), r.get("LEGAL DESCRIPTION"))
            )
            if section is None:
                # Nueces names the filing in the subdivision itself -- "PALMILLA
                # BEACH UNIT 1B" -- and UNIT is its word for a phase, which
                # extract_phase_key_from_text does not read. plat_link's parser
                # does, and it is the same parser the parcel side is read with, so
                # both ends of the match speak one vocabulary.
                section = parse_subdivision_and_section(_plat_row_identity(r))["section"]
            if section is None:
                continue
            by_section.setdefault(section, []).append(r)

        for section, section_rows in by_section.items():
            # The land's own FIRST real plat date is what this project tracks (a later
            # amendment/replat of the same already-platted phase doesn't un-platt it) --
            # confirmed real necessary: Collin's own "Star Trail" phase 8 has two rows,
            # an original 2021-12-21 plat and a 2022-06-02 one, and upsert_plat's own
            # ON CONFLICT is last-write-wins, so iterating rows in an arbitrary order
            # could silently keep the LATER date instead.
            earliest = min(section_rows, key=lambda r: _parse_slash_date(r.get("RECORDED DATE")) or date.max)
            # A section whose every row carries a placeholder date (Nueces' 1/1/1800,
            # Collin's 1/1/1900) would be written 'found' with recording_date NULL --
            # a row that asserts nothing, is skipped by plat_link's candidate query,
            # and yet puts the subdivision into `held` so it is never searched again.
            # Leaving it unwritten keeps the section findable on the next run.
            if _parse_slash_date(earliest.get("RECORDED DATE")) is None:
                continue
            upsert_plat(
                # Keyed on the PLAT's own collapsed name, not the reciting parcel's
                # string. Storing it under the recited name conflated two different
                # things: Collin's index numbers its filings "#1..#8" while its CAD
                # writes them "PHASE III", so searching four recited names wrote the
                # same nine plats four times over, each copy carrying a name saying
                # PHASE III and a section saying 7. plat_link matches names with
                # connectors removed, which is what lets the CAD's "HEIGHTS AT
                # WESTRIDGE" find the index's "HEIGHTS WESTRIDGE".
                session, county_fips=county_fips, subdivision_name=query,
                section=section, lookup_status="found",
                recording_instrument=earliest.get("FILE NUMBER") or earliest.get("DOC NUMBER") or None,
                recording_date=_parse_slash_date(earliest.get("RECORDED DATE")),
                book_volume_page=earliest.get("VOL/BK/PG") or None, abstract_name=earliest.get("ABSTRACT NAME") or None,
                source_id=plat_source_id,
            )
            plats_found += 1

    # SECOND PASS: ASK FOR THE PHASE BY NAME. A subdivision-wide search answers with
    # what the index returns for that name, and this vendor caps the result set --
    # "PALMILLA BEACH" came back with units 1B through 1H and 2 while the parcels sit
    # in units 1, 2A, 3B, 3C, 4A-4C, 5, 6A, 6B and 7. Searching "PALMILLA BEACH UNIT
    # 6A" returns that unit's own plat ("PALMILLA BEACH P U D Unit: 6A", recorded
    # 2022-11-09) immediately. So each section still unaccounted for gets one search
    # of its own -- bounded by the number of distinct sections, not lots, and only
    # for sections the broad search failed to cover.
    for wanted_query, wanted_section in _sections_still_missing(
            session, county_fips, [p.recited_legal_description for p in parcels]):
        for phrasing in (f"{wanted_query} UNIT {wanted_section}",
                          f"{wanted_query} {wanted_section}"):
            rows = run_with_job_queue(
                lambda name=phrasing: _search(name), job_type="title_plat_lookup",
                county_fips=county_fips, covid=covid,
                payload={"base_url": base_url, "searched_as": phrasing,
                         "for_section": wanted_section})
            hits = [r for r in rows
                    if _row_section(base_url, county_fips, covid, r) == wanted_section
                    and _parse_slash_date(r.get("RECORDED DATE")) is not None
                    and (r.get("FILE NUMBER") or r.get("DOC NUMBER"))]
            if not hits:
                continue
            # The section's OWN first filing, same rule as the main pass: a later
            # amendment of an already-platted phase does not un-platt it.
            first = min(hits, key=lambda r: _parse_slash_date(r.get("RECORDED DATE")))
            upsert_plat(
                session, county_fips=county_fips, subdivision_name=wanted_query,
                section=wanted_section, lookup_status="found",
                recording_instrument=first.get("FILE NUMBER") or first.get("DOC NUMBER"),
                recording_date=_parse_slash_date(first.get("RECORDED DATE")),
                book_volume_page=first.get("VOL/BK/PG") or None,
                abstract_name=first.get("ABSTRACT NAME") or None,
                source_id=insert_source(session, source_type="recorder_portal",
                                        reference=f"{base_url} (plats, {phrasing})",
                                        confidence=None))
            plats_found += 1
            break

    # ASSIGNMENT IS DELEGATED, not duplicated. plat_link.link_parcels_to_plats is
    # the matcher that reads every section form this corpus uses (01/1/06B/ONE
    # B/III) and compares names with connectors removed -- and it is checked
    # against every link already on record. The exact-string lookup that used to
    # live here could only match when the CAD and the index happened to spell the
    # subdivision identically, which is exactly the failure this rewrite is about.
    link_result = link_parcels_to_plats(session, county_fips=county_fips, covid=covid,
                                        dry_run=False)
    assigned = link_result["linked"]
    unresolved = [
        (r.apn, r.recited_legal_description, "")
        for r in session.execute(
            text("""
                SELECT DISTINCT p.apn, p.recited_legal_description
                  FROM parcel_covenant pc
                  JOIN parcel p ON p.county_fips = pc.county_fips AND p.apn = pc.apn
                 WHERE pc.covid = :covid AND pc.tract_no = :tract_no
                   AND p.plat_id IS NULL AND p.recited_legal_description IS NOT NULL
            """), {"covid": covid, "tract_no": tract_no}).fetchall()
        if parse_plat_reference(r.recited_legal_description) is not None
        and parse_plat_reference(r.recited_legal_description).platted
    ]

    notes = []
    if ambiguous_apns:
        shown = ", ".join(ambiguous_apns[:10]) + ("..." if len(ambiguous_apns) > 10 else "")
        notes.append(f"{len(ambiguous_apns)} parcel(s) have a recited legal description this project's plat "
                      f"parser doesn't recognize as either platted or raw ({shown}) -- needs manual review")
    if unresolved:
        detail = "; ".join(f"{apn}: {name!r} sec {section!r}" for apn, name, section in unresolved[:10])
        notes.append(f"{len(unresolved)} parcel(s) recite a platted subdivision/section not found among this "
                      f"project's own plat search results ({detail}) -- needs manual review")
    if notes:
        _flag_plat_lookup_note(session, covid, tract_no, "; ".join(notes))

    return {
        "parcels_considered": len(parcels),
        "platted_parcels_parsed": sum(1 for r in parsed.values() if r.platted),
        "raw_tract_parcels_parsed": sum(1 for r in parsed.values() if not r.platted),
        "ambiguous_parcels": len(ambiguous_apns),
        "subdivisions_searched": len(to_search),
        "plats_found": plats_found,
        "plats_not_found": plats_not_found,
        "parcels_assigned_plat": assigned,
        "unresolved_section_parcels": len(unresolved),
    }


def platting_timeline(session, covid: int, tract_no: int) -> dict:
    """Reconstructs, from already-resolved plat.recording_date facts (no new
    lookups here -- pure deterministic aggregation), how much of the tract
    was platted as of each real plat-recording event, oldest first. Any
    acreage not tied to a 'found' plat (whether it's a still-raw abstract-
    tract parcel, an unmatched residual, or a parcel plat_tracking hasn't
    resolved yet) simply hasn't contributed to "platted" at any date --
    conservative by construction, never assumed platted without a real date."""
    tract_row = session.execute(
        text("SELECT ST_Area(geom::geography) / 4046.8564224 AS acres FROM tract WHERE covid = :covid AND tract_no = :tract_no"),
        {"covid": covid, "tract_no": tract_no},
    ).fetchone()
    if tract_row is None:
        raise RuntimeError(f"covid {covid} tract {tract_no} not found")
    tract_acreage = float(tract_row.acres)

    # distinct_matched_parcels de-duplicates BEFORE joining to plat: parcel_covenant
    # carries one row per (apn, run_seq), and this tract's own history includes
    # several run_seq batches from repeated classify_metes_and_bounds_tract runs --
    # summing p.acreage straight off a plain join would multiply-count the same
    # real parcel's acreage once per historical batch (confirmed real: this
    # produced an obviously-wrong 716-parcel, inflated-acreage first event before
    # this fix, on a subdivision phase that's really ~179 parcels).
    rows = session.execute(
        text("""
            WITH distinct_matched_parcels AS (
                SELECT DISTINCT p.apn, p.acreage, p.plat_id
                FROM parcel p
                JOIN parcel_covenant pc ON pc.county_fips = p.county_fips AND pc.apn = p.apn
                WHERE pc.covid = :covid AND pc.tract_no = :tract_no AND p.plat_id IS NOT NULL
            )
            SELECT pl.recording_date, pl.subdivision_name, pl.section, pl.recording_instrument,
                   SUM(dmp.acreage) AS platted_acreage, count(*) AS n_parcels
            FROM distinct_matched_parcels dmp
            JOIN plat pl ON pl.plat_id = dmp.plat_id
            WHERE pl.recording_date IS NOT NULL
            GROUP BY pl.recording_date, pl.subdivision_name, pl.section, pl.recording_instrument
            ORDER BY pl.recording_date, pl.subdivision_name, pl.section
        """),
        {"covid": covid, "tract_no": tract_no},
    ).fetchall()

    events, cumulative = [], 0.0
    for r in rows:
        cumulative += float(r.platted_acreage or 0)
        events.append({
            "recording_date": str(r.recording_date),
            "subdivision_name": r.subdivision_name,
            "section": r.section,
            "recording_instrument": r.recording_instrument,
            "parcels_platted": r.n_parcels,
            "acreage_this_event": float(r.platted_acreage or 0),
            "cumulative_platted_acreage": cumulative,
            "remaining_raw_acreage": tract_acreage - cumulative,
        })

    return {"tract_acreage": tract_acreage, "events": events}
