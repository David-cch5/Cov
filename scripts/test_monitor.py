"""Smoke test for app/gis/monitor.py -- CLAUDE.md's own "monitor remaining raw
acreage for new plats," plus the parcel_lineage tracking migration 0029 named
the Monitor to build (a fee owed on a bulk transfer needs to trace forward to
whatever lot a later split/replat/merge produced). Confirmed real before this
was built: monitor_run's residual_acreage_before/after columns, parcel_
history's 'monitor_diff'/'replat' change_reasons, and the entire parcel_
lineage table have existed in the schema since migrations 0001/0029, but
nothing had ever written to any of them.

Live re-checks that find nothing new are run for real, not rolled back --
logging "checked, nothing changed" is itself the real, intended behavior of a
monitoring pass, the same way a clean reconcile_tract run is. The
new-parcel/retirement/lineage detection mechanisms, though, can't be
exercised against genuine events on demand (nothing has actually been
re-platted in Montgomery since this project's last live classification run)
-- so those paths are tested against synthetic candidate sets (a real,
already-classified parcel's own geometry, reused under a fake apn to stand in
for "the county's next live response"), with the live GIS call itself mocked
out rather than hitting the real service, rolled back after each test. This
exercises the exact same code paths a real new plat/split/merge would.

Usage: python3 scripts/test_monitor.py
"""
import json
import sys
from unittest.mock import patch

sys.path.insert(0, ".")

from sqlalchemy import text

from app.config import DB_SCHEMA
from app.db.session import SessionLocal, get_session
from app.gis.adapters import montgomery_tx
from app.gis.monitor import monitor_tract_for_new_plats


def _matched_candidates(session, covid: int, tract_no: int) -> list[dict]:
    """The tract's own real, already-classified parcels, in the same dict
    shape montgomery_tx.iter_parcels yields -- the base candidate set for
    tests that mock the live GIS response."""
    rows = session.execute(text("""
        SELECT p.county_fips, p.apn, p.owner_name_raw, p.situs_address, p.acreage,
               ST_AsGeoJSON(p.geom) AS geojson
        FROM parcel p JOIN parcel_covenant pc ON pc.county_fips = p.county_fips AND pc.apn = p.apn
        WHERE pc.covid = :covid AND pc.tract_no = :tract_no
    """), {"covid": covid, "tract_no": tract_no}).fetchall()
    return [
        {"county_fips": r.county_fips, "apn": r.apn, "owner_name_raw": r.owner_name_raw,
         "situs_address": r.situs_address, "acreage": float(r.acreage) if r.acreage is not None else None,
         "geojson": json.loads(r.geojson)}
        for r in rows
    ]


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


def test_monitor_detects_clean_1to1_replat() -> None:
    """A retired apn replaced by exactly one new apn covering the same
    footprint (a pure renumbering/boundary correction, no real subdivision)
    -> lineage_type='replat', a parcel_history snapshot of the retired
    parcel's last-known state, its parcel_covenant row removed (no longer a
    current lot), and -- since this is a confidently-resolved event, not an
    uncertain one -- no review flag. Live GIS response mocked (see module
    docstring); rolled back."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        base = _matched_candidates(session, 8245, 1)
        parent = base[0]
        synthetic_child = {**parent, "apn": "TESTCHILD-1to1"}
        synthetic_candidates = base[1:] + [synthetic_child]

        before = session.execute(text("SELECT status, review_reason FROM covenant WHERE covid = 8245")).fetchone()

        with patch.object(montgomery_tx, "iter_parcels", return_value=synthetic_candidates):
            result = monitor_tract_for_new_plats(session, covid=8245, tract_no=1, run_type="manual")

        assert result["retired_parcels_found"] == 1, result
        assert result["lineage_edges_written"] == 1, result
        assert result["unresolved_retirements"] == [], result

        lineage = session.execute(text("""
            SELECT lineage_type FROM parcel_lineage WHERE apn = 'TESTCHILD-1to1' AND parent_apn = :p
        """), {"p": parent["apn"]}).fetchone()
        assert lineage is not None and lineage.lineage_type == "replat", lineage

        history = session.execute(text("""
            SELECT change_reason FROM parcel_history WHERE apn = :p
        """), {"p": parent["apn"]}).fetchone()
        assert history is not None and history.change_reason == "replat", history

        still_tracked = session.execute(text("""
            SELECT 1 FROM parcel_covenant WHERE covid = 8245 AND tract_no = 1 AND apn = :p
        """), {"p": parent["apn"]}).fetchone()
        assert still_tracked is None, "a retired parcel must no longer count as a current lot"

        after = session.execute(text("SELECT status, review_reason FROM covenant WHERE covid = 8245")).fetchone()
        assert after.status == before.status and after.review_reason == before.review_reason, (before, after)
    finally:
        session.rollback()
        session.close()
    print("PASS: monitor_tract_for_new_plats -> a clean 1:1 retirement is recorded as "
          "lineage_type='replat', with no review flag (confidently resolved)")


def test_monitor_detects_1to_many_subdivision_split() -> None:
    """A retired apn replaced by two new apns, both overlapping its old
    footprint -> lineage_type='subdivision_split' for both edges."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        base = _matched_candidates(session, 8245, 1)
        parent = base[0]
        child_a = {**parent, "apn": "TESTCHILD-SPLIT-A"}
        child_b = {**parent, "apn": "TESTCHILD-SPLIT-B"}
        synthetic_candidates = base[1:] + [child_a, child_b]

        with patch.object(montgomery_tx, "iter_parcels", return_value=synthetic_candidates):
            result = monitor_tract_for_new_plats(session, covid=8245, tract_no=1, run_type="manual")

        assert result["retired_parcels_found"] == 1, result
        assert result["lineage_edges_written"] == 2, result
        assert result["unresolved_retirements"] == [], result

        rows = session.execute(text("""
            SELECT apn, lineage_type FROM parcel_lineage WHERE parent_apn = :p ORDER BY apn
        """), {"p": parent["apn"]}).fetchall()
        assert [r.apn for r in rows] == ["TESTCHILD-SPLIT-A", "TESTCHILD-SPLIT-B"], rows
        assert all(r.lineage_type == "subdivision_split" for r in rows), rows
    finally:
        session.rollback()
        session.close()
    print("PASS: monitor_tract_for_new_plats -> a one-parent, two-child retirement is "
          "recorded as lineage_type='subdivision_split' for both edges")


def test_monitor_detects_many_to_1_merge() -> None:
    """Two retired apns whose union is covered by exactly one new apn ->
    lineage_type='merge' for both edges."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        base = _matched_candidates(session, 8245, 1)
        parent_a, parent_b = base[0], base[1]
        merged_geom = session.execute(text("""
            SELECT ST_AsGeoJSON(ST_Union(geom)) AS gj FROM parcel WHERE county_fips = '48339' AND apn = ANY(:apns)
        """), {"apns": [parent_a["apn"], parent_b["apn"]]}).fetchone()
        merged_child = {**parent_a, "apn": "TESTCHILD-MERGE", "geojson": json.loads(merged_geom.gj)}
        synthetic_candidates = base[2:] + [merged_child]

        with patch.object(montgomery_tx, "iter_parcels", return_value=synthetic_candidates):
            result = monitor_tract_for_new_plats(session, covid=8245, tract_no=1, run_type="manual")

        assert result["retired_parcels_found"] == 2, result
        assert result["lineage_edges_written"] == 2, result
        assert result["unresolved_retirements"] == [], result

        rows = session.execute(text("""
            SELECT parent_apn, lineage_type FROM parcel_lineage WHERE apn = 'TESTCHILD-MERGE' ORDER BY parent_apn
        """)).fetchall()
        assert {r.parent_apn for r in rows} == {parent_a["apn"], parent_b["apn"]}, rows
        assert all(r.lineage_type == "merge" for r in rows), rows
    finally:
        session.rollback()
        session.close()
    print("PASS: monitor_tract_for_new_plats -> two retired parents covered by one new "
          "child is recorded as lineage_type='merge' for both edges")


def test_monitor_flags_unresolved_retirement_for_review() -> None:
    """A retired apn with NO overlapping successor at all -- still
    snapshotted into parcel_history and removed from parcel_covenant, but
    with no lineage edge to write (nothing to link it to), so the covenant
    is flagged needs_review rather than the loss being silently dropped."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        base = _matched_candidates(session, 8245, 1)
        parent = base[0]
        synthetic_candidates = base[1:]  # parent simply vanishes, nothing replaces it

        with patch.object(montgomery_tx, "iter_parcels", return_value=synthetic_candidates):
            result = monitor_tract_for_new_plats(session, covid=8245, tract_no=1, run_type="manual")

        assert result["retired_parcels_found"] == 1, result
        assert result["lineage_edges_written"] == 0, result
        assert result["unresolved_retirements"] == [parent["apn"]], result

        history = session.execute(text("""
            SELECT change_reason FROM parcel_history WHERE apn = :p
        """), {"p": parent["apn"]}).fetchone()
        assert history is not None and history.change_reason == "replat", history

        covenant = session.execute(text("SELECT status, review_reason FROM covenant WHERE covid = 8245")).fetchone()
        assert covenant.status == "needs_review", covenant
        assert "MONITOR-STAGE" in covenant.review_reason and parent["apn"] in covenant.review_reason, covenant
    finally:
        session.rollback()
        session.close()
    print("PASS: monitor_tract_for_new_plats -> a retirement with no overlapping "
          "successor flags the covenant for review instead of being silently dropped")


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
            FROM monitor_run WHERE covid = 3194 AND run_seq = 5
        """)).fetchone()
        r8245 = session.execute(text("""
            SELECT run_type, new_parcels_found, residual_acreage_before, residual_acreage_after
            FROM monitor_run WHERE covid = 8245 AND run_seq = 2
        """)).fetchone()
    assert r3194.run_type == "manual" and r3194.new_parcels_found == 0, r3194
    assert abs(float(r3194.residual_acreage_before) - float(r3194.residual_acreage_after)) < 0.001, r3194
    assert r8245.run_type == "manual" and r8245.new_parcels_found == 0, r8245
    assert float(r8245.residual_acreage_before) < 0.001, r8245
    print("PASS: monitor_run (covid 3194 run_seq 5, covid 8245 run_seq 2) -> real "
          "'manual' re-checks correctly found nothing new, residual unchanged")


if __name__ == "__main__":
    test_monitor_wrong_boundary_method_raises()
    test_monitor_invalid_run_type_raises()
    test_monitor_detects_a_real_but_untracked_parcel()
    test_monitor_detects_clean_1to1_replat()
    test_monitor_detects_1to_many_subdivision_split()
    test_monitor_detects_many_to_1_merge()
    test_monitor_flags_unresolved_retirement_for_review()
    test_monitor_persisted_real_nothing_new_runs()
    print("\nall monitor smoke tests passed")
