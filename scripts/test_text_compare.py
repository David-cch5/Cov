"""Tests for the three-way reading comparison.

Every assertion here is a rule that already cost time when it was broken:

  blank COV.IDp belongs to the covid ABOVE -- read literally, the sheet loses
    every tract after the first of each covenant
  ROW ORDER IS NOT TRACT ORDER -- content identifies a tract, position does not
  an absent string is evidence about our COPY -- covid 4981's "55.73" is in the
    OCR as "55 73"
  closure is reported as a FRACTION, not the 1:N surveyors write -- reading it
    the wrong way round made every tract in the portfolio look unanchorable

Usage: python3 scripts/test_text_compare.py
"""
import sys

sys.path.insert(0, ".")

from app.ingestion.exha_sheet import SheetRow, looks_like_tract, read_sheet
from app.ingestion.text_compare import compare_covenant, mentions_acreage, tract_facts


def test_a_blank_covid_belongs_to_the_row_above() -> None:
    rows = read_sheet()
    assert len(rows) > 1500, len(rows)
    assert all(r.covid for r in rows), "every row must resolve to a covenant"
    # covid 4981 writes its id once and follows it with three rows.
    mine = [r for r in rows if r.covid == 4981]
    assert len(mine) == 3, [r.row_number for r in mine]
    assert [r.row_number for r in mine] == sorted(r.row_number for r in mine)
    print(f"PASS: {len(rows):,} rows across {len({r.covid for r in rows}):,} covenants, "
          f"every one attributed; covid 4981 keeps all 3 of its rows")


def test_a_tract_is_identified_by_content_not_by_row_number() -> None:
    """The sheet's order is not the document's. Two readings of one tract must
    collide on content_key even when their row numbers differ, and two genuinely
    different tracts must not."""
    young = "BEING 11.878 acres in the Andrew S. Young Survey, Abstract No. 1037"
    young_again = "containing 11.878 acres, Andrew S Young Survey, Abstract 1037, McKinney"
    cox = "BEING 16.865 acres in the John W. Cox Survey, Abstract No 160"
    assert tract_facts(young).content_key == tract_facts(young_again).content_key
    assert tract_facts(young).content_key != tract_facts(cox).content_key
    # and the key is built from the land, never from where the row sat
    assert tract_facts(young).content_key == (11.9, "1037"), tract_facts(young).content_key
    print("PASS: two spellings of one tract share a content key; a different tract does not")


def test_an_absent_string_is_evidence_about_our_copy() -> None:
    """Covid 4981's 55.73 survives OCR as '55 73'. Reporting it missing would be
    a statement about a lost decimal point dressed as a finding about land."""
    assert mentions_acreage("of 55 73 acres of land", 55.73)
    assert mentions_acreage("containing 55.73 acres", 55.73)
    assert mentions_acreage("55,73 acres", 55.73)
    assert not mentions_acreage("containing 12.00 acres", 55.73)
    assert not mentions_acreage("", 55.73)
    print("PASS: a lost decimal point still matches; a genuinely absent acreage does not")


def test_closure_is_reported_as_surveyors_write_it() -> None:
    """walk_traverse returns error/perimeter. Covid 4981's Young tract is
    5.35e-06, which IS 1:186,912 -- reading the fraction as the ratio reported
    a survey-grade traverse as a closure of zero."""
    from app.ingestion.corrected_text import load_correction

    facts = tract_facts(load_correction(4981, "tract_young_survey_11878")["text"])
    assert facts.course_count == 14, facts.course_count
    assert facts.closure_denominator > 100_000, facts.closure_denominator
    assert facts.closure_ratio < 1e-4, facts.closure_ratio
    assert facts.area_agrees is True, (facts.area_acres, facts.stated_acres)
    print(f"PASS: 1:{facts.closure_denominator:,.0f} from a raw ratio of "
          f"{facts.closure_ratio:.2e}, area agrees with the stated 11.878 ac")


def test_a_declaration_row_is_not_mistaken_for_a_tract() -> None:
    """Covid 4981's third sheet row is the Phase IV declaration, not a tract:
    it recites an acreage and a survey but its traverse closes at 1:7 and its
    area misses by a third. It must not reach the anchorable list."""
    got = compare_covenant(4981)
    rows = {e["sheet_row"]: e for e in got["sheet_tracts"]}
    assert len(rows) == 3, sorted(rows)
    assert rows[1667]["declaration"] is True, rows[1667]
    assert rows[1667]["facts"].area_agrees is False
    anchorable = {e["sheet_row"] for e in got["anchorable"]}
    assert anchorable == {1665, 1666}, anchorable
    print(f"PASS: covid 4981's two real tracts are anchorable (1:"
          f"{rows[1665]['facts'].closure_denominator:,.0f} and 1:"
          f"{rows[1666]['facts'].closure_denominator:,.0f}); the declaration row is not")


def test_a_reference_row_is_not_a_tract() -> None:
    assert not looks_like_tract("2766013")
    assert not looks_like_tract("CAD Account 1944864")
    assert not looks_like_tract("")
    assert looks_like_tract("BEING a tract of land in the John W. Cox Survey, Abstract 160, "
                            "containing 16.865 acres of land")
    print("PASS: a bare account number is a reference; a description is a tract")


def test_the_parser_not_the_text_is_the_portfolio_bottleneck() -> None:
    """The finding this whole comparison exists to produce, pinned so it cannot
    regress unnoticed: most tracts that close badly do so because the parser
    read fewer courses than the description has THENCE calls, not because the
    reading is wrong. Anchoring cannot outrun that."""
    import re

    thence_re = re.compile(r"\bTHENCE\b", re.IGNORECASE)
    dropped = walkable = 0
    for row in read_sheet():
        if not row.is_tract:
            continue
        facts = tract_facts(row.text)
        if facts.course_count < 3:
            continue
        walkable += 1
        if thence_re.findall(row.text).__len__() > facts.course_count:
            dropped += 1
    assert walkable > 200, walkable
    assert dropped > 100, f"only {dropped} of {walkable} drop calls -- has the parser improved?"
    print(f"PASS: {dropped} of {walkable} walkable tract descriptions read fewer courses "
          f"than they have THENCE calls -- the parser is the bottleneck, not the readings")


if __name__ == "__main__":
    test_a_blank_covid_belongs_to_the_row_above()
    test_a_tract_is_identified_by_content_not_by_row_number()
    test_an_absent_string_is_evidence_about_our_copy()
    test_closure_is_reported_as_surveyors_write_it()
    test_a_declaration_row_is_not_mistaken_for_a_tract()
    test_a_reference_row_is_not_a_tract()
    test_the_parser_not_the_text_is_the_portfolio_bottleneck()
    print("\nall text-comparison tests passed")
