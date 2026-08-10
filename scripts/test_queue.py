"""Tests for app/queue/queue.py -- the work-queue side of covenant.job_queue.

Runs against the real database, because every property worth testing here is a
property of Postgres: the partial unique index that makes enqueueing
idempotent, and FOR UPDATE SKIP LOCKED making concurrent claims safe. Mocked,
this file would assert that the code calls SQL, which is not the question.

Each test cleans up the rows it created, keyed by a job_type prefix no real
stage uses, so a failed run cannot leave debris that breaks the next one.

Usage: python3 scripts/test_queue.py
"""
import sys
import threading

from sqlalchemy import text

sys.path.insert(0, ".")

from app.db.session import get_session
from app.queue.queue import (
    MAX_ATTEMPTS, RETRY_BACKOFF_SECONDS, claim_next, complete, depth, enqueue, fail,
    hold_for_captcha, hold_for_review, reclaim_stale, worker_id,
)

TEST_PREFIX = "_test_queue_"


def _cleanup() -> None:
    with get_session() as session:
        session.execute(text("DELETE FROM job_queue WHERE job_type LIKE :p"),
                        {"p": TEST_PREFIX + "%"})


def _row(job_id: int) -> dict:
    with get_session() as session:
        r = session.execute(
            text("""SELECT job_id, job_type, status, attempts, locked_by, locked_at,
                           error_message, priority, covid,
                           available_at <= now() AS available_now
                      FROM job_queue WHERE job_id = :j"""),
            {"j": job_id},
        ).fetchone()
        return dict(r._mapping) if r else {}


def test_enqueue_and_claim_roundtrip() -> None:
    jt = TEST_PREFIX + "roundtrip"
    job_id = enqueue(jt, payload={"path": "/tmp/a.pdf"})
    assert job_id, "enqueue must return a job_id"
    assert _row(job_id)["status"] == "queued"

    job = claim_next(job_types=[jt])
    assert job and job["job_id"] == job_id, job
    assert job["payload"] == {"path": "/tmp/a.pdf"}, job["payload"]
    assert job["attempts"] == 1, "claiming counts as an attempt"

    after = _row(job_id)
    assert after["status"] == "in_progress" and after["locked_by"] == job["worker"]
    assert after["locked_at"] is not None

    complete(job_id)
    done = _row(job_id)
    assert done["status"] == "done"
    assert done["locked_by"] is None and done["locked_at"] is None, (
        "a finished job must not still look leased")
    print("PASS: enqueue -> claim -> complete, lease taken and released")


def test_duplicate_live_work_cannot_be_enqueued() -> None:
    """Migration 0042's whole purpose. The drop folder rescans and stages
    re-run; neither may produce two live jobs doing the same thing at once."""
    jt = TEST_PREFIX + "dup"
    first = enqueue(jt, payload={"path": "/tmp/same.pdf"})
    second = enqueue(jt, payload={"path": "/tmp/same.pdf"})
    assert first and second is None, f"duplicate must return None, got {second}"

    # A DIFFERENT path is different work and must go in.
    other = enqueue(jt, payload={"path": "/tmp/other.pdf"})
    assert other, "a different path is different work"

    # covid-keyed jobs are unique per (job_type, covid) independently of path.
    a = enqueue(jt + "_covid", covid=5838)
    b = enqueue(jt + "_covid", covid=5838)
    c = enqueue(jt + "_covid", covid=5839)
    assert a and b is None and c, f"{a=} {b=} {c=}"
    print("PASS: identical live work is refused by the database, not by a racy pre-check")


def test_finished_work_can_be_requeued() -> None:
    """Only LIVE work is unique. Re-running a stage after fixing something is
    normal, so a done/error/needs_review row must not block it forever."""
    jt = TEST_PREFIX + "requeue"
    for finisher in (complete, lambda j: hold_for_review(j, "needs a human"),
                     lambda j: hold_for_captcha(j)):
        job_id = enqueue(jt, covid=5838)
        assert job_id, "must be enqueueable after the previous one finished"
        claim_next(job_types=[jt])
        finisher(job_id)
    assert enqueue(jt, covid=5838), "a terminal row must not block re-enqueueing"
    print("PASS: done / needs_review / captcha_pending rows do not block re-enqueueing")


def test_concurrent_claims_never_hand_out_the_same_job() -> None:
    """FOR UPDATE SKIP LOCKED, exercised for real: 8 threads race for 4 jobs.
    Every job must go to exactly one worker and 4 threads must come back
    empty -- not block, and not double-claim."""
    jt = TEST_PREFIX + "race"
    ids = {enqueue(jt, covid=c) for c in (5838, 5839, 5963, 3028)}
    assert len(ids) == 4 and None not in ids

    claimed, empties, errors = [], [], []
    lock = threading.Lock()

    def grab() -> None:
        try:
            job = claim_next(job_types=[jt])
        except Exception as e:  # a deadlock or serialization failure is a real bug
            with lock:
                errors.append(e)
            return
        with lock:
            (claimed if job else empties).append(job)

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent claims raised: {errors}"
    got = [j["job_id"] for j in claimed]
    assert sorted(got) == sorted(ids), f"expected each job once, got {got} for {ids}"
    assert len(got) == len(set(got)), f"a job was claimed twice: {got}"
    assert len(empties) == 4, f"expected 4 empty claims, got {len(empties)}"
    print(f"PASS: 8 threads racing for 4 jobs -- each claimed exactly once, "
          f"{len(empties)} came back empty")


def test_priority_then_job_id_ordering() -> None:
    jt = TEST_PREFIX + "prio"
    low = enqueue(jt, covid=5838, priority=200)
    high = enqueue(jt, covid=5839, priority=10)
    mid_first = enqueue(jt, covid=5963, priority=100)
    mid_second = enqueue(jt, covid=3028, priority=100)
    order = []
    while (job := claim_next(job_types=[jt])):
        order.append(job["job_id"])
    assert order == [high, mid_first, mid_second, low], f"{order} vs {[high, mid_first, mid_second, low]}"
    print("PASS: claimed in priority order, FIFO by job_id within a priority")


def test_failure_retries_with_backoff_then_gives_up() -> None:
    jt = TEST_PREFIX + "fail"
    job_id = enqueue(jt, covid=5838)
    job = claim_next(job_types=[jt])

    status = fail(job_id, RuntimeError("portal timed out"), attempts=job["attempts"])
    assert status == "queued", status
    row = _row(job_id)
    assert row["status"] == "queued" and "portal timed out" in row["error_message"]
    assert not row["available_now"], (
        f"a retry must be deferred by the backoff, not immediately claimable")
    assert claim_next(job_types=[jt]) is None, "backed-off work must not be claimable yet"

    # Exhausted: reported, not retried forever.
    status = fail(job_id, "still broken", attempts=MAX_ATTEMPTS)
    assert status == "error", status
    row = _row(job_id)
    assert row["status"] == "error" and "gave up after" in row["error_message"]
    assert row["locked_by"] is None
    print(f"PASS: failure backs off {RETRY_BACKOFF_SECONDS[0]}s then gives up at "
          f"{MAX_ATTEMPTS} attempts, reported not retried")


def test_review_is_not_a_failure() -> None:
    """hold_for_review must not be retried: retrying cannot answer the
    question, and burning attempts would bury the reason -- which IS the
    payload of this outcome -- under a retries-exhausted message."""
    jt = TEST_PREFIX + "review"
    job_id = enqueue(jt, covid=5838)
    claim_next(job_types=[jt])
    reason = "POB could not be georeferenced with confidence"
    hold_for_review(job_id, reason)
    row = _row(job_id)
    assert row["status"] == "needs_review" and row["error_message"] == reason
    assert claim_next(job_types=[jt]) is None, "review work must not be re-claimed"
    print("PASS: needs_review keeps its reason verbatim and is never re-claimed")


def test_reclaim_returns_a_dead_workers_job() -> None:
    jt = TEST_PREFIX + "reclaim"
    job_id = enqueue(jt, covid=5838)
    job = claim_next(job_types=[jt])
    assert _row(job_id)["status"] == "in_progress"

    # Nothing reclaimed while the lease is fresh.
    assert not [r for r in reclaim_stale(lease_seconds=3600) if r["job_id"] == job_id], (
        "a fresh lease must not be reclaimed")

    # Age the lease rather than sleeping through a real one.
    with get_session() as session:
        session.execute(text("UPDATE job_queue SET locked_at = now() - interval '9 hours' "
                             "WHERE job_id = :j"), {"j": job_id})
    reclaimed = [r for r in reclaim_stale(lease_seconds=3600) if r["job_id"] == job_id]
    assert len(reclaimed) == 1, reclaimed
    assert job["worker"] in reclaimed[0]["note"], (
        f"reclaim must name who held it, got {reclaimed[0]['note']!r}")
    row = _row(job_id)
    assert row["status"] == "queued" and row["locked_by"] is None
    assert claim_next(job_types=[jt]), "reclaimed work must be claimable again"
    print("PASS: an expired lease is reclaimed, names the dead worker, and is claimable again")


def test_county_scoped_claim() -> None:
    """Portal politeness: a worker pinned to one county must not pick up
    another's work, which is why job_queue_dequeue_idx leads with county."""
    jt = TEST_PREFIX + "county"
    with get_session() as session:
        counties = [r[0] for r in session.execute(
            text("SELECT county_fips FROM county ORDER BY county_fips LIMIT 2"))]
    if len(counties) < 2:
        print("SKIP: need two counties seeded")
        return
    a = enqueue(jt, covid=5838, county_fips=counties[0])
    b = enqueue(jt, covid=5839, county_fips=counties[1])
    assert a and b
    job = claim_next(job_types=[jt], county_fips=counties[1])
    assert job and job["job_id"] == b, f"claimed {job} instead of the {counties[1]} job"
    assert claim_next(job_types=[jt], county_fips=counties[1]) is None
    assert claim_next(job_types=[jt], county_fips=counties[0])["job_id"] == a
    print(f"PASS: a county-scoped claim takes only that county's work "
          f"({counties[1]} then {counties[0]})")


def test_job_type_filter_and_payload_validation() -> None:
    jt = TEST_PREFIX + "filter"
    mine = enqueue(jt, covid=5838)
    enqueue(TEST_PREFIX + "other", covid=5838)
    job = claim_next(job_types=[jt])
    assert job and job["job_id"] == mine
    assert claim_next(job_types=[jt]) is None, "must not reach into another job_type"

    try:
        enqueue(jt, payload=["not", "a", "dict"])  # type: ignore[arg-type]
    except ValueError as e:
        assert "must be a dict" in str(e)
    else:
        raise AssertionError("a non-dict payload must be refused, not written as ambiguous JSON")
    print("PASS: job_type filter is respected; a malformed payload is refused")


def test_depth_reports_the_queue() -> None:
    jt = TEST_PREFIX + "depth"
    enqueue(jt, covid=5838)
    rows = [r for r in depth() if r["job_type"] == jt]
    assert rows and rows[0]["status"] == "queued" and rows[0]["n"] == 1, rows
    assert rows[0]["next_available"] is not None
    print("PASS: depth() reports status/job_type counts and the next available time")


def test_worker_id_identifies_host_and_process() -> None:
    wid = worker_id()
    assert ":" in wid and wid.rsplit(":", 1)[1].isdigit(), wid
    print(f"PASS: worker_id names host and pid ({wid})")


if __name__ == "__main__":
    _cleanup()
    try:
        test_enqueue_and_claim_roundtrip()
        test_duplicate_live_work_cannot_be_enqueued()
        test_finished_work_can_be_requeued()
        test_concurrent_claims_never_hand_out_the_same_job()
        test_priority_then_job_id_ordering()
        test_failure_retries_with_backoff_then_gives_up()
        test_review_is_not_a_failure()
        test_reclaim_returns_a_dead_workers_job()
        test_county_scoped_claim()
        test_job_type_filter_and_payload_validation()
        test_depth_reports_the_queue()
        test_worker_id_identifies_host_and_process()
        print("\nall queue tests passed")
    finally:
        _cleanup()
