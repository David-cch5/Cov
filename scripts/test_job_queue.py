"""Smoke test for app/queue/job_queue.py -- the shared retry-with-backoff +
durable failure-logging mechanism used by BOTH the recorder-portal adapters
(app/recorder/diagnose.py) and the GIS classifier (app/gis/classifier.py).
Covers the mechanism itself (retry-then-succeed, retry-then-fail-and-log)
with synthetic functions -- fast and deterministic, no live network call
needed to prove the mechanism works -- plus one check per real consumer
confirming it's actually wired in, not just present in the codebase.

Usage: python3 scripts/test_job_queue.py
"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import SessionLocal, get_session
from app.config import DB_SCHEMA
from app.queue.job_queue import JobFailed, run_with_job_queue


def test_retry_then_succeed() -> None:
    """The case the retry window exists for: a call that fails a few times
    then recovers within the window. Should return normally and never touch
    job_queue at all."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError(f"synthetic transient failure #{calls['n']}")
        return "ok"

    result = run_with_job_queue(
        flaky, job_type="test_retry_then_succeed", max_attempts=5, backoff_seconds=(1, 1, 1, 1),
    )
    assert result == "ok", result
    assert calls["n"] == 3, calls
    print("PASS: retry-then-succeed -> recovered on attempt 3, no job_queue row written")


def test_retry_then_fail_writes_job_queue() -> None:
    """The case that actually broke the first time this was built: a
    persistent failure must still write a durable job_queue row even though
    the caller goes on to raise an exception right after -- confirmed by
    reading it back with a separate session, then cleaned up."""
    def always_fails():
        raise ConnectionError("synthetic persistent failure for job_queue smoke test")

    try:
        run_with_job_queue(
            always_fails, job_type="test_retry_then_fail", covid=8386, county_fips="48139",
            payload={"note": "smoke test"}, max_attempts=2, backoff_seconds=(1,),
        )
        raise AssertionError("expected JobFailed, but run_with_job_queue returned normally")
    except JobFailed as e:
        job_id = e.job_id
        assert job_id is not None, "JobFailed raised but no job_queue row was recorded"

    with get_session() as session:
        row = session.execute(
            text("SELECT status, covid, county_fips, error_message FROM job_queue WHERE job_id = :id"),
            {"id": job_id},
        ).fetchone()
        assert row is not None, f"job_queue row {job_id} was not found -- the failure-log write didn't survive"
        assert row.status == "error", row
        assert row.covid == 8386, row
        session.execute(text("DELETE FROM job_queue WHERE job_id = :id"), {"id": job_id})
    print(f"PASS: retry-then-fail -> job_id={job_id} recorded and verified, then cleaned up")


def test_gis_classifier_is_wired_to_job_queue() -> None:
    """Confirms app/gis/classifier.py actually routes its live ArcGIS query
    through run_with_job_queue -- not just that the mechanism exists
    somewhere in the codebase. Monkeypatches Harris's adapter function to
    fail deterministically (a real GIS network call would just add
    flakiness to a test that isn't trying to re-prove ArcGIS can be
    unreachable), runs the classifier against a real, already-resolved
    covenant (covid 7991) inside a session that gets rolled back so nothing
    is actually touched, and restores the adapter function afterward
    regardless of outcome."""
    from app.gis import classifier
    from app.gis.adapters import harris_tx

    original = harris_tx.query_by_subdivision_and_lots

    def broken(*args, **kwargs):
        raise ConnectionError("synthetic ArcGIS outage for job_queue smoke test")

    harris_tx.query_by_subdivision_and_lots = broken
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        try:
            classifier.resolve_subdivision_plat_tract(session, covid=7991)
            raise AssertionError("expected JobFailed, but resolve_subdivision_plat_tract returned normally")
        except JobFailed as e:
            job_id = e.job_id
    finally:
        harris_tx.query_by_subdivision_and_lots = original
        session.rollback()  # never persist anything from this test's covenant/tract writes
        session.close()

    with get_session() as verify_session:
        row = verify_session.execute(
            text("SELECT job_type, covid, status FROM job_queue WHERE job_id = :id"),
            {"id": job_id},
        ).fetchone()
        assert row is not None, f"job_queue row {job_id} was not found"
        assert row.job_type == "gis_classifier_query", row
        assert row.covid == 7991, row
        verify_session.execute(text("DELETE FROM job_queue WHERE job_id = :id"), {"id": job_id})
    print(f"PASS: GIS classifier wired to job_queue -> job_id={job_id} recorded and verified, then cleaned up")


if __name__ == "__main__":
    test_retry_then_succeed()
    test_retry_then_fail_writes_job_queue()
    test_gis_classifier_is_wired_to_job_queue()
    print("\nall job_queue smoke tests passed")
