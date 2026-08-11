"""Build a covenant's formation map by hand. The pipeline does this automatically
(app/pipeline/stages.py's publish_map stage); this is for one-offs and debugging.

Usage:
  python3 scripts/make_formation_map.py <covid> [--simplify-ft 10]
  python3 scripts/make_formation_map.py --all
"""
import argparse
import sys

sys.path.insert(0, ".")

from app.db.session import get_session
from app.gis.formation_map import build, eligible_covids


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("covid", nargs="?", type=int)
    ap.add_argument("--all", action="store_true", help="every covenant with geometry and a census")
    ap.add_argument("--simplify-ft", type=float, default=10.0)
    args = ap.parse_args(argv)
    if not args.covid and not args.all:
        ap.error("give a covid or --all")

    with get_session() as session:
        covids = eligible_covids(session) if args.all else [args.covid]
        for covid in covids:
            r = build(session, covid, args.simplify_ft)
            if r["written"]:
                print(f"covid {covid}: {r['written']} ({r['bytes']:,} bytes) -- "
                      f"{r['parcels']:,} parcels, {r['formation_dates']} dates, "
                      f"{r['undated']:,} undated, {r['from']}..{r['to']}")
            else:
                print(f"covid {covid}: not built -- {r['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
