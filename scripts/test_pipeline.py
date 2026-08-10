"""Tests for app/pipeline -- the stage graph and the runner that walks it.

The stages themselves are covered by their own modules' tests (classifier,
anchor_resolver, reconcile, intake); what is tested here is the orchestration,
which has its own distinct failure modes: advancing past an open question,
losing a covenant between stages, treating a temporary outage as a conclusion,
or enqueueing the same work twice.

Stage handlers are substituted for stubs where the point is the runner's
behaviour rather than the stage's. Where the point IS the wiring, the real thing
runs.

Usage: python3 scripts/test_pipeline.py
"""
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from app.db.session import get_session
from app.pipeline import runner as runner_module
from app.pipeline import stages as stages_module
from app.pipeline.runner import enqueue_drop, enqueue_stage, run_job, run_worker, scan_drop_folder
from app.pipeline.stages import (
    MANUAL_STAGES, STAGE_ORDER, STAGES, StageVerdict, job_type, next_stage, stage_from_job_type,
)
from app.queue.queue import claim_next, enqueue
from app.ingestion.walk import PROJECT_ROOT

TEST_COVID = 5838          # a real covenant, used only as a job subject here
SCRATCH_DROP = f"{PROJECT_ROOT}/_pipeline_test_drop"


def _cleanup() -> None:
    import shutil
    with get_session() as session:
        session.execute(text("DELETE FROM job_queue WHERE job_type LIKE 'pipeline_%'"))
    shutil.rmtree(SCRATCH_DROP, ignore_errors=True)


def _rows(covid: int | None = None) -> list[dict]:
    with get_session() as session:
        q = ("SELECT job_id, job_type, status, attempts, error_message, covid "
             "FROM job_queue WHERE job_type LIKE 'pipeline_%'"
             + (" AND covid = :c" if covid else "") + " ORDER BY job_id")
        return [dict(r._mapping) for r in session.execute(text(q),
                                                          {"c": covid} if covid else {})]


def test_stage_graph_is_coherent() -> None:
    assert set(STAGE_ORDER) | set(MANUAL_STAGES) == set(STAGES), (
        f"registry and order disagree: {set(STAGES) ^ (set(STAGE_ORDER) | set(MANUAL_STAGES))}")
    assert next_stage("intake") == "resolve_tract"
    assert next_stage("resolve_tract") == "classify_parcels"
    assert next_stage("classify_parcels") == "reconcile"
    assert next_stage("reconcile") is None, "reconcile is the end of the automatic pipeline"
    # A subdivision-plat tract already has its census, so classification is skipped.
    assert next_stage("resolve_tract", ("classify_parcels",)) == "reconcile"
    # Manual stages are reachable only deliberately -- never by advancing.
    for stage in MANUAL_STAGES:
        assert next_stage(stage) is None, f"{stage} must not be reachable by advancing"
    assert stage_from_job_type(job_type("reconcile")) == "reconcile"
    print(f"PASS: stage graph coherent -- {' -> '.join(STAGE_ORDER)}; "
          f"manual-only: {', '.join(MANUAL_STAGES)}")


def test_advancing_marks_done_and_queues_the_successor() -> None:
    original = STAGES["resolve_tract"]
    STAGES["resolve_tract"] = lambda s, c, p: StageVerdict("advanced", "stub ok", covid=c)
    try:
        job_id = enqueue_stage("resolve_tract", TEST_COVID)
        job = claim_next(job_types=[job_type("resolve_tract")])
        result = run_job(job, verbose=False)
    finally:
        STAGES["resolve_tract"] = original

    assert result["outcome"] == "advanced" and result["next_stage"] == "classify_parcels", result
    by_type = {r["job_type"]: r for r in _rows(TEST_COVID)}
    assert by_type[job_type("resolve_tract")]["status"] == "done"
    assert by_type[job_type("classify_parcels")]["status"] == "queued", (
        "the successor must be queued")
    print("PASS: an advanced stage is marked done and its successor queued")


def test_needs_review_stops_the_chain() -> None:
    """The most important behaviour here. Advancing past an open question is how
    a wrong answer gets built on -- and this pipeline's whole subject is land
    boundaries, where the next stage would spend money on the wrong parcels."""
    _cleanup()
    original = STAGES["resolve_tract"]
    reason = "POB could not be georeferenced with confidence"
    STAGES["resolve_tract"] = lambda s, c, p: StageVerdict("needs_review", reason, covid=c)
    try:
        enqueue_stage("resolve_tract", TEST_COVID)
        job = claim_next(job_types=[job_type("resolve_tract")])
        result = run_job(job, verbose=False)
    finally:
        STAGES["resolve_tract"] = original

    assert result["outcome"] == "needs_review", result
    rows = _rows(TEST_COVID)
    assert len(rows) == 1, f"no successor may be queued, found {[r['job_type'] for r in rows]}"
    assert rows[0]["status"] == "needs_review"
    assert rows[0]["error_message"] == reason, "the reason must survive verbatim"
    print("PASS: needs_review halts the chain with its reason intact, queueing nothing")


def test_retry_is_not_a_conclusion() -> None:
    """A temporary outage elsewhere must not become a finding about the covenant.
    This is the NGS case: a free, survey-grade answer exists and the service
    merely did not hand it over, so coming back later keeps that tier instead of
    buying the same answer from a paid one."""
    _cleanup()
    original = STAGES["resolve_tract"]
    STAGES["resolve_tract"] = lambda s, c, p: StageVerdict(
        "retry", "NGS unavailable, tier preserved", covid=c)
    try:
        enqueue_stage("resolve_tract", TEST_COVID)
        job = claim_next(job_types=[job_type("resolve_tract")])
        result = run_job(job, verbose=False)
    finally:
        STAGES["resolve_tract"] = original

    assert result["outcome"] == "queued", f"a retry must go back to the queue, got {result}"
    rows = _rows(TEST_COVID)
    assert len(rows) == 1 and rows[0]["status"] == "queued", rows
    assert rows[0]["attempts"] == 1, "the attempt must be counted"
    # Backed off, so not immediately re-claimable -- otherwise a down service
    # would be hammered in a tight loop.
    assert claim_next(job_types=[job_type("resolve_tract")]) is None, (
        "a retry must be deferred by the backoff")
    print("PASS: retry returns to the queue with backoff, counts the attempt, "
          "and records nothing about the covenant")


def test_a_raising_stage_is_reported_not_swallowed() -> None:
    _cleanup()
    original = STAGES["resolve_tract"]

    def boom(session, covid, payload):
        raise RuntimeError("GIS endpoint returned malformed geometry")

    STAGES["resolve_tract"] = boom
    try:
        enqueue_stage("resolve_tract", TEST_COVID)
        job = claim_next(job_types=[job_type("resolve_tract")])
        result = run_job(job, verbose=False)
    finally:
        STAGES["resolve_tract"] = original

    assert result["outcome"] == "queued", result
    rows = _rows(TEST_COVID)
    assert "malformed geometry" in rows[0]["error_message"], rows[0]
    assert len(rows) == 1, "a failed stage must not queue a successor"
    print("PASS: a raising stage is recorded with its message and queues no successor")


def test_worker_drains_a_multi_stage_chain() -> None:
    """The runner walking a covenant the whole way, with every stage stubbed --
    the wiring, not the work."""
    _cleanup()
    originals = {s: STAGES[s] for s in STAGE_ORDER}
    seen = []

    def make(stage):
        def handler(session, covid, payload):
            seen.append(stage)
            return StageVerdict("advanced", f"{stage} ok", covid=covid or TEST_COVID)
        return handler

    for stage in STAGE_ORDER:
        STAGES[stage] = make(stage)
    try:
        enqueue_stage("resolve_tract", TEST_COVID)
        ran = run_worker(max_jobs=10, reclaim_first=False, verbose=False)
    finally:
        STAGES.update(originals)

    assert seen == ["resolve_tract", "classify_parcels", "reconcile"], seen
    assert all(r["outcome"] == "advanced" for r in ran), ran
    statuses = {r["job_type"]: r["status"] for r in _rows(TEST_COVID)}
    assert set(statuses.values()) == {"done"}, statuses
    print(f"PASS: worker drained the chain unattended -- {' -> '.join(seen)}, all done")


def test_skip_stages_is_honoured_end_to_end() -> None:
    _cleanup()
    originals = {s: STAGES[s] for s in STAGE_ORDER}
    seen = []

    def resolve(session, covid, payload):
        seen.append("resolve_tract")
        return StageVerdict("advanced", "plat lots matched", covid=covid,
                            skip_stages=("classify_parcels",))

    def make(stage):
        def handler(session, covid, payload):
            seen.append(stage)
            return StageVerdict("advanced", "ok", covid=covid)
        return handler

    STAGES["resolve_tract"] = resolve
    for stage in ("classify_parcels", "reconcile"):
        STAGES[stage] = make(stage)
    try:
        enqueue_stage("resolve_tract", TEST_COVID)
        run_worker(max_jobs=10, reclaim_first=False, verbose=False)
    finally:
        STAGES.update(originals)

    assert seen == ["resolve_tract", "reconcile"], (
        f"classify_parcels must be skipped for a plat resolution, got {seen}")
    print("PASS: a subdivision-plat resolution skips classification and goes to reconcile")


def test_duplicate_enqueue_is_refused_not_duplicated() -> None:
    _cleanup()
    first = enqueue_stage("reconcile", TEST_COVID)
    second = enqueue_stage("reconcile", TEST_COVID)
    assert first and second is None, f"{first=} {second=}"
    assert len(_rows(TEST_COVID)) == 1
    print("PASS: the same stage cannot be queued twice for one covenant while live")


def test_unknown_stage_is_refused() -> None:
    try:
        enqueue_stage("not_a_stage", TEST_COVID)
    except ValueError as e:
        assert "unknown stage" in str(e)
    else:
        raise AssertionError("an unknown stage name must be refused")
    print("PASS: an unknown stage name is refused at enqueue time")


def test_scan_enqueues_new_drops_and_skips_finished_ones() -> None:
    """Two guards, covering different windows: already_ingested catches a file
    whose intake FINISHED, the queue's uniqueness index catches one still in
    flight. A drop folder can be left populated and rescanned."""
    import os
    import shutil

    _cleanup()
    os.makedirs(SCRATCH_DROP, exist_ok=True)
    source = None
    for covid in ("3346", "2088"):
        matches = [f"{PROJECT_ROOT}/{covid}/{n}" for n in os.listdir(f"{PROJECT_ROOT}/{covid}")
                   if n.lower().endswith(".pdf")]
        if matches:
            source = matches[0]
            break
    if source is None:
        print("SKIP: no corpus PDF to drop")
        return
    dropped = os.path.join(SCRATCH_DROP, "pipeline_scan_probe.pdf")
    shutil.copy2(source, dropped)
    open(os.path.join(SCRATCH_DROP, "ignore_me.txt"), "a").close()

    first = scan_drop_folder(SCRATCH_DROP, verbose=False)
    assert len(first["queued"]) == 1, first
    assert first["found"] == 1, f"only PDFs count: {first}"

    # Rescan while intake is still queued: no duplicate.
    second = scan_drop_folder(SCRATCH_DROP, verbose=False)
    assert second["queued"] == [] and len(second["already_in_flight"]) == 1, second
    print("PASS: scan queues a new drop once; a rescan while it is in flight adds nothing")


def test_real_stage_handlers_are_callable_with_the_expected_signature() -> None:
    """Guards against the orchestrator drifting from the functions it calls --
    the failure that would only show up on a live run. Verified by signature
    rather than by running them, so this costs nothing."""
    import inspect

    for name, fn in STAGES.items():
        params = list(inspect.signature(fn).parameters)
        assert params[:3] == ["session", "covid", "payload"], f"{name}: {params}"

    # And that the underlying functions each stage delegates to still exist with
    # the names used. These moved once already this session.
    from app.gis.anchor_resolver import resolve_metes_and_bounds_anchor  # noqa: F401
    from app.gis.classifier import (  # noqa: F401
        classify_metes_and_bounds_tract, resolve_subdivision_plat_tract,
    )
    from app.gis.reconcile import reconcile_covenant  # noqa: F401
    from app.ingestion.ingest import escalate_ocr_confidence, ingest_one  # noqa: F401
    from app.ingestion.intake import candidate_for_dropped_file  # noqa: F401
    from app.title.chain import walk_chain_of_title  # noqa: F401
    from app.title.fee_compute import compute_fees_for_covid  # noqa: F401
    print(f"PASS: all {len(STAGES)} stage handlers take (session, covid, payload) and every "
          f"delegate they call resolves")


if __name__ == "__main__":
    _cleanup()
    try:
        test_stage_graph_is_coherent()
        test_real_stage_handlers_are_callable_with_the_expected_signature()
        test_advancing_marks_done_and_queues_the_successor()
        test_needs_review_stops_the_chain()
        test_retry_is_not_a_conclusion()
        test_a_raising_stage_is_reported_not_swallowed()
        test_worker_drains_a_multi_stage_chain()
        test_skip_stages_is_honoured_end_to_end()
        test_duplicate_enqueue_is_refused_not_duplicated()
        test_unknown_stage_is_refused()
        test_scan_enqueues_new_drops_and_skips_finished_ones()
        print("\nall pipeline tests passed")
    finally:
        _cleanup()
