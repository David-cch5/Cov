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

from scripts.ingest_probe import _merge_ingestion_note


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


if __name__ == "__main__":
    test_merge_preserves_unrelated_existing_note()
    test_merge_appends_ingestion_note()
    test_merge_replaces_only_its_own_prior_tag()
    test_merge_clears_when_ingestion_now_clean()
    print("\nall ingest_probe smoke tests passed")
