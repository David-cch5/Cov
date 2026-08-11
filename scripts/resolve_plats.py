"""Look up the recorded plats for every tract that still has unlinked parcels.

app/gis/plat_link.py's measurement said the links were not missing, the plats
were: 19 subdivisions across 8 counties have parcels but no plat row, and no
amount of matching creates a link to a document nobody has fetched. This drives
the fetch, tract by tract, through the same resolve_plats_for_tract the pipeline
already uses -- so the rows, sections, provenance and covenant notes come out
identical to the ones already on record.

Usage:
  python3 scripts/resolve_plats.py --list                 what would run, and why
  python3 scripts/resolve_plats.py --county 48355         one county (portal politeness)
  python3 scripts/resolve_plats.py --covid 5838           one covenant
  python3 scripts/resolve_plats.py --all                  every registered county

One county at a time is the default habit here: every recorder registry entry in
this project says workers_allowed=1, and these are counties' own public systems.

A county with no county_recorder_registry row is REPORTED, not skipped silently
-- Douglas CO, Llano, Travis and Hunt hold real parcels whose plats simply
cannot be searched until someone registers a recorder for them, and that is a
finding about coverage rather than an error.
"""
import argparse
import sys
import traceback

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import get_session
from app.gis.plat_tracking import resolve_plats_for_tract
from app.queue.job_queue import JobFailed

WORKLIST_SQL = """
    SELECT pc.covid, pc.tract_no, c.county_fips, ct.county_name,
           count(DISTINCT p.apn) AS unlinked,
           (r.county_fips IS NOT NULL) AS has_recorder
      FROM parcel_covenant pc
      JOIN parcel p ON p.county_fips = pc.county_fips AND p.apn = pc.apn
      JOIN covenant c ON c.covid = pc.covid
      LEFT JOIN county ct ON ct.county_fips = c.county_fips
      LEFT JOIN county_recorder_registry r ON r.county_fips = c.county_fips
     WHERE p.plat_id IS NULL AND p.recited_legal_description IS NOT NULL
       AND (:cf IS NULL OR c.county_fips = :cf)
       AND (:covid IS NULL OR pc.covid = :covid)
     GROUP BY 1, 2, 3, 4, 6
     ORDER BY 5 DESC
"""


def worklist(county_fips=None, covid=None) -> list:
    with get_session() as session:
        return session.execute(text(WORKLIST_SQL),
                               {"cf": county_fips, "covid": covid}).fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--county", default=None, help="county_fips to run")
    ap.add_argument("--covid", type=int, default=None)
    ap.add_argument("--all", action="store_true", help="every county with a recorder")
    ap.add_argument("--list", action="store_true", help="show the worklist, fetch nothing")
    args = ap.parse_args(argv)
    if not (args.county or args.covid or args.all or args.list):
        ap.error("choose --list, --county, --covid or --all")

    rows = worklist(args.county, args.covid)
    unreachable = [r for r in rows if not r.has_recorder]
    todo = [r for r in rows if r.has_recorder]

    print(f"{len(rows)} tract(s) with unlinked parcels, "
          f"{sum(r.unlinked for r in rows):,} parcels")
    for r in rows:
        mark = " " if r.has_recorder else "!"
        print(f" {mark} covid {r.covid:<6} tract {r.tract_no}  "
              f"{(r.county_name or r.county_fips)[:12]:<13}{r.unlinked:>5} unlinked")
    if unreachable:
        print(f"\n! {len(unreachable)} tract(s) have no county_recorder_registry entry "
              f"({sum(r.unlinked for r in unreachable)} parcels) -- their plats cannot be "
              f"searched until a recorder is registered for: "
              f"{', '.join(sorted({(r.county_name or r.county_fips) for r in unreachable}))}")
    if args.list:
        return 0

    ok = failed = 0
    for r in todo:
        print(f"\n=== covid {r.covid} tract {r.tract_no} "
              f"({r.county_name or r.county_fips}, {r.unlinked} unlinked) ===", flush=True)
        try:
            with get_session() as session:
                result = resolve_plats_for_tract(session, r.covid, r.tract_no)
                session.commit()
            print("   ", {k: v for k, v in result.items() if not isinstance(v, (list, dict))},
                  flush=True)
            ok += 1
        except JobFailed as e:
            # Already durably recorded in job_queue by run_with_job_queue, which is
            # the point of letting it through rather than catching it deeper: a
            # county whose portal is down stays visible in `pipeline status`.
            print(f"    PORTAL FAILED (recorded as a job_queue row): {e}", flush=True)
            failed += 1
        except Exception as e:
            print(f"    ERROR {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
            failed += 1
    print(f"\n{ok} tract(s) resolved, {failed} failed")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
