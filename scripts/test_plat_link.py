"""Tests for app/gis/plat_link.py -- parcel -> plat matching.

Every case below is a bug this module actually had against real data, not a
hypothetical. Four of them were found only after a bulk run had already written
wrong data (one overwrote 680 parcels' correct plat), so they are pinned here:

  Reserve-in-a-name     "The Reserve On Lake Conroe" is 291 lots, not a plat reserve
  abstract-then-code    "A0494 - Walker Co Sch L" -- the account code looks like an
                        abstract number, so the abstract test must run first
  exact-not-subset      a street-dedication plat is not the subdivision whose name
                        its own name contains
  section "06B"         an unreadable section made 680 lots match a sectionless plat
  honest counting       `linked` double-counted, reporting 52 writes for 26

Usage: python3 scripts/test_plat_link.py
"""
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from app.db.session import get_session
from app.gis.plat_link import (
    _comparison_tokens,
    _sections_match,
    link_parcels_to_plats,
    parse_lot_block,
    plat_matches_parcel,
    plat_row_matches_query,
    plat_search_name,
    normalize_subdivision,
    parse_subdivision_and_section,
    plats_needed,
)


def test_reserve_in_a_subdivision_name_is_still_a_lot() -> None:
    """The bug that discarded 291 real Montgomery lots: a singular "Reserve" is
    part of a subdivision NAME here. Only the plural, a RES <letter>, or an
    explicit dedication marks a genuine plat reserve."""
    lots = parse_subdivision_and_section(
        "S929300 - The Reserve On Lake Conroe 01, BLOCK 2, Lot 53")
    assert lots["plattable"], lots
    assert lots["subdivision"] == "RESERVE ON LAKE CONROE", lots
    # The county's own spelling is preserved; canonicalising is _sections_match's
    # job, so nothing here has to guess whether "01" or "1" is the true form.
    assert lots["section"] == "01", lots

    for genuine in ("Harrington Trails 06B, RESERVES A",
                    "Canopies Parkway, RES B",
                    "Timber Edge, STREET DEDICATION",
                    "Some Place, DETENTION",
                    "Woodward Blvd, SCHOOL SITE"):
        got = parse_subdivision_and_section(genuine)
        assert not got["plattable"], f"{genuine!r} should be unplattable, got {got}"
    print("PASS: a singular 'Reserve' in a name stays a lot; plural/RES/dedication "
          "are refused")


def test_abstract_is_detected_before_the_account_code_is_stripped() -> None:
    """Montgomery's account code is shaped exactly like an abstract number, so
    stripping it first destroyed the marker and an unsubdivided survey tract was
    accepted as a subdivision called "WALKER CO SCH L"."""
    got = parse_subdivision_and_section("A0494 - Walker Co Sch L, TRACT 1D-1")
    assert not got["plattable"], got
    assert "abstract" in got["reason"], got

    # And a real account code on a real subdivision still gets stripped.
    got = parse_subdivision_and_section("S929300 - Townsend Reserve 01, BLOCK 1, Lot 7")
    assert got["plattable"] and got["subdivision"] == "TOWNSEND RESERVE", got
    print("PASS: 'A0494 - ...' reads as an abstract tract; 'S929300 - ...' is stripped")


def test_names_must_match_exactly_not_as_a_subset() -> None:
    """chain.py's keyword-subset correlator matched the street-dedication plat
    "CANOPIES PARKWAY & WOODWARD BOULEVARD AT TIMBER EDGE" to the subdivision
    "THE CANOPIES". Right for a lookup hint, wrong for writing a formation date."""
    dedication = normalize_subdivision("CANOPIES PARKWAY & WOODWARD BOULEVARD AT TIMBER EDGE")
    canopies = normalize_subdivision("THE CANOPIES")
    assert dedication != canopies, (dedication, canopies)
    assert canopies == "CANOPIES", canopies
    # The variance the exact matcher must still absorb.
    assert (normalize_subdivision("PALMILLA BEACH P.U.D.")
            == normalize_subdivision("PALMILLA BEACH PUD") == "PALMILLA BEACH PUD")
    assert (normalize_subdivision("HEIGHTS AT WESTRIDGE PHASE III THE")
            == "HEIGHTS AT WESTRIDGE PHASE III")
    print("PASS: subset names stay distinct; punctuation and a trailing article collapse")


def test_section_06b_is_read_and_canonicalised() -> None:
    """The overwrite: the parser could not read Montgomery's "06B", so 680
    parcels looked sectionless and matched the subdivision's sectionless plat --
    attaching one filing's date to 680 other filings' lots."""
    got = parse_subdivision_and_section("S581206 - Harrington Trails 06B, BLOCK 1, Lot 12")
    assert got["section"] == "06B", got
    assert got["subdivision"] == "HARRINGTON TRAILS", got

    assert _sections_match("06B", "6B"), "leading zero is not a different section"
    assert _sections_match("01", "1")
    assert _sections_match(None, "") and _sections_match(None, None)
    assert _sections_match("2", "02")
    # The asymmetry that matters: absence on one side is not agreement.
    assert not _sections_match("06B", "6"), "6B and 6 are different filings"
    assert not _sections_match("5", None), "a known section must not match an unsectioned plat"
    assert not _sections_match(None, "5")
    assert not _sections_match("1", "2")
    # Phase and roman forms.
    assert parse_subdivision_and_section("Foo Ranch Phase 2A, Lot 1")["section"] == "2A"
    assert parse_subdivision_and_section("Foo Ranch Section III, Lot 1")["section"] == "3"
    print("PASS: '06B' parses and canonicalises to 6B; absence never matches a "
          "known section")


def test_worded_phases_are_read() -> None:
    """Collin spells phases out for 1,068 of its parcels and writes the matching
    plat row as "1B". Reading "PHASE ONE B" as no section at all is the same
    failure that caused the 680-parcel overwrite, one county over."""
    for legal, sub, sec in [
        ("STAR TRAIL PHASE ONE B, BLK B, LOT 2", "STAR TRAIL", "1B"),
        ("STAR TRAIL PHASE ONE A, BLK G, LOT 8", "STAR TRAIL", "1A"),
        ("STAR TRAIL PHASE TWO, BLK S, LOT 26", "STAR TRAIL", "2"),
        ("STAR TRAIL PHASE SIX, BLK NN, LOT 10", "STAR TRAIL", "6"),
    ]:
        got = parse_subdivision_and_section(legal)
        assert (got["subdivision"], got["section"]) == (sub, sec), (legal, got)
    assert _sections_match("1B", "1B") and not _sections_match("1B", "1")
    # A trailing word is not a section letter: [A-Z]\b must not eat the W of WEST.
    assert parse_subdivision_and_section("Foo Phase Two West, Lot 1")["section"] == "2"
    assert parse_subdivision_and_section("Foo Phase Two West, Lot 1")["subdivision"] == "FOO"
    print("PASS: 'PHASE ONE B' reads as section 1B; a following word is not a "
          "section letter")


def test_matcher_agrees_with_the_independent_subdivision_plat_path() -> None:
    """The strongest available check: every existing plat link was written by
    resolve_subdivision_plat_tract, which shares no code with this matcher. So
    re-deriving those links here is a genuine cross-check, and a disagreement means
    one of the two paths is wrong -- which must fail loudly rather than silently
    overwrite, since these links carry formation dates."""
    from app.gis.plat_link import _candidate_plats, choose_plats_for_parcel

    with get_session() as session:
        rows = session.execute(text("""
            SELECT p.county_fips, p.apn, p.recited_legal_description AS legal, p.plat_id
              FROM parcel p JOIN plat pl ON pl.plat_id = p.plat_id
             WHERE p.recited_legal_description IS NOT NULL
        """)).fetchall()
        plats: dict[str, list[dict]] = {}
        reproduced = declined = 0
        for r in rows:
            parsed = parse_subdivision_and_section(r.legal)
            if not parsed["plattable"]:
                declined += 1  # plat reserves: linked by the other path, not by this one
                continue
            plats.setdefault(r.county_fips, _candidate_plats(session, r.county_fips))
            # The module's OWN selection, not a copy of it. A duplicated comparison
            # here went stale twice today -- once when the real matcher learned to
            # ignore connector words, once when it learned that a lot-specific replat
            # outranks a phase plat -- and both times this reported correct links as
            # disagreements.
            matches = choose_plats_for_parcel(parsed, plats[r.county_fips])
            assert len(matches) == 1 and matches[0]["plat_id"] == r.plat_id, (
                f"parcel {r.apn} ({r.county_fips}) {r.legal!r} is linked to plat "
                f"{r.plat_id} but this matcher derives "
                f"{[m['plat_id'] for m in matches]}")
            reproduced += 1
        assert reproduced > 4600, reproduced
    print(f"PASS: this matcher independently re-derives all {reproduced:,} plattable "
          f"links written by the subdivision-plat path, and declines {declined} plat "
          f"reserves")


def test_collapsed_search_name_and_the_directional_filter() -> None:
    """What to ASK a recorder for, and what to accept back.

    Searching the CAD's own recited string found nothing at all for 630 Nueces and
    85 Collin parcels whose plats were in the index the whole time -- Collin files
    "HEIGHTS AT WESTRIDGE" as "HEIGHTS WESTRIDGE #3 MCKINNEY", connector dropped and
    city appended."""
    for recited, expected in [
        ("PALMILLA BEACH PUD UNIT 4C .5472 ACS OUT OF BLK 10", "PALMILLA BEACH"),
        ("PALMILLA BEACH .0186 ACS OUT OF BLK 3", "PALMILLA BEACH"),
        ("HEIGHTS AT WESTRIDGE PHASE III", "HEIGHTS WESTRIDGE"),
        ("THE RESERVE ON LAKE CONROE 01 PARTIAL REPLAT NO", "RESERVE ON LAKE CONROE"),
        ("GLENEAGLES 04", "GLENEAGLES"),
        ("WATERMARK 01 PHASE", "WATERMARK"),
    ]:
        assert plat_search_name(recited) == expected, (recited, plat_search_name(recited))
    assert plat_search_name("A0494 - Walker Co Sch L, TRACT 1D-1") is None

    # UNIT is Nueces' word for a phase, so it is the SECTION, not a detail boundary.
    got = parse_subdivision_and_section("PALMILLA BEACH PUD UNIT 4C .5472 ACS OUT OF BLK 10")
    assert (got["subdivision"], got["section"]) == ("PALMILLA BEACH PUD", "4C"), got

    # The filter is directional: a row may EXTEND the query, never shorten it.
    assert plat_row_matches_query("PALMILLA BEACH PUD", "PALMILLA BEACH")
    assert plat_row_matches_query("HEIGHTS WESTRIDGE #3 MCKINNEY", "HEIGHTS WESTRIDGE")
    assert plat_row_matches_query("STAR TRAIL #5 PROSPER", "STAR TRAIL")
    assert not plat_row_matches_query("KIM BEACH LLC", "PALMILLA BEACH")
    assert not plat_row_matches_query("EAGLES NEST WESTRIDGE #2", "HEIGHTS WESTRIDGE")
    # The Canopies case, both ways. Two separate real plats: THE CANOPIES, and the
    # Canopies Parkway & Woodward Boulevard at Timber Edge plat, which carries
    # platted lots as well as the street dedication.
    parkway = "CANOPIES PARKWAY & WOODWARD BOULEVARD AT TIMBER EDGE"
    assert not plat_row_matches_query("THE CANOPIES", plat_search_name(parkway)), (
        "the shorter subdivision must never answer a search for the longer plat")
    assert plat_row_matches_query(parkway, plat_search_name(parkway))
    assert plat_search_name(parkway) and "CANOPIES" in plat_search_name(parkway)
    print(f"PASS: recited strings collapse to searchable names; a row may extend a "
          f"query but never shorten it, so THE CANOPIES cannot answer for the "
          f"Canopies Parkway plat")


def test_a_plat_that_created_one_lot_is_matched_by_that_lot() -> None:
    """Not every plat is a phase. A recorder's index carries filings that replatted
    a SINGLE LOT ("PALMILLA BEACH Lot: 14C Block: 3", 2018-06-12), and for a parcel
    whose own phase no phase-plat covers, that filing is the only evidence of when
    it came into existence."""
    parcel = parse_subdivision_and_section("PALMILLA BEACH P.U.D. UNIT 7 BLK 3 LOT 14C")
    assert (parcel["subdivision"], parcel["section"]) == ("PALMILLA BEACH PUD", "7"), parcel

    lot_plat = {"subdivision_name": "PALMILLA BEACH", "section": "", "lot": "14C", "block": "3"}
    assert plat_matches_parcel(parcel, lot_plat), "its own lot and block must match"
    # Block is required: block numbering restarts per subdivision, so lot 14C of
    # block 9 is a different parcel entirely.
    assert not plat_matches_parcel(parcel, {**lot_plat, "block": "9"})
    assert not plat_matches_parcel(parcel, {**lot_plat, "block": ""})
    # A PLAIN NUMERIC LOT IS REFUSED, list or not. Block numbering restarts inside
    # each UNIT here ("UNIT 7 BLK 2 LOT 9", "UNIT 1B BLK 6 LOT 31A1"), so an index row
    # saying "Lot: 9 Block: 2" with no unit of its own matches lot 9 block 2 in EVERY
    # unit -- and a lot-specific match outranks the phase plat, so the wrong date
    # would win. Only a replat-shaped designation (one carrying a letter) is trusted,
    # which is what every single-lot replat in this corpus actually looks like.
    assert not plat_matches_parcel(
        parse_subdivision_and_section("PALMILLA BEACH BLK 1 LOT 5"),
        {"subdivision_name": "PALMILLA BEACH", "section": "", "lot": "1,2,3,4,5", "block": "1"})
    assert not plat_matches_parcel(
        parse_subdivision_and_section("PALMILLA BEACH UNIT 7 BLK 2 LOT 9"),
        {"subdivision_name": "PALMILLA BEACH", "section": "", "lot": "9", "block": "2"})
    # But a letter-bearing replat designation still matches its own parcel.
    assert plat_matches_parcel(
        parse_subdivision_and_section("PALMILLA BEACH UNIT 1B BLK 6 LOT 31A1"),
        {"subdivision_name": "PALMILLA BEACH", "section": "", "lot": "31A1", "block": "6"})
    # "ET AL" and a lot RANGE state no single lot, so neither is matched to one.
    for indeterminate in ("5A ET AL", "101A-601A"):
        assert not plat_matches_parcel(parcel, {**lot_plat, "lot": indeterminate}), indeterminate
    # "N/A" is the vendor saying it has no lot, not a lot named N/A.
    assert parse_lot_block("Lot: N/A Block: N/A") == {
        "lot": None, "block": None, "lots": [], "indeterminate": False}
    print("PASS: a single-lot replat matches its own lot+block; a lot range, an "
          "'ET AL' and an 'N/A' are all refused")


def test_the_more_specific_filing_wins() -> None:
    """A parcel can match both the plat that created its PHASE and a later replat
    naming its own LOT. Both are true; only one says when THIS parcel came into
    existence. Confirmed real on Nueces parcel 528941, "PALMILLA BEACH UNIT 1B BLK 6
    LOT 31A1": unit 1B was platted 2014-04-02, and lot 31A1 of block 6 was replatted
    2018-12-11 -- and "31A1" is itself a replat designation."""
    parcel = parse_subdivision_and_section("PALMILLA BEACH UNIT 1B BLK 6 LOT 31A1")
    phase = {"plat_id": 1, "subdivision_name": "PALMILLA BEACH", "section": "1B",
             "lot": "", "block": ""}
    lot_replat = {"plat_id": 2, "subdivision_name": "PALMILLA BEACH", "section": "",
                  "lot": "31A1", "block": "6"}
    assert plat_matches_parcel(parcel, phase) and plat_matches_parcel(parcel, lot_replat)

    with get_session() as session:
        row = session.execute(text("""
            SELECT p.plat_id, pl.lot, pl.recording_date FROM parcel p
              JOIN plat pl ON pl.plat_id = p.plat_id
             WHERE p.county_fips = '48355' AND p.apn = '528941'
        """)).fetchone()
    assert row is not None and row.lot == "31A1", (
        f"parcel 528941 must be dated by the filing naming its own lot, got {row}")
    assert str(row.recording_date) == "2018-12-11", row
    print(f"PASS: a lot-specific replat outranks the phase plat -- parcel 528941 dated "
          f"{row.recording_date} by the filing that names lot {row.lot}, not by its "
          f"phase's 2014 plat")


def test_a_condominium_unit_is_not_a_phase() -> None:
    """UNIT means two different things. In Nueces' PALMILLA BEACH it is the phase --
    the index files "PALMILLA BEACH P U D Unit: 6A" -- but in a CONDOMINIUM it is the
    individual unit being owned, and one declaration created all of them. Reading
    condo units as phases turned 34 of them into 34 pointless plat searches."""
    for legal, name in [
        ("SEAGATE CONDOMINIUMS UNIT 305", "SEAGATE CONDOMINIUMS"),
        ("PALMILLA BEACH CASITA CONDOMINIUMS UNIT 6", "PALMILLA BEACH CASITA CONDOMINIUMS"),
        ("THE HIDEAWAY AT SUNFLOWER BEACH CONDOMINIUMS UNIT 14 PLUS 4.16 % INT IN COM AREA",
         "HIDEAWAY AT SUNFLOWER BEACH CONDOMINIUMS"),
    ]:
        got = parse_subdivision_and_section(legal)
        assert got["section"] is None, f"a condo unit is not a section: {got}"
        assert got["subdivision"] == name, got
        assert got["plattable"], got

    # But a phase written as UNIT still reads as one, colon and all -- the vendor
    # titles the plat "Unit: 6A" and requiring whitespace read no section, so every
    # unit-specific search found its plat and then discarded it.
    for legal, section in [("PALMILLA BEACH P U D Unit: 6A", "6A"),
                           ("PALMILLA BEACH UNIT 1B", "1B"),
                           ("PALMILLA BEACH P.U.D. UNIT 7 BLK 2 LOT 9", "7")]:
        assert parse_subdivision_and_section(legal)["section"] == section, legal
    # And the designation itself is not part of the name.
    assert (_comparison_tokens("PALMILLA BEACH P.U.D.") == _comparison_tokens("PALMILLA BEACH PUD")
            == _comparison_tokens("PALMILLA BEACH P U D") == _comparison_tokens("PALMILLA BEACH"))
    print("PASS: a condominium's UNIT is dropped as a unit, a subdivision's UNIT reads "
          "as its phase, and P.U.D./PUD/P U D are one place")


def test_dry_run_is_the_default_and_reports_what_it_would_do() -> None:
    """A bulk mutation must be inspectable before it runs. This is the guard
    against repeating the unreviewed run that caused the overwrite."""
    import inspect

    sig = inspect.signature(link_parcels_to_plats)
    assert sig.parameters["dry_run"].default is True, "dry_run must default to True"

    with get_session() as session:
        d = link_parcels_to_plats(session)
        assert d["dry_run"] is True
        assert "would_link" in d and "linked" not in d, d.keys()
        assert d["examined"] > 4000, d["examined"]
        # Every proposed change carries both ends, so an overwrite is visible.
        for c in d["changes"]:
            assert {"from_plat_id", "to_plat_id", "apn", "plat"} <= set(c), c
        assert d["overwrites"] == len([c for c in d["changes"] if c["from_plat_id"]])
    print(f"PASS: dry run is the default; {d['would_link']} proposed, "
          f"{d['overwrites']} of them overwrites, {d['already_linked']:,} already linked")


def test_reported_count_equals_rows_actually_written() -> None:
    """`linked` was incremented twice per write and reported 52 for 26 real
    links. A bulk-link report whose number disagrees with the database is how the
    earlier damage went unnoticed, so the count now derives from the change list
    and is checked against the table itself."""
    with get_session() as session:
        before = session.execute(text(
            "SELECT count(*) FROM parcel WHERE plat_id IS NOT NULL")).scalar()

        # Detach one linked parcel inside a transaction we roll back, so there is
        # real work to do and the count has something to be wrong about. The victim
        # must be one this matcher can re-derive on its own -- 107 linked parcels
        # are plat reserves it deliberately declines to link, so picking the first
        # row would test nothing and fail for the wrong reason.
        victim = next(
            (r for r in session.execute(text("""
                SELECT p.county_fips, p.apn, p.plat_id, p.recited_legal_description AS legal
                  FROM parcel p WHERE p.plat_id IS NOT NULL
                   AND p.recited_legal_description IS NOT NULL LIMIT 200
             """)).fetchall()
             if parse_subdivision_and_section(r.legal)["plattable"]), None)
        assert victim is not None, "no re-derivable linked parcel to test with"
        session.execute(text("UPDATE parcel SET plat_id = NULL "
                             "WHERE county_fips = :cf AND apn = :apn"),
                        {"cf": victim.county_fips, "apn": victim.apn})

        applied = link_parcels_to_plats(session, county_fips=victim.county_fips,
                                        dry_run=False)
        after = session.execute(text(
            "SELECT count(*) FROM parcel WHERE plat_id IS NOT NULL")).scalar()
        assert applied["linked"] == len(applied["changes"]), applied["linked"]
        assert applied["linked"] >= 1, "the detached parcel should have been relinked"
        assert after - (before - 1) == applied["linked"], (
            f"reported {applied['linked']} but the table moved "
            f"{after - (before - 1)}")
        restored = session.execute(text(
            "SELECT plat_id FROM parcel WHERE county_fips = :cf AND apn = :apn"),
            {"cf": victim.county_fips, "apn": victim.apn}).scalar()
        assert restored == victim.plat_id, (restored, victim.plat_id)
        session.rollback()

    # And the rollback really put it back.
    with get_session() as session:
        now = session.execute(text(
            "SELECT count(*) FROM parcel WHERE plat_id IS NOT NULL")).scalar()
        assert now == before, (now, before)
    print(f"PASS: reported {applied['linked']} link(s) and the table moved by exactly "
          f"that; parcel {victim.apn} relinked to its own plat {victim.plat_id}")


def test_live_state_is_consistent_and_idempotent() -> None:
    """Two invariants over the real data: nothing has a plat without a formation
    date derived from it, and re-running proposes nothing."""
    with get_session() as session:
        linked, formed = session.execute(text("""
            SELECT count(plat_id), count(formed_date) FROM parcel
        """)).fetchone()
        orphan = session.execute(text("""
            SELECT count(*) FROM parcel WHERE plat_id IS NOT NULL AND formed_date IS NULL
        """)).scalar()
        disagree = session.execute(text("""
            SELECT count(*) FROM parcel p JOIN plat pl ON pl.plat_id = p.plat_id
             WHERE p.formed_by_instrument IS NOT NULL
               AND p.formed_by_instrument <> pl.recording_instrument
        """)).scalar()
        assert orphan == 0, f"{orphan} parcels carry a plat but no formation date"
        assert disagree == 0, f"{disagree} parcels cite a different plat than they link to"
        assert link_parcels_to_plats(session)["would_link"] == 0, "not idempotent"
    print(f"PASS: {linked:,} parcels linked and {formed:,} formed, 0 orphans, "
          f"0 citation disagreements, re-run proposes nothing")


def test_worklist_collapses_spellings_and_excludes_the_unplattable() -> None:
    with get_session() as session:
        work = plats_needed(session, min_parcels=10)
        assert work, "the worklist should not be empty -- 50 subdivisions lack plats"
        assert work == sorted(work, key=lambda e: -e["parcels"]), "not ranked by size"
        for e in work:
            assert e["parcels"] >= 10 and e["search_name"], e
            assert all(e["search_name"] in s or s in e["search_name"]
                       or e["search_name"].split()[0] in s for s in e["spellings"]), e
        multi = [e for e in work if len(e["spellings"]) > 1]
        assert multi, "at least one subdivision must show collapsed spelling variants"
    print(f"PASS: worklist has {len(work)} subdivision(s) >=10 parcels covering "
          f"{sum(e['parcels'] for e in work):,} parcels; {len(multi)} span multiple "
          f"spellings (largest: {work[0]['search_name']}, {work[0]['parcels']})")


if __name__ == "__main__":
    test_reserve_in_a_subdivision_name_is_still_a_lot()
    test_abstract_is_detected_before_the_account_code_is_stripped()
    test_names_must_match_exactly_not_as_a_subset()
    test_section_06b_is_read_and_canonicalised()
    test_worded_phases_are_read()
    test_collapsed_search_name_and_the_directional_filter()
    test_a_plat_that_created_one_lot_is_matched_by_that_lot()
    test_a_condominium_unit_is_not_a_phase()
    test_the_more_specific_filing_wins()
    test_matcher_agrees_with_the_independent_subdivision_plat_path()
    test_dry_run_is_the_default_and_reports_what_it_would_do()
    test_reported_count_equals_rows_actually_written()
    test_live_state_is_consistent_and_idempotent()
    test_worklist_collapses_spellings_and_excludes_the_unplattable()
    print("\nall plat-link tests passed")
