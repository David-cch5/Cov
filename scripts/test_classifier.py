"""Smoke test for app/gis/classifier.py's classify_metes_and_bounds_tract --
the spatial-first parcel census CLAUDE.md requires for metes-and-bounds-
resolved tracts ("enumerate every parcel whose geometry falls in the covenant
polygon"; "NEVER a bounding-box approximation"). Confirmed real before this
was built: every metes_and_bounds_traverse tract in this project had ZERO
parcel_covenant rows.

The live-classification tests are rolled back, not committed -- unlike
reconcile_tract/reconcile_covenant (idempotent UPDATEs), each call here
INSERTs a new monitor_run/parcel_covenant batch (run_seq = MAX+1), by design
(monitor_run is an audit trail of periodic re-checks, not a cache) -- so
re-running this file live would accumulate a fresh duplicate batch every time.
The already-real, already-committed results of running this live exactly once
against Montgomery's ArcGIS service (covid 3194 tracts 1 & 2, covid 8245
tract 1) are instead checked directly from the DB, which is fast, has no
network dependency, and exercises reconcile.py's own consumption of this
table for free.

Usage: python3 scripts/test_classifier.py
"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import SessionLocal, get_session
from app.config import DB_SCHEMA
from app.gis.classifier import classify_metes_and_bounds_tract


def test_classify_wrong_boundary_method_raises() -> None:
    """covid 3297 tract 1 is boundary_resolution_method='current_parcel_match'
    (a subdivision-plat tract, resolved by resolve_subdivision_plat_tract) --
    calling the metes-and-bounds classifier on it must fail loudly rather than
    silently doing a spatial query against a tract.geom that's already just
    the union of its own matched parcels (nothing independent to classify)."""
    with get_session() as session:
        try:
            classify_metes_and_bounds_tract(session, covid=3297, tract_no=1)
            raise AssertionError("expected RuntimeError, but classify_metes_and_bounds_tract returned normally")
        except RuntimeError as e:
            assert "current_parcel_match" in str(e), e
    print("PASS: classify_metes_and_bounds_tract -> refuses to run against a "
          "current_parcel_match tract")


def test_classify_live_montgomery_3194_tract1() -> None:
    """Live spatial query against Montgomery's ArcGIS service for covid 3194
    tract 1 (934.58-ac metes-and-bounds tract). Rolled back -- see module
    docstring. Bounds-checked rather than exact-matched against parcel counts
    (the county's own live parcel roll can shift slightly between runs, same
    live-data-drift risk documented for chain-of-title's own tests), but
    classified_acreage + residual acreage must always sum to the tract's own
    polygon area exactly, by construction (ST_Difference against the same
    tract.geom) -- that internal consistency is the real regression check."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        result = classify_metes_and_bounds_tract(session, covid=3194, tract_no=1)
        assert result["matched_parcels"] > 0, result
        assert result["candidates_in_bbox"] >= result["matched_parcels"], result
        assert result["interior"] + result["boundary"] == result["matched_parcels"], result

        row = session.execute(text("""
            SELECT classified_acreage, ST_Area(geom::geography) / 4046.8564224 AS tract_acreage,
                   ST_Area(residual_geom::geography) / 4046.8564224 AS residual_acreage,
                   ST_IsValid(residual_geom) AS residual_valid
            FROM tract WHERE covid = 3194 AND tract_no = 1
        """)).fetchone()
        assert row.residual_valid, row
        assert abs(float(row.classified_acreage) + float(row.residual_acreage) - float(row.tract_acreage)) < 0.001, row
    finally:
        session.rollback()
        session.close()
    print("PASS: classify_metes_and_bounds_tract (live, covid 3194 tract 1) -> "
          "classified_acreage + residual always reconstitutes the tract's own polygon area")


def test_persisted_montgomery_3194_real_classification() -> None:
    """The already-committed, real result of running classify_metes_and_
    bounds_tract live against covid 3194's two tracts (see reconcile.py's own
    tests for the reconciliation-level consequence): 327 parcels matched for
    tract 1 (265 interior, 62 boundary), 856.418 ac classified against a
    934.58-ac tract, a 78.159-ac real geometric residual."""
    with get_session() as session:
        row = session.execute(text("""
            SELECT classified_acreage, ST_Area(residual_geom::geography) / 4046.8564224 AS residual_acreage
            FROM tract WHERE covid = 3194 AND tract_no = 1
        """)).fetchone()
        counts = dict(session.execute(text("""
            SELECT classification, count(*) AS n FROM parcel_covenant
            WHERE covid = 3194 AND tract_no = 1 GROUP BY classification
        """)).fetchall())
    assert counts["interior"] == 265, counts
    assert counts["boundary"] == 62, counts
    assert abs(float(row.classified_acreage) - 856.418) < 0.01, row
    assert abs(float(row.residual_acreage) - 78.159) < 0.01, row
    print("PASS: parcel_covenant (covid 3194 tract 1) -> real, committed spatial "
          "classification (265 interior / 62 boundary) matches the live run's own result")


def test_persisted_montgomery_8245_real_classification() -> None:
    """The already-committed result for covid 8245 tract 1 -- corrected
    2026-07-28/29 after a real georeferencing error was found (the tract's
    original geom, likely built from an incomplete _textcache_final copy of
    the deed's Exhibit A missing its opening courses, was shifted enough to
    miss its true parcels and instead spatially catch 8 unrelated ones).
    Re-derived from the deed's complete metes-and-bounds text (found in
    _textcache) and anchored to 4 real corners of the adjoining Oak Ridge
    North Sec. 5 lots the deed itself ties to. The corrected polygon
    dominantly matches exactly 2 real parcels -- APN 451910 (94% overlap,
    the "Alore Center" Reserve A equivalent) and APN 41116 (99% overlap,
    Reserve B) -- classified_acreage lands within 0.01 ac of the deed's own
    stated 4.6055 ac. A few negligible sliver matches (<3% overlap, low
    confidence) from the polygon's own small residual imprecision may also
    appear -- not asserted on by exact count, since that's sensitive to
    live GIS data and floating-point noise at a sub-acre scale; the two
    real, dominant matches are the actual regression check."""
    with get_session() as session:
        row = session.execute(text("""
            SELECT classified_acreage, ST_Area(residual_geom::geography) / 4046.8564224 AS residual_acreage
            FROM tract WHERE covid = 8245 AND tract_no = 1
        """)).fetchone()
        overlaps = dict(session.execute(text("""
            SELECT apn, overlap_fraction FROM parcel_covenant
            WHERE covid = 8245 AND tract_no = 1 AND apn IN ('451910', '41116')
        """)).fetchall())
    assert float(overlaps["451910"]) > 0.9, overlaps
    assert float(overlaps["41116"]) > 0.9, overlaps
    assert abs(float(row.classified_acreage) - 4.6055) < 0.05, row
    print("PASS: parcel_covenant (covid 8245 tract 1) -> corrected classification "
          "dominantly matches the real Alore Center Reserve A/B parcels (>90% overlap each)")


if __name__ == "__main__":
    test_classify_wrong_boundary_method_raises()
    test_classify_live_montgomery_3194_tract1()
    test_persisted_montgomery_3194_real_classification()
    test_persisted_montgomery_8245_real_classification()
    print("\nall classifier smoke tests passed")
