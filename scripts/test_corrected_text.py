"""Tests for app/ingestion/corrected_text.py -- corrections kept so they are made once.

The three machine readings (_textcache*, _intake_text/, _ocr_escalated/) all
re-derive from the same damaged scan every run. Covid 4981's Exhibit A took six
separate repairs and nothing kept them, which is what this closes.

Two rules carry the risk, and both are pinned here:
  a SEGMENT correction must never be served where the whole document was asked for
  CORRECTED is not VERIFIED -- a description that parses can still be wrong

Usage: python3 scripts/test_corrected_text.py
"""
import sys

sys.path.insert(0, ".")

from app.db.session import get_session
from app.ingestion import walk
from app.ingestion.corrected_text import (
    corrected_document_text, corrections_for, load_correction, save_correction)


def test_a_segment_correction_never_replaces_the_document() -> None:
    """A corrected tract description covers ONE tract of a document that may hold
    several -- covid 4981 holds two. Returning it where the whole text was asked for
    would silently discard the other, which is a regression wearing a fix's clothes."""
    rec = load_correction(4981, "tract_young_survey_11878")
    assert rec is not None and rec["scope"] == "segment", rec
    assert corrected_document_text(4981) is None, \
        "a segment correction must not answer as the document"
    with get_session() as session:
        whole = walk.get_deed_text(session, 4981)
    assert len(whole) > 60000, len(whole)
    assert len(whole) > rec["chars"] * 5, (len(whole), rec["chars"])
    print(f"PASS: covid 4981's {rec['chars']:,}-char segment correction is on record and "
          f"get_deed_text still returns all {len(whole):,} chars")


def test_closure_picked_the_reading_not_preference() -> None:
    """Which of three witnesses is right is decided by the land, not by whose text it
    is. Covid 4981's Young Survey tract walks to 0.02 ft over 2,892 ft (1:186,912) from
    the reviewed sheet and to 115.13 ft (1:25) from the document's own OCR -- the same
    14 courses, one of them misread in the scan. The sheet won on that evidence, and
    the rejected alternative is kept so the choice can be re-examined."""
    rec = load_correction(4981, "tract_young_survey_11878")
    ev = rec["evidence"]
    assert rec["verified"] is True, "a 1:186,912 closure is verified"
    assert ev["courses_read"] == ev["thence_calls"] == 14, ev
    assert ev["closure_ratio_denominator"] > 100000, ev
    assert abs(ev["area_acres"] - ev["stated_acres"]) < 0.05, ev
    assert ev["source"] == "COV_EXHA_EXTRACT.xlsx", ev
    rejected = ev["rejected_alternative"]
    assert rejected["closure_ratio_denominator"] < 100, rejected
    assert rejected["closure_error_ft"] > ev["closure_error_ft"] * 1000, rejected
    assert rec["corrected_by"] and rec["basis"], rec
    print(f"PASS: the reviewed sheet's reading closes 1:{ev['closure_ratio_denominator']:,} "
          f"against the document OCR's 1:{rejected['closure_ratio_denominator']}, and the "
          f"loser is recorded alongside it")


def test_a_correction_must_name_its_author_and_evidence() -> None:
    """This file outranks three machine readings, so an anonymous one is refused."""
    for kwargs in ({"corrected_by": "", "basis": "x"}, {"corrected_by": "x", "basis": ""}):
        try:
            save_correction(999999, "some text", **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"a correction with {kwargs} must be refused")
    try:
        save_correction(999999, "", corrected_by="x", basis="y")
    except ValueError:
        pass
    else:
        raise AssertionError("an empty correction must be refused")
    try:
        save_correction(999999, "t", corrected_by="x", basis="y", scope="whatever")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown scope must be refused")
    print("PASS: a correction without an author, evidence, text or a known scope is refused")


def test_corrections_are_listable_per_covenant() -> None:
    found = corrections_for(4981)
    assert found and all(f["covid"] == 4981 for f in found), found
    print(f"PASS: {len(found)} correction(s) on record for covid 4981")


if __name__ == "__main__":
    test_a_segment_correction_never_replaces_the_document()
    test_closure_picked_the_reading_not_preference()
    test_a_correction_must_name_its_author_and_evidence()
    test_corrections_are_listable_per_covenant()
    print("\nall corrected-text tests passed")
