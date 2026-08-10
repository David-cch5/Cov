"""Smoke test for scripts/ingest_probe.py's re-run safety.

Real incident this guards against: re-running ingest_one() on an
already-progressed covenant (GIS-classified, chain-walked) silently wiped
a prior manual "RE-VERIFIED" acreage-reconciliation note on covid 2497 --
the first (unconditional, unmerged) upsert_covenant call at the top of the
function overwrote review_reason/status before the later, merge-aware
logic ever got a chance to see the real prior state. Confirmed fixed live
(re-ran ingestion on covid 2497 twice after restoring its note; it
survived both times) -- this test covers _merge_ingestion_note's own pure
logic directly, since that's the part cheap and deterministic enough to
regression-test without a live LLM call.

Usage: python3 scripts/test_ingest_probe.py
"""
import sys

sys.path.insert(0, ".")

from app.db.session import get_session
from app.ingestion.walk import CovenantCandidate, _KNOWN_COUNTY_NAME_TYPOS
from scripts.ingest_probe import _merge_ingestion_note, ingest_one


def test_merge_preserves_unrelated_existing_note() -> None:
    """The exact shape of the real incident: an existing note from a
    DIFFERENT stage (untagged, e.g. a manual investigation) must survive
    completely untouched when ingestion has nothing new to say."""
    existing = "RE-VERIFIED (2026-07-24): confirmed the recorded instrument is complete..."
    merged = _merge_ingestion_note(existing, None)
    assert merged == existing, merged
    print("PASS: _merge_ingestion_note -> an unrelated existing note is untouched when ingestion is clean")


def test_merge_appends_ingestion_note() -> None:
    existing = "RE-VERIFIED (2026-07-24): confirmed the recorded instrument is complete..."
    merged = _merge_ingestion_note(existing, "low extraction confidence (0.6)")
    assert existing in merged, merged
    assert "INGESTION-STAGE" in merged and "low extraction confidence (0.6)" in merged, merged
    print("PASS: _merge_ingestion_note -> ingestion's own note is appended, not overwriting the rest")


def test_merge_replaces_only_its_own_prior_tag() -> None:
    """Re-running ingestion a second time must replace ITS OWN previous
    tagged note (not duplicate it), while still leaving an unrelated note
    from another stage alone."""
    first_pass = _merge_ingestion_note("RE-VERIFIED (2026-07-24): some other note.", "low extraction confidence (0.6)")
    second_pass = _merge_ingestion_note(first_pass, "low extraction confidence (0.9)")
    assert second_pass.count("INGESTION-STAGE") == 1, second_pass
    assert "0.6" not in second_pass and "0.9" in second_pass, second_pass
    assert "RE-VERIFIED (2026-07-24): some other note." in second_pass, second_pass
    print("PASS: _merge_ingestion_note -> a second run replaces its own prior tag, not the other stage's note")


def test_merge_clears_when_ingestion_now_clean() -> None:
    """If ingestion previously flagged something and now finds nothing,
    its own tagged section disappears -- but an unrelated note stays."""
    first_pass = _merge_ingestion_note("RE-VERIFIED (2026-07-24): some other note.", "low extraction confidence (0.6)")
    cleared = _merge_ingestion_note(first_pass, None)
    assert "INGESTION-STAGE" not in cleared, cleared
    assert cleared == "RE-VERIFIED (2026-07-24): some other note.", cleared
    print("PASS: _merge_ingestion_note -> a resolved ingestion concern clears, unrelated note remains")


def test_known_county_name_typo_correction() -> None:
    """Confirmed real: _pilot/covid_index.csv's own county column for covid
    4123 reads "DOUGLAS OQ." instead of "DOUGLAS" (the county table's
    actual name for Colorado's Douglas County) -- an OCR/data-entry
    artifact in a read-only data location, corrected here rather than by
    editing the source index file itself."""
    assert _KNOWN_COUNTY_NAME_TYPOS[("COLORADO", "DOUGLAS OQ.")] == "DOUGLAS"
    print("PASS: _KNOWN_COUNTY_NAME_TYPOS -> covid 4123's real county-name typo is corrected")


def test_ingest_one_raises_clear_error_on_unresolved_county() -> None:
    """Confirmed real: a candidate whose county can't be resolved at all
    (county_fips=None) used to crash ingest_one() with a raw
    psycopg2.errors.NotNullViolation (covenant.county_fips is NOT NULL)
    instead of a clear, review-queue-style message -- run()'s own
    failed-list reporting caught the exception either way, but the
    traceback was uninformative. Uses a covid guaranteed not to already
    exist (so `existing` is None, the exact condition that used to crash);
    nothing is ever written, so there's nothing to roll back -- get_session's
    own exception handling closes the session cleanly either way."""
    candidate = CovenantCandidate(
        covid=999999999, relpath=None, state_name="COLORADO", county_name=None,
        county_fips=None, template_version_id=None, template_confidence=None,
        text=None, pages=None, ocr=None, vocab_score=None,
        needs_review=True, review_reason="county not resolved (COLORADO/NOWHERE COUNTY)",
    )
    try:
        with get_session() as session:
            ingest_one(session, candidate)
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "county not resolved" in str(exc), exc
    assert raised, "expected a clear RuntimeError, not a silent success or a raw DB exception"
    print("PASS: ingest_one -> a genuinely unresolvable county raises a clear error instead of "
          "crashing on a raw NOT NULL violation")


def test_best_cache_file_prefers_the_fuller_document() -> None:
    """19 of 1,056 cached covenants have more than one cache file, and this
    used to be text_files[0] -- filesystem order. covid 4497's two files are a
    real 14-page document (54,005 chars) and a 26-page one holding 4,691 chars
    (180/page, no usable body). Order must not decide which one the whole
    pipeline reads."""
    import os

    from app.ingestion.walk import TEXTCACHE, _best_cache_file

    names = [f for f in os.listdir(TEXTCACHE) if f.startswith("4497_")]
    if len(names) < 2:
        print("SKIP: covid 4497 no longer has duplicate cache files")
        return
    # Both orderings must give the same answer -- that is the whole point.
    for ordering in (sorted(names), sorted(names, reverse=True)):
        best = _best_cache_file(TEXTCACHE, ordering)
        assert best.get("relpath", "").endswith("D2045.pdf"), (
            f"expected the fuller D2045 document, got {best.get('relpath')!r}")
    assert _best_cache_file(TEXTCACHE, []) == {}, "no files must not crash"
    print("PASS: _best_cache_file picks the fuller document regardless of listing order")


if __name__ == "__main__":
    test_merge_preserves_unrelated_existing_note()
    test_merge_appends_ingestion_note()
    test_merge_replaces_only_its_own_prior_tag()
    test_merge_clears_when_ingestion_now_clean()
    test_known_county_name_typo_correction()
    test_ingest_one_raises_clear_error_on_unresolved_county()
    test_best_cache_file_prefers_the_fuller_document()
    print("\nall ingest_probe smoke tests passed")
