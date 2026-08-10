"""The work-queue side of covenant.job_queue: enqueue, claim, complete, fail.

app/queue/job_queue.py is a different thing that shares the table. It wraps a
single network call in retry-with-backoff and, when the retries are exhausted,
writes one status='error' row so the failure outlives whoever's terminal was
running it. It never enqueues anything and nothing ever claims what it writes.
This module is the actual queue: work is put in, workers take it out, and the
row tracks whose it is and what happened.

They coexist deliberately. A stage claimed from here will often make a network
call wrapped by there, and both write to job_queue -- one row for the unit of
work, possibly another for a network failure inside it. Reading the table, the
distinction is job_type plus whether anything ever claimed the row.

TRANSACTION DISCIPLINE (the thing to get right)
Every state transition here runs in its OWN committed transaction, never the
caller's. This is the same lesson job_queue.py's docstring records the hard
way, and it matters more here:

  - The lease must be visible to other workers the instant it is taken. Held
    inside the caller's open transaction, two workers can each believe they own
    the same job.
  - A job marked done inside the same transaction as the work means a failure
    rolls back the bookkeeping along with the work: attempts never increments,
    so a poison job is retried forever instead of being reported.
  - Conversely a stage's own data writes are NOT part of this module's
    transactions, so a crash between "work committed" and "job marked done"
    leaves the job to be reclaimed and re-run. That is safe only because every
    stage is idempotent, which this project already requires of them; a stage
    that is not idempotent cannot be queued here.

WHAT COUNTS AS FAILURE
Three distinct outcomes, because collapsing them loses the information that
decides what to do next:

  error         something broke. Retried with backoff until attempts run out,
                then left as 'error' for a human.
  needs_review  the work ran correctly and reached a question only a human can
                answer -- an unanchorable POB, an unresolved county. NOT a
                failure, never retried, and the reason is the point.
  captcha_pending  blocked on a portal challenge. Its own status since the
                initial schema, and never a bypass attempt.
"""
import json
import socket
import os

from sqlalchemy import text

from app.db.session import get_session

# How long a claimed job may go untouched before another worker may take it.
# Generous because a single stage here can legitimately run for a long time --
# app/llm/anchor_agent.py's own budget is ~90 minutes per attempt, and a
# Tesseract pass over a 26-page instrument at ~5-7s/page is minutes more. Too
# short and two workers duplicate expensive work; too long and a crashed
# worker's job sits idle. Reclaim is explicit (reclaim_stale), not automatic.
DEFAULT_LEASE_SECONDS = 4 * 60 * 60

# Retry backoff for status='error', in seconds, indexed by attempts already
# made. Far longer than job_queue.py's in-process (3, 8, 20, 45) because these
# are different failures: that module has already absorbed the transient blip
# before it ever reports one, so a job failing HERE has a real problem, and
# re-running it seconds later just burns the attempt.
RETRY_BACKOFF_SECONDS = (60, 300, 1800, 7200)
MAX_ATTEMPTS = 5

LIVE_STATUSES = ("queued", "in_progress")
TERMINAL_STATUSES = ("done", "error", "needs_review", "captcha_pending")


def worker_id() -> str:
    """Identifies the claiming process in locked_by. Host plus pid, so a stale
    lease can be traced to a machine and a process that is or isn't still
    alive -- the question anyone looks at reclaim_stale to answer."""
    return f"{socket.gethostname()}:{os.getpid()}"


def enqueue(job_type: str, *, covid: int | None = None, county_fips: str | None = None,
            payload: dict | None = None, priority: int = 100,
            available_in_seconds: int = 0) -> int | None:
    """Put work in the queue. Returns the new job_id, or None when identical
    live work is already queued or in progress.

    That None is the normal, expected answer, not an error: the drop folder
    rescans, a stage re-runs, a covenant is dropped twice. Uniqueness is
    enforced by migration 0042's partial indexes rather than by checking first,
    because a check-then-insert races -- two workers scanning the same folder
    at the same moment would both see nothing and both insert.

    Lower `priority` is claimed first (the column defaults to 100).
    """
    if payload is not None and not isinstance(payload, dict):
        raise ValueError(f"payload must be a dict, got {type(payload).__name__}")
    with get_session() as session:
        row = session.execute(
            text("""
                INSERT INTO job_queue (job_type, covid, county_fips, payload, status,
                                       priority, available_at)
                VALUES (:job_type, :covid, :county_fips, (:payload)::jsonb, 'queued',
                        :priority, now() + make_interval(secs => :delay))
                ON CONFLICT DO NOTHING
                RETURNING job_id
            """),
            {"job_type": job_type, "covid": covid, "county_fips": county_fips,
             "payload": json.dumps(payload or {}), "priority": priority,
             "delay": available_in_seconds},
        ).fetchone()
        return row.job_id if row else None


def claim_next(*, job_types: list[str] | None = None, county_fips: str | None = None,
               worker: str | None = None) -> dict | None:
    """Take the next available job and lease it, or None if there is nothing to
    do. Committed before returning, so the lease is immediately visible.

    FOR UPDATE SKIP LOCKED is what makes several workers safe on one table:
    each skips rows another has locked instead of blocking behind them. Ordered
    by priority then job_id -- job_id rather than created_at because two rows
    inserted in the same transaction share a timestamp and FIFO would become
    arbitrary among them.

    `county_fips` exists for portal politeness: job_queue_dequeue_idx leads
    with (status, county_fips) precisely so a worker can be pinned to one
    county's recorder and the per-portal concurrency stays at one or two.
    """
    worker = worker or worker_id()
    with get_session() as session:
        row = session.execute(
            text("""
                WITH next AS (
                    SELECT job_id FROM job_queue
                     WHERE status = 'queued'
                       AND available_at <= now()
                       -- CAST(...) rather than :param::type: SQLAlchemy's bind
                       -- parser reads the leading colon of a "::" cast as the
                       -- start of another parameter and leaves the first one
                       -- unsubstituted.
                       AND (CAST(:job_types AS text[]) IS NULL OR job_type = ANY(CAST(:job_types AS text[])))
                       AND (CAST(:county_fips AS char(5)) IS NULL OR county_fips = CAST(:county_fips AS char(5)))
                     ORDER BY priority, job_id
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                )
                UPDATE job_queue j
                   SET status = 'in_progress', locked_by = :worker, locked_at = now(),
                       attempts = j.attempts + 1, updated_at = now()
                  FROM next
                 WHERE j.job_id = next.job_id
             RETURNING j.job_id, j.job_type, j.covid, j.county_fips, j.payload,
                       j.attempts, j.priority
            """),
            {"job_types": job_types, "county_fips": county_fips, "worker": worker},
        ).fetchone()
        if row is None:
            return None
        return {"job_id": row.job_id, "job_type": row.job_type, "covid": row.covid,
                "county_fips": row.county_fips, "payload": row.payload or {},
                "attempts": row.attempts, "priority": row.priority, "worker": worker}


def _finish(job_id: int, status: str, *, error_message: str | None = None,
            available_in_seconds: int | None = None) -> None:
    """The one place a job leaves in_progress. Clears the lease on every
    terminal status so a 'done' or 'error' row never looks like it is still
    held by a worker."""
    with get_session() as session:
        session.execute(
            text("""
                UPDATE job_queue
                   SET status = :status,
                       error_message = :error_message,
                       locked_by = NULL, locked_at = NULL,
                       available_at = CASE WHEN :delay IS NULL THEN available_at
                                           ELSE now() + make_interval(secs => :delay) END,
                       updated_at = now()
                 WHERE job_id = :job_id
            """),
            {"job_id": job_id, "status": status, "error_message": error_message,
             "delay": available_in_seconds},
        )


def complete(job_id: int) -> None:
    _finish(job_id, "done")


def hold_for_review(job_id: int, reason: str) -> None:
    """The work ran and reached a question a human has to answer. Deliberately
    not 'error' and never retried -- retrying cannot produce an answer, and
    burning attempts on it would eventually bury the reason under a generic
    retries-exhausted message."""
    _finish(job_id, "needs_review", error_message=reason)


def hold_for_captcha(job_id: int, reason: str = "portal presented a challenge") -> None:
    _finish(job_id, "captcha_pending", error_message=reason)


def fail(job_id: int, error: BaseException | str, *, attempts: int,
         max_attempts: int = MAX_ATTEMPTS) -> str:
    """Report a broken job. Returns the status it was left in: 'queued' when
    there are attempts left (available_at pushed forward by the backoff), or
    'error' when there are not.

    `attempts` is the count claim_next already recorded, passed back in rather
    than re-read, so the decision uses the same number the row holds and cannot
    drift if something else touched the row.
    """
    message = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    if attempts >= max_attempts:
        _finish(job_id, "error", error_message=f"{message} (gave up after {attempts} attempts)")
        return "error"
    delay = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
    _finish(job_id, "queued", error_message=message, available_in_seconds=delay)
    return "queued"


def reclaim_stale(*, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> list[dict]:
    """Return jobs whose worker died to the queue. Explicit rather than
    automatic, so nothing silently re-runs expensive work while the original
    worker is in fact still going -- a caller should know it is doing this.

    Returns what was reclaimed, including who held it, because a worker that
    keeps losing leases is a problem worth seeing rather than a statistic.
    """
    with get_session() as session:
        rows = session.execute(
            text("""
                UPDATE job_queue
                   SET status = 'queued', locked_by = NULL, locked_at = NULL,
                       error_message = 'lease expired; reclaimed from ' || COALESCE(locked_by, '?'),
                       updated_at = now()
                 WHERE status = 'in_progress'
                   AND locked_at < now() - make_interval(secs => :lease)
             RETURNING job_id, job_type, covid, attempts, error_message
            """),
            {"lease": lease_seconds},
        ).fetchall()
        return [{"job_id": r.job_id, "job_type": r.job_type, "covid": r.covid,
                 "attempts": r.attempts, "note": r.error_message} for r in rows]


def depth() -> list[dict]:
    """Queue state by status and job_type -- what a worker prints on start and
    what anyone asks first when the pipeline seems stuck."""
    with get_session() as session:
        rows = session.execute(
            text("""
                SELECT status, job_type, count(*) AS n,
                       min(available_at) AS next_available
                  FROM job_queue
                 GROUP BY status, job_type
                 ORDER BY status, job_type
            """)
        ).fetchall()
        return [{"status": r.status, "job_type": r.job_type, "n": r.n,
                 "next_available": r.next_available} for r in rows]
