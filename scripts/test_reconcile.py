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
from app.gis.reconcile import RECONCILIATION_TOLERANCE_ACRES, reconcile_covenant, reconcile_tract


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


def test_reconcile_tract_not_eligible_before_parcel_census() -> None:
    """covid 3346's tract is only geocode-approximate (approximate_geom, not a
    confirmed tract.geom -- boundary_resolution_method is NULL) -- there's no
    real boundary to reconcile against at all yet, distinct from (and a step
    earlier than) a metes-and-bounds tract that has a confirmed boundary but
    no parcel census yet."""
    with get_session() as session:
        result = reconcile_tract(session, covid=3346, tract_no=1)
    assert result["checked"] is False, result
    assert "not yet confirmed" in result["reason"], result
    print("PASS: reconcile_tract (covid 3346) -> a tract with only an approximate, "
          "unconfirmed boundary is correctly reported as not yet checkable")


def test_reconcile_tract_metes_and_bounds_real_residual_unaccounted() -> None:
    """covid 3194 tract 1 (Montgomery): a real, independently-derived
    metes-and-bounds polygon (934.58 ac) now has a real spatial parcel
    census against it (app/gis/classifier.py's
    classify_metes_and_bounds_tract, run live against Montgomery's ArcGIS
    service -- 327 parcels matched, 856.418 ac classified). The 78.159-ac
    gap is a REAL geometric residual (ST_Difference against tract.geom),
    not a stated-acreage comparison -- flagged unaccounted_area for human
    review rather than silently accepted."""
    with get_session() as session:
        result = reconcile_tract(session, covid=3194, tract_no=1)
    assert result["checked"] is True, result
    assert result["status"] == "unaccounted_area", result
    assert abs(result["unaccounted_acreage"] - 78.159) < 0.01, result
    print("PASS: reconcile_tract (covid 3194 tract 1) -> real geometric residual from "
          "spatial parcel classification correctly flagged as unaccounted_area")


def test_reconcile_tract_metes_and_bounds_real_residual_reconciled() -> None:
    """covid 8245 tract 1 (Montgomery): a small (4.61-ac) metes-and-bounds
    tract whose spatial parcel census (8 parcels, all boundary-classified --
    consistent with one or more larger parent parcels straddling the tract's
    edge rather than lots platted wholly inside it) leaves a residual of
    ~6.5e-7 acres -- floating-point noise, not a real gap. Confirms the
    tolerance check (not a naive nonzero check) is what actually gates
    'reconciled' here."""
    with get_session() as session:
        result = reconcile_tract(session, covid=8245, tract_no=1)
    assert result["checked"] is True, result
    assert result["status"] == "reconciled", result
    assert result["unaccounted_acreage"] < RECONCILIATION_TOLERANCE_ACRES, result
    print("PASS: reconcile_tract (covid 8245) -> a metes-and-bounds tract whose real "
          "residual is within tolerance reconciles cleanly")


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


def test_reconcile_covenant_metes_and_bounds_flags_real_residual() -> None:
    """covid 3194: both tracts now have a real spatial parcel census, and
    both carry a genuine (not tolerance-noise) residual -- the covenant must
    land in needs_review, with the RECONCILIATION-STAGE note carrying both
    tracts' own residual detail, and the pre-existing unrelated note (the
    stated_acreage correction explaining Tract I + Tract II) left intact."""
    with get_session() as session:
        result = reconcile_covenant(session, covid=3194)
    assert result["tract_results"][1]["status"] == "unaccounted_area", result
    assert result["tract_results"][2]["status"] == "unaccounted_area", result
    assert result["final_status"] == "needs_review", result
    print("PASS: reconcile_covenant (covid 3194) -> real residuals on both tracts "
          "correctly keep the covenant in needs_review")


def test_reconcile_covenant_metes_and_bounds_advances_when_clean() -> None:
    """covid 8245: a single metes-and-bounds tract whose real residual is
    within tolerance, and no other outstanding review_reason note --
    advances all the way to 'reconciled', same as a clean current_parcel_
    match covenant does."""
    with get_session() as session:
        result = reconcile_covenant(session, covid=8245)
    assert result["tract_results"][1]["status"] == "reconciled", result
    assert result["final_status"] == "reconciled", result
    print("PASS: reconcile_covenant (covid 8245) -> a clean metes-and-bounds "
          "reconciliation advances the covenant same as current_parcel_match does")


if __name__ == "__main__":
    test_reconcile_tract_over_classified()
    test_reconcile_tract_unaccounted_area()
    test_reconcile_tract_no_stated_acreage_is_reconciled()
    test_reconcile_tract_not_eligible_before_parcel_census()
    test_reconcile_tract_metes_and_bounds_real_residual_unaccounted()
    test_reconcile_tract_metes_and_bounds_real_residual_reconciled()
    test_reconcile_covenant_advances_status_when_fully_clean()
    test_reconcile_covenant_never_silently_clears_an_unrelated_note()
    test_reconcile_covenant_metes_and_bounds_flags_real_residual()
    test_reconcile_covenant_metes_and_bounds_advances_when_clean()
    print("\nall reconcile smoke tests passed")
