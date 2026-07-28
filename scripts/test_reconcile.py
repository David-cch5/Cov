"""Smoke test for app/gis/reconcile.py -- the reconciliation check CLAUDE.md
requires before a covenant is considered done ("classified acreage must
reconcile with the covenant's stated acreage, and any unaccounted area
inside the footprint is flagged").

Runs live against real, already-classified covenants. Not rolled back --
reconcile_tract/reconcile_covenant are idempotent (an UPDATE keyed by
covid/tract_no, and covenant.review_reason's tagged-note merge pattern), so
re-running this simply reconfirms the same result.

Two of these (covid 2497, covid 4955) were ALREADY manually reconciled by a
human during earlier work on this project, before this module existed --
this module's own automated logic independently reproduces both exact
prior conclusions, not a coincidence.

Usage: python3 scripts/test_reconcile.py
"""
import sys

sys.path.insert(0, ".")

from app.db.session import get_session
from app.gis.reconcile import reconcile_covenant, reconcile_tract


def test_reconcile_tract_over_classified() -> None:
    """covid 2497 (Bexar): matched parcels total 0.534 ac against a
    deed-stated 0.452 ac -- a real, already-investigated 18% overage
    (documented in the covenant's own RE-VERIFIED note as measurement/
    digitization variance, not a wrong parcel), manually marked
    over_classified before this module existed. Confirms this module
    reproduces that exact conclusion, including the un-signed (magnitude)
    convention the pre-existing manual value already used."""
    with get_session() as session:
        result = reconcile_tract(session, covid=2497, tract_no=1)
    assert result["checked"] is True, result
    assert result["status"] == "over_classified", result
    assert result["unaccounted_acreage"] > 0, result  # magnitude, not signed
    print("PASS: reconcile_tract (covid 2497) -> reproduces the pre-existing manual "
          "over_classified conclusion")


def test_reconcile_tract_unaccounted_area() -> None:
    """covid 4955 (Collin): matched parcels total 10.622 ac against a
    deed-stated 12.023 ac -- a real, already-investigated 1.401-ac gap
    (documented as a later TxDOT right-of-way dedication the covenant's
    own trustee notice doesn't discount), manually marked unaccounted_area
    before this module existed."""
    with get_session() as session:
        result = reconcile_tract(session, covid=4955, tract_no=1)
    assert result["checked"] is True, result
    assert result["status"] == "unaccounted_area", result
    assert abs(result["unaccounted_acreage"] - 1.401) < 0.01, result
    print("PASS: reconcile_tract (covid 4955) -> reproduces the pre-existing manual "
          "unaccounted_area conclusion")


def test_reconcile_tract_no_stated_acreage_is_reconciled() -> None:
    """covid 3297: stated_acreage was deliberately nulled (the original
    extraction conflated a whole subdivision-section's plat acreage with
    this covenant's actual encumbered area) rather than guessed -- nothing
    to compare classified_acreage against, so this is accepted as reconciled
    outright, not left pending forever for a number that will never arrive."""
    with get_session() as session:
        result = reconcile_tract(session, covid=3297, tract_no=1)
    assert result["checked"] is True, result
    assert result["status"] == "reconciled", result
    assert result["unaccounted_acreage"] is None, result
    print("PASS: reconcile_tract (covid 3297) -> no stated_acreage to compare against "
          "is accepted as reconciled, not stuck pending")


def test_reconcile_tract_not_eligible_for_unresolved_boundary() -> None:
    """covid 3194's tracts are boundary_resolution_method='metes_and_bounds_
    traverse' -- a real, independently-derived polygon, but with ZERO
    parcel_covenant rows (confirmed real: this project has never actually
    built the spatial-first interior-parcel classification this resolution
    method needs before a residual/acreage check would mean anything).
    Must report not-checkable rather than silently comparing against a
    classified_acreage that doesn't reflect any real parcel match."""
    with get_session() as session:
        result = reconcile_tract(session, covid=3194, tract_no=1)
    assert result["checked"] is False, result
    assert "not-yet-built" in result["reason"] or "not yet built" in result["reason"], result
    print("PASS: reconcile_tract (covid 3194) -> metes-and-bounds tracts with no "
          "spatially-classified parcels are correctly reported as not yet checkable")


def test_reconcile_covenant_advances_status_when_fully_clean() -> None:
    """covid 3595 (Douglas Co CO): a clean current_parcel_match tract with
    no stated_acreage to compare AND no other outstanding review_reason
    note from any earlier stage -- the one case in this project's own data
    where reconciliation alone is enough to justify covenant.status
    advancing all the way to 'reconciled', not just the tract."""
    with get_session() as session:
        result = reconcile_covenant(session, covid=3595)
    assert result["tract_results"][1]["status"] == "reconciled", result
    assert result["final_status"] == "reconciled", result
    print("PASS: reconcile_covenant (covid 3595) -> a fully clean covenant advances "
          "all the way to 'reconciled'")


def test_reconcile_covenant_never_silently_clears_an_unrelated_note() -> None:
    """covid 3297: its OWN tract reconciles cleanly, but covenant.
    review_reason still carries an unrelated, untagged historical note (the
    stated_acreage-nulling explanation) -- reconcile_covenant must never
    silently advance status past 'needs_review' just because ITS OWN check
    passed, when a human might still need to see that other note."""
    with get_session() as session:
        result = reconcile_covenant(session, covid=3297)
    assert result["tract_results"][1]["status"] == "reconciled", result
    assert result["final_status"] == "needs_review", result
    print("PASS: reconcile_covenant (covid 3297) -> a clean reconciliation does not "
          "override an unrelated, still-present review note")


if __name__ == "__main__":
    test_reconcile_tract_over_classified()
    test_reconcile_tract_unaccounted_area()
    test_reconcile_tract_no_stated_acreage_is_reconciled()
    test_reconcile_tract_not_eligible_for_unresolved_boundary()
    test_reconcile_covenant_advances_status_when_fully_clean()
    test_reconcile_covenant_never_silently_clears_an_unrelated_note()
    print("\nall reconcile smoke tests passed")
