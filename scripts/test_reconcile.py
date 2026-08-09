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

from sqlalchemy import text

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
    """covid 3346 tract 2 is only geocode-approximate (approximate_geom, not a
    confirmed tract.geom -- boundary_resolution_method is NULL) -- there's no
    real boundary to reconcile against at all yet, distinct from (and a step
    earlier than) a metes-and-bounds tract that has a confirmed boundary but
    no parcel census yet. (Tract 1 of this same covid was this test's own
    original fixture until it got resolved via chain-of-title parcel
    matching -- a real, expected consequence of the project moving forward,
    not a regression; tract 2 remains genuinely unresolved and now serves
    the same role.)"""
    with get_session() as session:
        result = reconcile_tract(session, covid=3346, tract_no=2)
    assert result["checked"] is False, result
    assert "not yet confirmed" in result["reason"], result
    print("PASS: reconcile_tract (covid 3346 tract 2) -> a tract with only an approximate, "
          "unconfirmed boundary is correctly reported as not yet checkable")


def test_reconcile_tract_metes_and_bounds_real_residual_unaccounted() -> None:
    """covid 3194 tract 1 (Montgomery): a real, independently-derived
    metes-and-bounds polygon (934.58 ac) now has a real spatial parcel
    census against it (app/gis/classifier.py's
    classify_metes_and_bounds_tract, run live against Montgomery's ArcGIS
    service -- 856.263 ac classified). The 78.314-ac gap is a REAL geometric
    residual (ST_Difference against tract.geom), not a stated-acreage
    comparison -- flagged unaccounted_area for human review rather than
    silently accepted.

    Was 78.159 ac against 856.418 classified until 2026-08-08, when apn 32827
    and 449104 were attributed to tract 2 (they straddle this covenant's own
    internal tract line at 0.03% and 4.22% here, against 99.14% and 95.78%
    there). The 0.155 ac they had covered is now correctly unmatched."""
    with get_session() as session:
        result = reconcile_tract(session, covid=3194, tract_no=1)
    assert result["checked"] is True, result
    assert result["status"] == "unaccounted_area", result
    assert abs(result["unaccounted_acreage"] - 78.314) < 0.01, result
    print("PASS: reconcile_tract (covid 3194 tract 1) -> real geometric residual from "
          "spatial parcel classification correctly flagged as unaccounted_area")


def test_reconcile_tract_metes_and_bounds_small_residual_over_tolerance() -> None:
    """covid 8245 tract 1 (Montgomery): after its tract.geom was corrected
    2026-07-28/29 (a real georeferencing error was found and fixed -- see
    app/gis/classifier.py's own test for detail), the tract now dominantly
    matches its two real parcels (APN 451910, 41116 -- the "Alore Center"
    Reserve A/B equivalents) with a small residual of ~0.017 ac -- just over
    RECONCILIATION_TOLERANCE_ACRES, unlike 3194's dramatically-over-tolerance
    78-ac gap. Confirms the tolerance check is a real boundary, not a fudge
    factor -- a small but genuine residual still gets flagged, not waved
    through just because it's close."""
    with get_session() as session:
        result = reconcile_tract(session, covid=8245, tract_no=1)
    assert result["checked"] is True, result
    assert result["status"] == "unaccounted_area", result
    assert RECONCILIATION_TOLERANCE_ACRES < result["unaccounted_acreage"] < 0.05, result
    print("PASS: reconcile_tract (covid 8245) -> a small but genuine residual just over "
          "tolerance is still correctly flagged, not waved through")


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


def test_reconcile_covenant_note_stripping_preserves_a_later_note() -> None:
    """Confirmed real (covid 8534 tract 1, 2026-08-06): classifier.py's
    exclude_non_tract_parcels appended its own NON-TRACT PARCEL EXCLUSION
    note AFTER an earlier RECONCILIATION-STAGE note already in
    review_reason. Re-running reconcile_covenant silently deleted that
    later note entirely -- the old regex stripped its own tag with a bare
    `.*$`, which greedily matched everything to the end of the string,
    not just its own note's own text. Synthetic fixture (a clean
    current_parcel_match tract, so reconcile_covenant itself adds no new
    note) isolates the string-manipulation bug from any real classify/
    reconcile business logic."""
    try:
        with get_session() as session:
            session.execute(text("""
                INSERT INTO covenant (covid, county_fips, status, legal_description_raw, stated_acreage, review_reason)
                VALUES (999995, '48339', 'needs_review', 'test fixture', 10.0,
                        'EARLIER-STAGE (automated, 2020-01-01): old unrelated note; '
                        'RECONCILIATION-STAGE (automated, 2020-01-01): stale prior residual detail; '
                        'NON-TRACT PARCEL EXCLUSION (automated, tract 1): must survive this run (APN1, APN2)')
                ON CONFLICT (covid) DO UPDATE SET
                    status = EXCLUDED.status, stated_acreage = EXCLUDED.stated_acreage,
                    review_reason = EXCLUDED.review_reason
            """))
            session.execute(text("""
                INSERT INTO tract (covid, tract_no, approximate_geom, boundary_resolution_method, classified_acreage)
                VALUES (999995, 1, ST_SetSRID(ST_GeomFromGeoJSON(
                    '{"type":"MultiPolygon","coordinates":[[[[-95.5,30.3],[-95.5,30.301],[-95.499,30.301],[-95.499,30.3],[-95.5,30.3]]]]}'
                ), 4326), 'current_parcel_match', 10.0)
                ON CONFLICT (covid, tract_no) DO UPDATE SET
                    boundary_resolution_method = EXCLUDED.boundary_resolution_method,
                    classified_acreage = EXCLUDED.classified_acreage
            """))

        with get_session() as session:
            result = reconcile_covenant(session, covid=999995)
            session.commit()
        assert result["tract_results"][1]["status"] == "reconciled", result

        with get_session() as session:
            reason = session.execute(
                text("SELECT review_reason FROM covenant WHERE covid = 999995")
            ).scalar()
        assert "EARLIER-STAGE" in reason, reason
        assert "NON-TRACT PARCEL EXCLUSION" in reason and "must survive this run" in reason, reason
        assert "stale prior residual detail" not in reason, reason
    finally:
        with get_session() as session:
            session.execute(text("DELETE FROM tract WHERE covid = 999995"))
            session.execute(text("DELETE FROM covenant WHERE covid = 999995"))
    print("PASS: reconcile_covenant -> stripping its own stale RECONCILIATION-STAGE note "
          "no longer swallows a later, unrelated note appended after it")


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


def test_reconcile_covenant_metes_and_bounds_small_residual_needs_review() -> None:
    """covid 8245: the corrected tract's own small (~0.017 ac) over-tolerance
    residual is enough, by itself, to keep the covenant in needs_review --
    same behavior as covid 3194's much larger residual, at a very different
    scale, confirming the covenant-level rollup doesn't have some implicit
    minimum-severity threshold of its own beyond the tract-level tolerance
    check that already gates it."""
    with get_session() as session:
        result = reconcile_covenant(session, covid=8245)
    assert result["tract_results"][1]["status"] == "unaccounted_area", result
    assert result["final_status"] == "needs_review", result
    print("PASS: reconcile_covenant (covid 8245) -> a small but genuine tract-level "
          "residual alone is enough to keep the covenant in needs_review")


def test_multi_tract_covenant_never_reconciles_against_the_covenant_total() -> None:
    """Migration 0036. covenant.stated_acreage describes ALL of a covenant's
    tracts together, but reconcile_tract compared each tract's own
    classified_acreage against it. On a single-tract covenant that is the same
    number; on any other it measures one tract against all of them.

    Confirmed real on covid 5838, whose tract 2 is the Gulfside Estates land --
    the deed's own 31.140 + 2.454 = 33.594 ac, classified at 33.518. It was being
    checked against tract 1's 318.779 ac and reported 285.261 ac "unaccounted":
    a gap 850% of the tract's own size, on land that is fully accounted for. With
    tract.stated_acreage set it reports 0.076 ac, which is just the rounding
    between the deed's figure and the parcels'.

    Two tracts here: one carrying its own stated acreage, one not. The first must
    reconcile against ITS number, and the second must report not-checkable rather
    than borrow the covenant's."""
    try:
        with get_session() as session:
            session.execute(text("""
                INSERT INTO covenant (covid, county_fips, status, legal_description_raw, stated_acreage)
                VALUES (999993, '48339', 'needs_review', 'multi-tract fixture', 500.0)
                ON CONFLICT (covid) DO UPDATE SET stated_acreage = EXCLUDED.stated_acreage
            """))
            for tract_no, stated in ((1, 100.0), (2, None)):
                session.execute(text("""
                    INSERT INTO tract (covid, tract_no, approximate_geom, boundary_resolution_method,
                                       classified_acreage, stated_acreage, unaccounted_acreage,
                                       reconciliation_status)
                    VALUES (999993, :tract_no, ST_SetSRID(ST_GeomFromGeoJSON(
                        '{"type":"MultiPolygon","coordinates":[[[[-95.5,30.3],[-95.5,30.301],[-95.499,30.301],[-95.499,30.3],[-95.5,30.3]]]]}'
                    ), 4326), 'current_parcel_match', 97.5, :stated, 402.5, 'unaccounted_area')
                    ON CONFLICT (covid, tract_no) DO UPDATE SET
                        boundary_resolution_method = EXCLUDED.boundary_resolution_method,
                        classified_acreage = EXCLUDED.classified_acreage,
                        stated_acreage = EXCLUDED.stated_acreage,
                        unaccounted_acreage = EXCLUDED.unaccounted_acreage,
                        reconciliation_status = EXCLUDED.reconciliation_status
                """), {"tract_no": tract_no, "stated": stated})

        with get_session() as session:
            own = reconcile_tract(session, covid=999993, tract_no=1)
            session.commit()
        # 100.0 stated - 97.5 classified = 2.5, NOT 500.0 - 97.5 = 402.5
        assert own["checked"] and own["status"] == "unaccounted_area", own
        assert abs(own["unaccounted_acreage"] - 2.5) < 0.001, own

        with get_session() as session:
            unknown = reconcile_tract(session, covid=999993, tract_no=2)
            session.commit()
        assert not unknown["checked"], unknown
        assert "2 tracts" in unknown["reason"], unknown

        # the stale 402.5 written by the old covenant-wide comparison must be
        # cleared, not left sitting as a known-wrong figure
        with get_session() as session:
            row = session.execute(text("""
                SELECT unaccounted_acreage, reconciliation_status
                FROM tract WHERE covid = 999993 AND tract_no = 2
            """)).one()
        assert row.unaccounted_acreage is None, row
        assert row.reconciliation_status == "pending", row
        print(f"PASS: multi-tract covenant -> tract 1 reconciles against its own 100 ac "
              f"({own['unaccounted_acreage']:.1f} ac, not 402.5), tract 2 reports not-checkable "
              f"and its stale figure is cleared")
    finally:
        with get_session() as session:
            session.execute(text("DELETE FROM tract WHERE covid = 999993"))
            session.execute(text("DELETE FROM covenant WHERE covid = 999993"))
            session.commit()


def test_single_tract_covenant_still_uses_the_covenant_figure() -> None:
    """The fallback that must survive: where a covenant has exactly one tract,
    its stated acreage IS that tract's, and reconciliation has to keep working
    without tract.stated_acreage being populated. Migration 0036 backfills those
    rows, but the code path is what is pinned here."""
    try:
        with get_session() as session:
            session.execute(text("""
                INSERT INTO covenant (covid, county_fips, status, legal_description_raw, stated_acreage)
                VALUES (999992, '48339', 'needs_review', 'single-tract fixture', 10.0)
                ON CONFLICT (covid) DO UPDATE SET stated_acreage = EXCLUDED.stated_acreage
            """))
            session.execute(text("""
                INSERT INTO tract (covid, tract_no, approximate_geom, boundary_resolution_method,
                                   classified_acreage, stated_acreage)
                VALUES (999992, 1, ST_SetSRID(ST_GeomFromGeoJSON(
                    '{"type":"MultiPolygon","coordinates":[[[[-95.5,30.3],[-95.5,30.301],[-95.499,30.301],[-95.499,30.3],[-95.5,30.3]]]]}'
                ), 4326), 'current_parcel_match', 8.0, NULL)
                ON CONFLICT (covid, tract_no) DO UPDATE SET
                    classified_acreage = EXCLUDED.classified_acreage, stated_acreage = NULL
            """))
        with get_session() as session:
            got = reconcile_tract(session, covid=999992, tract_no=1)
            session.commit()
        assert got["checked"] and abs(got["unaccounted_acreage"] - 2.0) < 0.001, got
        print("PASS: single-tract covenant -> still falls back to covenant.stated_acreage (2.0 ac gap)")
    finally:
        with get_session() as session:
            session.execute(text("DELETE FROM tract WHERE covid = 999992"))
            session.execute(text("DELETE FROM covenant WHERE covid = 999992"))
            session.commit()


if __name__ == "__main__":
    test_reconcile_tract_over_classified()
    test_reconcile_tract_unaccounted_area()
    test_reconcile_tract_no_stated_acreage_is_reconciled()
    test_reconcile_tract_not_eligible_before_parcel_census()
    test_reconcile_tract_metes_and_bounds_real_residual_unaccounted()
    test_reconcile_tract_metes_and_bounds_small_residual_over_tolerance()
    test_reconcile_covenant_advances_status_when_fully_clean()
    test_reconcile_covenant_note_stripping_preserves_a_later_note()
    test_reconcile_covenant_never_silently_clears_an_unrelated_note()
    test_reconcile_covenant_metes_and_bounds_flags_real_residual()
    test_reconcile_covenant_metes_and_bounds_small_residual_needs_review()
    test_multi_tract_covenant_never_reconciles_against_the_covenant_total()
    test_single_tract_covenant_still_uses_the_covenant_figure()
    print("\nall reconcile smoke tests passed")
