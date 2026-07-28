"""Smoke test for app/gis/monitor.py -- CLAUDE.md's own "monitor remaining raw
acreage for new plats." Confirmed real before this was built: monitor_run's
residual_acreage_before/after columns and parcel_history's 'monitor_diff'
change_reason have existed in the schema since migration 0001 but nothing had
ever written to them.

Live re-checks that find nothing new are run for real, not rolled back --
logging "checked, nothing changed" is itself the real, intended behavior of a
monitoring pass, the same way a clean reconcile_tract run is. The
new-parcel-detection mechanism, though, can't be exercised against a genuine
new plat on demand (nothing has actually been re-platted in Montgomery since
this project's last live classification run) -- so that path is tested by
temporarily deleting one already-classified tract's own parcel_covenant row
(simulating "as if this tract's own classifier had never seen this real
parcel"), rolled back after, which exercises the exact same code path a real
new plat would.

Usage: python3 scripts/test_monitor.py
"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from app.config import DB_SCHEMA
from app.db.session import SessionLocal, get_session
from app.gis.monitor import monitor_tract_for_new_plats


def test_monitor_wrong_boundary_method_raises() -> None:
    """covid 3297 tract 1 is boundary_resolution_method='current_parcel_match'
    -- its own tract.geom IS the union of matched parcels by construction, so
    there's no independent residual to monitor at all."""
    with get_session() as session:
        try:
            monitor_tract_for_new_plats(session, covid=3297, tract_no=1)
            raise AssertionError("expected RuntimeError, but monitor_tract_for_new_plats returned normally")
        except RuntimeError as e:
            assert "current_parcel_match" in str(e), e
    print("PASS: monitor_tract_for_new_plats -> refuses to run against a "
          "current_parcel_match tract (no independent residual to watch)")


def test_monitor_invalid_run_type_raises() -> None:
    with get_session() as session:
        try:
            monitor_tract_for_new_plats(session, covid=3194, tract_no=1, run_type="bogus")
            raise AssertionError("expected ValueError, but monitor_tract_for_new_plats returned normally")
        except ValueError as e:
            assert "bogus" in str(e), e
    print("PASS: monitor_tract_for_new_plats -> rejects a run_type outside the "
          "schema's own CHECK constraint (scheduled/manual)")


def test_monitor_detects_a_real_but_untracked_parcel() -> None:
    """covid 8245 tract 1: temporarily forget one already-classified parcel
    (delete just its parcel_covenant row, leaving the real parcel row alone)
    and confirm a monitoring re-check re-discovers it, re-classifies it
    correctly, and logs a parcel_history snapshot tagged 'monitor_diff' for
    it (since it already existed in the parcel table) -- exactly what a
    monitoring pass finding a genuine new plat would do, exercised without
    needing an actual new plat to exist on the ground right now. Rolled
    back -- this is a simulated gap, not a real one."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        victim = session.execute(text("""
            SELECT apn FROM parcel_covenant WHERE covid = 8245 AND tract_no = 1 LIMIT 1
        """)).fetchone()
        session.execute(text("""
            DELETE FROM parcel_covenant WHERE covid = 8245 AND tract_no = 1 AND apn = :apn
        """), {"apn": victim.apn})

        result = monitor_tract_for_new_plats(session, covid=8245, tract_no=1, run_type="manual")
        assert result["new_parcels_found"] == 1, result

        still_tracked = session.execute(text("""
            SELECT 1 FROM parcel_covenant WHERE covid = 8245 AND tract_no = 1 AND apn = :apn
        """), {"apn": victim.apn}).fetchone()
        assert still_tracked is not None, "monitoring should have re-classified the untracked parcel"

        history = session.execute(text("""
            SELECT change_reason FROM parcel_history WHERE county_fips = '48339' AND apn = :apn
        """), {"apn": victim.apn}).fetchone()
        assert history is not None and history.change_reason == "monitor_diff", history
    finally:
        session.rollback()
        session.close()
    print("PASS: monitor_tract_for_new_plats (covid 8245) -> re-discovers a real, "
          "previously-untracked parcel, re-classifies it, and logs a monitor_diff "
          "parcel_history snapshot")


def test_monitor_persisted_real_nothing_new_runs() -> None:
    """The already-committed result of running monitor_tract_for_new_plats
    live against both real tracts immediately after their own initial
    classification: correctly nothing new (nothing has actually been
    re-platted in the few minutes between the two runs), residual acreage
    unchanged, logged as run_type='manual' with new_parcels_found=0 --
    the first-ever writes to monitor_run.residual_acreage_before/after.
    monitor_run is keyed (covid, run_seq), not per-tract (same convention
    classify_metes_and_bounds_tract's own run_seq lookup already uses), so
    these are checked by the specific run_seq each live call reported back."""
    with get_session() as session:
        r3194 = session.execute(text("""
            SELECT run_type, new_parcels_found, residual_acreage_before, residual_acreage_after
            FROM monitor_run WHERE covid = 3194 AND run_seq = 4
        """)).fetchone()
        r8245 = session.execute(text("""
            SELECT run_type, new_parcels_found, residual_acreage_before, residual_acreage_after
            FROM monitor_run WHERE covid = 8245 AND run_seq = 2
        """)).fetchone()
    assert r3194.run_type == "manual" and r3194.new_parcels_found == 0, r3194
    assert abs(float(r3194.residual_acreage_before) - float(r3194.residual_acreage_after)) < 0.001, r3194
    assert r8245.run_type == "manual" and r8245.new_parcels_found == 0, r8245
    assert float(r8245.residual_acreage_before) < 0.001, r8245
    print("PASS: monitor_run (covid 3194 run_seq 4, covid 8245 run_seq 2) -> real "
          "'manual' re-checks correctly found nothing new, residual unchanged")


if __name__ == "__main__":
    test_monitor_wrong_boundary_method_raises()
    test_monitor_invalid_run_type_raises()
    test_monitor_detects_a_real_but_untracked_parcel()
    test_monitor_persisted_real_nothing_new_runs()
    print("\nall monitor smoke tests passed")
