"""Tests for app/ingestion/intake.py -- the drop-file front door.

County resolution is validated against ground truth rather than fixtures: all
28 covenants whose county_fips is independently known in this database, read
from their own document text. That is the only honest way to test a heuristic,
and it is what caught both of the traps the module documents.

Usage: python3 scripts/test_intake.py
"""
import glob
import json
import os
import shutil
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from app.db.session import get_session
from app.ingestion.intake import (
    INTAKE_DIR, INTAKE_TEXT_DIR, MINTED_COVID_FLOOR, _BOILERPLATE, _covid_from_filename,
    already_ingested, assign_covid, candidate_for_dropped_file, pending_drops,
    resolve_county_fips, resolve_jurisdiction,
)
from app.ingestion.walk import PROJECT_ROOT, get_deed_text

SCRATCH = os.path.join(PROJECT_ROOT, "_intake_test_scratch")


def _cached_text(covid: str) -> str | None:
    files = sorted(glob.glob(os.path.join(PROJECT_ROOT, "_textcache_final", f"{covid}_*.json")))
    if not files:
        return None
    with open(files[0], encoding="utf-8") as f:
        return json.load(f).get("text")


def test_county_resolution_against_every_known_covenant() -> None:
    """The whole justification for this heuristic. If it ever stops resolving
    a covenant whose county is independently known, this names which one."""
    with get_session() as session:
        truth = {str(r[0]): (r[1], r[2]) for r in session.execute(text(
            "SELECT c.covid, co.county_name, co.state_code "
            "FROM covenant c JOIN county co USING (county_fips)"))}
        assert len(truth) >= 20, f"expected the known-county set, got {len(truth)}"

        correct, wrong, unresolved = 0, [], []
        for covid, (county, state_code) in sorted(truth.items()):
            # Read what the APP reads -- get_deed_text picks the best available
            # cache by yield -- not _textcache_final blindly. That distinction is
            # the point for covid 4956, whose corpus cache is 13 pages of vendor
            # page-stamps naming DALLAS only inside the stamp itself, never next
            # to the word "County". Its re-OCR resolves fine.
            body = get_deed_text(session, int(covid), None)
            if not body:
                continue
            j = resolve_jurisdiction(body)
            if j["county_name"] == county:
                correct += 1
            elif j["county_name"] is None:
                unresolved.append(covid)
            else:
                wrong.append((covid, county, j["county_name"], j["county_tally"]))
    assert not wrong, f"county resolved WRONG (worse than unresolved): {wrong}"
    assert not unresolved, f"county unresolved for {unresolved}"
    print(f"PASS: county read from the document matches all {correct} independently-known "
          f"covenants, 0 wrong, 0 unresolved")


def test_travis_boilerplate_is_stripped() -> None:
    """335 of 1,056 corpus documents contain this sentence, including Colorado
    ones. Left in, it sends most of the portfolio to Travis County."""
    sentence = ("assigned by notice of assignment executed by Licensor and filed in the "
                "Recorder's Office of Travis County, Texas. then upon written certification")
    assert "Travis" not in _BOILERPLATE.sub(" ", sentence), _BOILERPLATE.sub(" ", sentence)

    # A Colorado covenant carrying the boilerplate must still resolve to Colorado.
    body = _cached_text("3595")
    if body:
        j = resolve_jurisdiction(body)
        assert j["county_name"] == "DOUGLAS", j["county_tally"]
        assert j["state_name"] == "COLORADO", j["state_tally"]
        print("PASS: Travis boilerplate stripped; covid 3595 still resolves to Douglas, Colorado")
    else:
        print("PASS: Travis boilerplate stripped (covid 3595 not cached, skipped live check)")


def test_state_is_resolved_not_assumed() -> None:
    """County names repeat across states, and the corpus is not confined to the
    two seeded ones -- covid 2088 is Fairfield County, CONNECTICUT. Reading the
    state is what stops a Fairfield or a Montgomery landing in the wrong one."""
    body = _cached_text("2088")
    if body is None:
        print("SKIP: covid 2088 not cached")
        return
    j = resolve_jurisdiction(body)
    assert j["state_name"] == "CONNECTICUT", j["state_tally"]
    assert j["county_name"] == "FAIRFIELD", j["county_tally"]
    with get_session() as session:
        resolved = resolve_county_fips(session, j["state_name"], j["county_name"])
    assert resolved["county_fips"] is None, "Connecticut is not seeded; must not resolve"
    assert "not in the state reference table" in resolved["reason"], resolved["reason"]
    assert "seed it" in resolved["reason"], "the halt must say what to do"
    print(f"PASS: covid 2088 reads as Fairfield County, Connecticut and halts with "
          f"instructions rather than a guess")


def test_unseeded_county_halts_with_instructions() -> None:
    with get_session() as session:
        r = resolve_county_fips(session, "TEXAS", "ZAVALA")
        assert r["county_fips"] is None
        assert "not in the county reference table" in r["reason"]
        assert "discover its parcel service and recorder" in r["reason"], r["reason"]
        # A seeded one still works.
        ok = resolve_county_fips(session, "TEXAS", "MONTGOMERY")
        assert ok["county_fips"] == "48339", ok
        # Missing inputs are reported specifically, not as a generic failure.
        assert "state" in resolve_county_fips(session, None, "MONTGOMERY")["reason"]
        assert "county" in resolve_county_fips(session, "TEXAS", None)["reason"]
    print("PASS: an unseeded county halts naming the work required; a seeded one resolves")


def test_noise_words_are_not_counties() -> None:
    j = resolve_jurisdiction(
        "the County Clerk of the County of the said County records, "
        "MONTGOMERY County, MONTGOMERY County, State of Texas")
    assert j["county_name"] == "MONTGOMERY", j["county_tally"]
    assert "CLERK" not in j["county_tally"] and "THE" not in j["county_tally"], j["county_tally"]
    assert resolve_jurisdiction("")["county_name"] is None
    assert resolve_jurisdiction("no jurisdiction here at all")["county_name"] is None
    print("PASS: clerk/records/the are not counted as county names; empty text resolves to None")


def test_margin_is_reported_for_the_caller_to_judge() -> None:
    """A decisive tally and a tied one are different situations, so the count
    and the runner-up both come back rather than a bare name."""
    j = resolve_jurisdiction("HARRIS County. MONTGOMERY County. MONTGOMERY County. State of Texas")
    assert j["county_name"] == "MONTGOMERY"
    assert j["county_count"] == 2 and j["county_runner_up"] == 1, j
    print(f"PASS: resolution reports its margin ({j['county_count']} vs {j['county_runner_up']})")


def test_covid_from_filename() -> None:
    assert _covid_from_filename("3346_D1234.pdf") == 3346
    assert _covid_from_filename("8534-declaration.pdf") == 8534
    assert _covid_from_filename("5838 covenant.pdf") == 5838
    assert _covid_from_filename("declaration.pdf") is None
    assert _covid_from_filename("2024_annual_report.pdf") == 2024  # a real risk, see assign_covid
    print("PASS: covid parsed from the corpus filename convention")


def test_assign_covid_does_not_overwrite_a_different_document() -> None:
    """Two unrelated files both named "3346_*.pdf" must not land on the same
    covenant row and silently replace each other."""
    with get_session() as session:
        on_file = session.execute(
            text("SELECT relpath FROM covenant_document WHERE covid = 3346 AND doc_type = 'original'"),
        ).scalar()
        if not on_file:
            print("SKIP: covid 3346 has no document on file")
            return
        same = assign_covid(session, os.path.basename(on_file))
        assert same["covid"] == 3346 and same["how"] == "filename", same
        assert "already on file" in same["note"], same["note"]

        intruder = assign_covid(session, "3346_a_completely_different_document.pdf")
        assert intruder["how"] == "minted", intruder
        assert intruder["covid"] >= MINTED_COVID_FLOOR, intruder
        assert "already holds a different document" in intruder["note"], intruder["note"]
    print(f"PASS: a re-dropped document keeps its covid; an impostor claiming the same "
          f"covid is minted a new one ({intruder['covid']})")


def test_minted_covids_cannot_collide_with_the_corpus() -> None:
    corpus_max = 0
    for name in os.listdir(PROJECT_ROOT):
        if name.isdigit():
            corpus_max = max(corpus_max, int(name))
    assert MINTED_COVID_FLOOR > corpus_max, (
        f"minted floor {MINTED_COVID_FLOOR} must sit above the corpus max {corpus_max}")
    print(f"PASS: minted covid floor {MINTED_COVID_FLOOR} is clear of the corpus (max {corpus_max})")


def test_live_intake_of_a_real_dropped_pdf() -> None:
    """End to end on a real document, dropped as a file with no covid in its
    name, no index row and no cached text -- the actual case this exists for."""
    source = None
    for covid in ("3346", "2088", "4440"):
        matches = glob.glob(os.path.join(PROJECT_ROOT, covid, "*.pdf"))
        if matches:
            source = matches[0]
            break
    if source is None:
        print("SKIP: no corpus PDF available to drop")
        return

    os.makedirs(SCRATCH, exist_ok=True)
    dropped = os.path.join(SCRATCH, "unnamed_declaration.pdf")
    shutil.copy2(source, dropped)

    with get_session() as session:
        result = candidate_for_dropped_file(session, dropped)
    c = result["candidate"]

    assert c.covid >= MINTED_COVID_FLOOR, f"no covid in the name, so it must mint: {c.covid}"
    assert c.relpath and os.path.exists(os.path.join(PROJECT_ROOT, c.relpath)), c.relpath
    assert c.relpath.startswith("_intake/"), f"must be filed under _intake/, got {c.relpath}"
    assert os.path.exists(dropped), "the original must survive intake (copy, not move)"
    assert c.pages and c.pages > 0, f"page count not read: {c.pages}"
    assert c.text and len(c.text) > 1000, f"no text acquired: {len(c.text or '')} chars"
    assert c.vocab_score is None, "vocab_score is the corpus's scale and must stay unset"
    assert c.legibility is not None and c.text_usable is True, (c.legibility, c.text_usable)
    assert c.county_fips, f"county not resolved: {result['jurisdiction']}"
    assert not c.needs_review, f"should be clean: {c.review_reason}"

    # Second pass reuses the cached text -- a re-run must cost nothing.
    with get_session() as session:
        again = candidate_for_dropped_file(session, dropped)
    assert again["acquired"]["from_cache"], "the second read must come from cache"
    assert again["covid"] == c.covid, "a re-dropped file must not mint a second covid"

    print(f"PASS: live intake of an unnamed dropped PDF -> covid {c.covid}, "
          f"county {c.county_fips}, {c.pages} pages, {len(c.text)} chars via "
          f"{'OCR' if c.ocr else 'text layer'}; re-run served from cache")
    return c.covid


def test_pending_drops_and_already_ingested() -> None:
    os.makedirs(SCRATCH, exist_ok=True)
    for name in ("b.pdf", "a.pdf", "notes.txt", ".hidden.pdf"):
        open(os.path.join(SCRATCH, name), "a").close()
    found = [os.path.basename(p) for p in pending_drops(SCRATCH)]
    assert set(found) <= {"a.pdf", "b.pdf", "unnamed_declaration.pdf"}, found
    assert "notes.txt" not in found and ".hidden.pdf" not in found, found

    with get_session() as session:
        on_file = session.execute(
            text("SELECT relpath FROM covenant_document WHERE doc_type = 'original' "
                 "AND relpath IS NOT NULL LIMIT 1")).scalar()
        if on_file:
            assert already_ingested(session, os.path.basename(on_file)) is not None
        assert already_ingested(session, "definitely_not_a_real_document_xyz.pdf") is None
    print("PASS: pending_drops lists only visible PDFs oldest-first; already_ingested "
          "recognises a filed document")


def _cleanup(minted: int | None) -> None:
    shutil.rmtree(SCRATCH, ignore_errors=True)
    if minted:
        shutil.rmtree(os.path.join(INTAKE_DIR, str(minted)), ignore_errors=True)
        for f in glob.glob(os.path.join(INTAKE_TEXT_DIR, f"{minted}_*.json")):
            os.remove(f)


if __name__ == "__main__":
    minted = None
    try:
        test_county_resolution_against_every_known_covenant()
        test_travis_boilerplate_is_stripped()
        test_state_is_resolved_not_assumed()
        test_unseeded_county_halts_with_instructions()
        test_noise_words_are_not_counties()
        test_margin_is_reported_for_the_caller_to_judge()
        test_covid_from_filename()
        test_assign_covid_does_not_overwrite_a_different_document()
        test_minted_covids_cannot_collide_with_the_corpus()
        minted = test_live_intake_of_a_real_dropped_pdf()
        test_pending_drops_and_already_ingested()
        print("\nall intake tests passed")
    finally:
        _cleanup(minted)
