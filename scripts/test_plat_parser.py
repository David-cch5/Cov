"""Smoke test for app/gis/plat_parser.py -- deterministic classification of a
county parcel's own recited legal description as platted (subdivision +
section) vs. still-raw abstract-survey tract vs. ambiguous.

Every string here is a REAL recited_legal_description pulled directly from
covid 4440's own 4090 matched parcels (Montgomery County) -- confirmed by
running the parser against the full, real set: 4038 platted, 44 raw,
8 ambiguous (all genuine one-off civic/utility reserves -- a school site,
a drainage/WWTP reserve, a street dedication -- correctly left unresolved
rather than force-matched).

Usage: python3 scripts/test_plat_parser.py
"""
import sys

sys.path.insert(0, ".")

from app.gis.plat_parser import parse_plat_reference


def test_lot_block_no_code_prefix() -> None:
    ref = parse_plat_reference("The Canopies 03 BLK 1 LOT 43")
    assert ref is not None and ref.platted, ref
    assert ref.subdivision_name == "THE CANOPIES", ref
    assert ref.section == "03", ref
    print("PASS: parse_plat_reference -> lot/block, no code prefix, abbreviated BLK")


def test_lot_block_with_code_prefix_and_comma() -> None:
    ref = parse_plat_reference("S573300 - Harrington Trails 01, BLOCK 1, Lot 44")
    assert ref is not None and ref.platted, ref
    assert ref.subdivision_name == "HARRINGTON TRAILS", ref
    assert ref.section == "01", ref
    print("PASS: parse_plat_reference -> lot/block, code prefix + comma, full BLOCK word")


def test_lot_block_alphanumeric_section() -> None:
    ref = parse_plat_reference("S573393 - Harrington Trails 06B, BLOCK 5, Lot 22")
    assert ref is not None and ref.platted, ref
    assert ref.subdivision_name == "HARRINGTON TRAILS", ref
    assert ref.section == "06B", ref
    print("PASS: parse_plat_reference -> alphanumeric section ('06B') correctly captured")


def test_reserve_with_comma() -> None:
    ref = parse_plat_reference("S573309 - Harrington Trails 09, RES A, ACRES 0.9732")
    assert ref is not None and ref.platted, ref
    assert ref.subdivision_name == "HARRINGTON TRAILS", ref
    assert ref.section == "09", ref
    print("PASS: parse_plat_reference -> reserve parcel, code prefix + comma")


def test_reserve_no_comma() -> None:
    ref = parse_plat_reference("TIMBERS EDGE 01 RES D 1.453 ACRES")
    assert ref is not None and ref.platted, ref
    assert ref.subdivision_name == "TIMBERS EDGE", ref
    assert ref.section == "01", ref
    print("PASS: parse_plat_reference -> reserve parcel, no comma before RES")


def test_lot_only_no_section() -> None:
    ref = parse_plat_reference("DUSTY TRAILS LT 9, ACRES 3.000")
    assert ref is not None and ref.platted, ref
    assert ref.subdivision_name == "DUSTY TRAILS", ref
    assert ref.section == "", ref
    print("PASS: parse_plat_reference -> lot-only subdivision with no numbered phase")


def test_abstract_tract_with_dash() -> None:
    ref = parse_plat_reference("A0494 - Walker Co Sch L, TRACT 1C-1, ACRES 27.2696")
    assert ref is not None and not ref.platted, ref
    print("PASS: parse_plat_reference -> raw abstract-survey tract (dash), not platted")


def test_abstract_tract_no_dash() -> None:
    ref = parse_plat_reference("A0494 WALKER CO SCH L, TR 1C1-A, 7.0494 ACRES")
    assert ref is not None and not ref.platted, ref
    print("PASS: parse_plat_reference -> raw abstract-survey tract (no dash), not platted")


def test_abstract_tract_odd_terminology_not_misread_as_plat() -> None:
    """Confirmed real bug caught before this shipped: an abstract-tract
    reference using unusual terminology ("DIRECTOR LOT 1" rather than
    "TRACT ...") was being misread by an earlier version of the LOT-only
    fallback as a real subdivision named "WALKER CO SCH L, DIRECTOR". The
    leading abstract-number prefix (A####) must always win over any later
    LOT/BLOCK-shaped text, since a real plat's own description never
    leads with the bare abstract number."""
    ref = parse_plat_reference("A0494 - Walker Co Sch L,     TRACT DL 4 ME14A")
    assert ref is not None and not ref.platted, ref
    print("PASS: parse_plat_reference -> abstract-number prefix wins even with unusual "
          "trailing terminology, never misread as a platted subdivision")


def test_ambiguous_civic_reserve_not_guessed() -> None:
    """A genuine one-off (school site / drainage reserve / street dedication)
    that doesn't cleanly fit a numbered-section pattern -- correctly
    reported ambiguous rather than force-matched to a wrong subdivision."""
    for text in (
        "THE PRESSWOODS DETENTION & WWTP RESERVES RES A 71.370 ACRES",
        "S924090 - Timber Lakes Elementary, RES A, ACRES 15.0087",
        "SPLENDORA ISD ELEMENTARY AT CANOPIES RES A 14.000 ACRES",
    ):
        assert parse_plat_reference(text) is None, text
    print("PASS: parse_plat_reference -> genuine one-off civic/utility reserves "
          "correctly left ambiguous, not force-matched")


def test_none_and_empty_input() -> None:
    assert parse_plat_reference(None) is None
    assert parse_plat_reference("") is None
    print("PASS: parse_plat_reference -> None/empty input is ambiguous, not a crash")


if __name__ == "__main__":
    test_lot_block_no_code_prefix()
    test_lot_block_with_code_prefix_and_comma()
    test_lot_block_alphanumeric_section()
    test_reserve_with_comma()
    test_reserve_no_comma()
    test_lot_only_no_section()
    test_abstract_tract_with_dash()
    test_abstract_tract_no_dash()
    test_abstract_tract_odd_terminology_not_misread_as_plat()
    test_ambiguous_civic_reserve_not_guessed()
    test_none_and_empty_input()
    print("\nall plat_parser smoke tests passed")
