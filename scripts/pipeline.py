"""Drive the covenant pipeline: scan the drop folder, run queued work, see state.

This is the entry point that replaces hand-writing a script per covenant. The
eleven covid-specific scripts still in this directory -- commit_covid3346_tract1,
manual_commit_covid4780_tract1, reanchor_covid5838_tract1,
resolve_covid5839_tract2 and the rest -- are what this exists to stop needing.

Usage:
  python3 scripts/pipeline.py scan                 enqueue intake for new drops
  python3 scripts/pipeline.py work                 run queued jobs until drained
  python3 scripts/pipeline.py work --county 48339  ... only this county's jobs
  python3 scripts/pipeline.py work --max 1         ... one job, then stop
  python3 scripts/pipeline.py run                  scan, then work
  python3 scripts/pipeline.py maps --now           build every covenant's formation map
  python3 scripts/pipeline.py status               queue depth and stuck covenants
  python3 scripts/pipeline.py stage <name> <covid> run one stage by hand

`stage` is the deliberate manual path: re-running a covenant that stopped at
needs_review once its question is answered, or running chain_of_title /
fee_compute, which are registered but never auto-enqueued because the encumbered
land has to be right first.
"""
import argparse
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import get_session
from app.pipeline.runner import enqueue_stage, run_worker, scan_drop_folder
from app.pipeline.stages import MANUAL_STAGES, STAGE_ORDER, STAGES
from app.queue.queue import depth


def cmd_scan(args) -> int:
    result = scan_drop_folder(args.drop_dir)
    return 0 if result["found"] or True else 1


def cmd_work(args) -> int:
    ran = run_worker(county_fips=args.county, max_jobs=args.max)
    return 1 if any(r["outcome"] == "error" for r in ran) else 0


def cmd_run(args) -> int:
    scan_drop_folder(args.drop_dir)
    return cmd_work(args)


def cmd_maps(args) -> int:
    """Enqueue publish_map for every covenant a map can be built for.

    Separate from `work` because the chain only reaches publish_map on a covenant
    that got all the way through reconcile -- and a map is often most useful on one
    still sitting at needs_review, where seeing the census is how somebody decides.
    """
    from app.gis.formation_map import eligible_covids

    with get_session() as session:
        covids = eligible_covids(session)
    queued, already = [], []
    for covid in covids:
        (queued if enqueue_stage("publish_map", covid) else already).append(covid)
    print(f"{len(covids)} covenant(s) eligible -- {len(queued)} queued, "
          f"{len(already)} already in flight")
    if args.now:
        ran = run_worker(job_types=["pipeline_publish_map"], verbose=True)
        return 1 if any(r["outcome"] == "error" for r in ran) else 0
    return 0


def cmd_status(args) -> int:
    rows = depth()
    if not rows:
        print("queue is empty")
    else:
        print(f"{'status':<16}{'job_type':<32}{'n':>5}  next available")
        for r in rows:
            print(f"{r['status']:<16}{r['job_type']:<32}{r['n']:>5}  {r['next_available']}")

    with get_session() as session:
        print("\ncovenants by status:")
        for r in session.execute(text(
                "SELECT status, count(*) FROM covenant GROUP BY 1 ORDER BY 2 DESC")):
            print(f"  {r[0] or '(none)':<20}{r[1]}")

        stuck = session.execute(text("""
            SELECT job_id, job_type, covid, left(coalesce(error_message, ''), 110) AS why
              FROM job_queue
             WHERE status IN ('needs_review', 'error', 'captcha_pending')
             ORDER BY updated_at DESC LIMIT 15
        """)).fetchall()
        if stuck:
            print("\nwaiting on a human (most recent first):")
            for r in stuck:
                print(f"  job {r.job_id:<7}{r.job_type:<30}covid {r.covid or '-':<8}{r.why}")
    return 0


def cmd_stage(args) -> int:
    if args.name not in STAGES:
        print(f"unknown stage {args.name!r}. Pipeline: {', '.join(STAGE_ORDER)}. "
              f"Manual: {', '.join(MANUAL_STAGES)}")
        return 2
    job_id = enqueue_stage(args.name, args.covid)
    if job_id is None:
        print(f"{args.name} for covid {args.covid} is already queued or in progress")
        return 0
    print(f"queued {args.name} for covid {args.covid} (job {job_id})")
    if args.now:
        ran = run_worker(max_jobs=1)
        return 1 if any(r["outcome"] == "error" for r in ran) else 0
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--drop-dir", default=None, help="override the watched drop folder")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan", help="enqueue intake jobs for new dropped PDFs")

    p_work = sub.add_parser("work", help="run queued jobs until the queue drains")
    p_work.add_argument("--county", default=None, help="only this county_fips (portal politeness)")
    p_work.add_argument("--max", type=int, default=None, help="stop after this many jobs")

    p_run = sub.add_parser("run", help="scan, then work")
    p_run.add_argument("--county", default=None)
    p_run.add_argument("--max", type=int, default=None)

    p_maps = sub.add_parser("maps", help="build the formation map for every eligible covenant")
    p_maps.add_argument("--now", action="store_true", help="also run the queued map jobs")

    sub.add_parser("status", help="queue depth, covenant statuses, and what is stuck")

    p_stage = sub.add_parser("stage", help="enqueue one stage for one covenant")
    p_stage.add_argument("name")
    p_stage.add_argument("covid", type=int)
    p_stage.add_argument("--now", action="store_true", help="also run it immediately")

    args = parser.parse_args(argv)
    return {"scan": cmd_scan, "work": cmd_work, "run": cmd_run, "maps": cmd_maps,
            "status": cmd_status, "stage": cmd_stage}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
