"""The runner: claims pipeline jobs, runs one stage, decides what happens next.

Everything about the queue lives here and nothing about it lives in the stages,
so a stage is a plain function anyone can call directly -- which is how they are
tested and how a human re-runs one by hand.

WHAT HAPPENS TO A JOB, and why each case is separate:

  advanced      the stage's own transaction has committed. The job is marked done
                and the NEXT stage is enqueued -- in that order, so a crash in
                between leaves a done job with no successor rather than a
                successor for work that did not finish.
  needs_review  the covenant stops here, with the reason on the job row AND in
                covenant.review_reason. No successor is enqueued: advancing past
                an open question is how a wrong answer gets built on. Resuming is
                just enqueueing the stage again once somebody has answered, which
                works because every stage is re-runnable.
  retry         something outside this covenant was briefly unavailable. Back to
                'queued' with backoff, attempt counted. The covenant is untouched
                and no conclusion is recorded about it.
  raised        a real error. job_queue records it with backoff; after
                MAX_ATTEMPTS it stays 'error' for a human. The covenant keeps
                whatever state the failed stage's rolled-back transaction left,
                which is the state before the stage ran.

ONE JOB PER PROCESS AT A TIME. There is no in-process concurrency here, on
purpose: parallelism comes from running more workers (BUILD_SPEC's 4 Mac minis),
and the queue is already safe for that -- claim_next uses FOR UPDATE SKIP
LOCKED, and per-portal politeness is a county-scoped worker, not a semaphore.
Threads inside one worker would add a second, weaker concurrency story next to a
working one.
"""
import traceback

from app.db.session import get_session
from app.pipeline.stages import (
    MANUAL_STAGES, STAGE_ORDER, STAGES, StageVerdict, job_type, next_stage,
    stage_from_job_type,
)
from app.queue.queue import (
    claim_next, complete, enqueue, fail, hold_for_review, reclaim_stale, worker_id,
)

PIPELINE_JOB_TYPES = [job_type(s) for s in (*STAGE_ORDER, *MANUAL_STAGES)]


def enqueue_drop(path: str, *, priority: int = 100) -> int | None:
    """Queue a dropped file for intake. Returns None when it is already queued
    or in progress -- the ordinary answer when a folder is rescanned, not an
    error. Enforced by migration 0042's partial unique index on
    (job_type, payload->>'path'), so two watchers scanning at the same instant
    cannot both win."""
    return enqueue(job_type("intake"), payload={"path": path}, priority=priority)


def enqueue_stage(stage: str, covid: int, *, county_fips: str | None = None,
                  priority: int = 100, payload: dict | None = None) -> int | None:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage!r}; known: {sorted(STAGES)}")
    return enqueue(job_type(stage), covid=covid, county_fips=county_fips,
                   payload=payload or {}, priority=priority)


def _county_of(covid: int | None) -> str | None:
    """The county a job belongs to, so a county-scoped worker can pick it up.
    Read here rather than carried in the payload because the payload is written
    once and a covenant's county can be corrected later."""
    if covid is None:
        return None
    from sqlalchemy import text
    with get_session() as session:
        return session.execute(
            text("SELECT county_fips FROM covenant WHERE covid = :c"), {"c": covid},
        ).scalar()


def run_job(job: dict, *, verbose: bool = True) -> dict:
    """Run one claimed job to a conclusion. Returns what happened.

    The stage runs inside its own session so its data writes commit or roll back
    as one unit; the job's own bookkeeping is separate (app/queue/queue.py opens
    its own transaction per transition) and deliberately so.
    """
    stage = stage_from_job_type(job["job_type"])
    handler = STAGES.get(stage)
    if handler is None:
        hold_for_review(job["job_id"], f"no handler for stage {stage!r}")
        return {"stage": stage, "outcome": "needs_review", "note": "unknown stage"}

    label = f"covid {job['covid']}" if job["covid"] else job["payload"].get("path", "?")
    if verbose:
        print(f"[{stage}] {label} (job {job['job_id']}, attempt {job['attempts']})", flush=True)

    try:
        with get_session() as session:
            verdict: StageVerdict = handler(session, job["covid"], job["payload"] or {})
    except Exception as exc:                       # noqa: BLE001 -- reported, never swallowed
        if verbose:
            print(f"  ! {type(exc).__name__}: {exc}", flush=True)
            print(traceback.format_exc(), flush=True)
        status = fail(job["job_id"], exc, attempts=job["attempts"])
        return {"stage": stage, "outcome": status, "note": f"{type(exc).__name__}: {exc}"}

    covid = verdict.covid if verdict.covid is not None else job["covid"]

    if verdict.status == "retry":
        status = fail(job["job_id"], f"retry: {verdict.note}", attempts=job["attempts"])
        if verbose:
            print(f"  ~ retry ({status}): {verdict.note}", flush=True)
        return {"stage": stage, "outcome": status, "covid": covid, "note": verdict.note}

    if verdict.status == "needs_review":
        hold_for_review(job["job_id"], verdict.note)
        if verbose:
            print(f"  = needs_review: {verdict.note}", flush=True)
        return {"stage": stage, "outcome": "needs_review", "covid": covid, "note": verdict.note}

    # advanced. Mark done BEFORE enqueueing the successor: a crash between the
    # two leaves a finished job with no successor, which a rescan or a manual
    # enqueue fixes. The other order would leave a successor queued for work that
    # is not actually recorded as complete.
    complete(job["job_id"])
    following = next_stage(stage, verdict.skip_stages)
    enqueued = None
    if following and covid is not None:
        enqueued = enqueue_stage(following, covid, county_fips=_county_of(covid))
    if verbose:
        onward = (f" -> {following}" + (" (already queued)" if following and enqueued is None else "")
                  if following else " -> done, end of pipeline")
        skipped = f" [skipped {', '.join(verdict.skip_stages)}]" if verdict.skip_stages else ""
        print(f"  + {verdict.note}{skipped}{onward}", flush=True)
    return {"stage": stage, "outcome": "advanced", "covid": covid, "note": verdict.note,
            "next_stage": following, "next_job_id": enqueued}


def run_worker(*, job_types: list[str] | None = None, county_fips: str | None = None,
               max_jobs: int | None = None, reclaim_first: bool = True,
               verbose: bool = True) -> list[dict]:
    """Claim and run jobs until the queue is empty (or max_jobs is reached).

    Drains rather than polling forever: this is called by a script or a cron, and
    a process that exits when there is nothing to do is easier to reason about
    than a daemon. Returns one record per job run.
    """
    if reclaim_first:
        # A worker starting up is the natural moment to notice jobs a previous
        # one died holding. Explicit, and it reports what it took rather than
        # doing it silently -- a worker that keeps losing leases is a problem to
        # see, not a statistic.
        for row in reclaim_stale():
            if verbose:
                print(f"[reclaim] job {row['job_id']} ({row['job_type']}, covid {row['covid']}): "
                      f"{row['note']}", flush=True)

    me = worker_id()
    ran: list[dict] = []
    while max_jobs is None or len(ran) < max_jobs:
        job = claim_next(job_types=job_types or PIPELINE_JOB_TYPES,
                         county_fips=county_fips, worker=me)
        if job is None:
            break
        ran.append(run_job(job, verbose=verbose))
    if verbose:
        counts: dict[str, int] = {}
        for r in ran:
            counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        print(f"\n[worker {me}] {len(ran)} job(s): "
              f"{', '.join(f'{k}={v}' for k, v in sorted(counts.items())) or 'nothing to do'}",
              flush=True)
    return ran


def scan_drop_folder(drop_dir: str | None = None, *, verbose: bool = True) -> dict:
    """Enqueue an intake job for every PDF waiting in the drop folder.

    Skips files already ingested, so the folder can be left populated and
    rescanned without re-running finished work. Two guards, not one, because
    they cover different windows: already_ingested catches a file whose intake
    FINISHED, and the queue's own uniqueness index catches one whose intake is
    still queued or running.
    """
    from app.ingestion.intake import DROP_DIR, already_ingested, pending_drops

    drop_dir = drop_dir or DROP_DIR
    found = pending_drops(drop_dir)
    queued, skipped_done, skipped_live = [], [], []
    with get_session() as session:
        for path in found:
            existing = already_ingested(session, path)
            if existing is not None:
                skipped_done.append((path, existing))
                continue
            job_id = enqueue_drop(path)
            (queued if job_id else skipped_live).append(path)

    if verbose:
        print(f"[scan] {drop_dir}: {len(found)} PDF(s) -- {len(queued)} queued, "
              f"{len(skipped_done)} already ingested, {len(skipped_live)} already in flight",
              flush=True)
        for path, covid in skipped_done:
            print(f"  already ingested as covid {covid}: {path}", flush=True)
    return {"found": len(found), "queued": queued,
            "already_ingested": skipped_done, "already_in_flight": skipped_live}
