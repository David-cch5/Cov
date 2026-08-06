"""Reusable runner for app/gis/anchor_resolver.py's automated tiered anchor
resolution -- invoke for any covid/tract_no still missing a real tract.geom
(deterministic ties first, then Opus 5, then Fable 5, then the existing
approximate-placement fallback). Commits only if
resolve_metes_and_bounds_anchor's own auto-commit bar is met (independently
re-verified closure/area + a live parcel dry-run, never the model's own
self-reported confidence alone); otherwise prints the diagnostic result and
leaves the DB untouched.

Usage: python3 scripts/run_anchor_resolver.py <covid> [tract_no]
"""
import json
import os
import sys

# Works regardless of the caller's own current directory -- see
# manual_commit_covid4780_tract1.py's own note on why this can't be
# sys.path.insert(0, ".").
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import get_session
from app.gis.anchor_resolver import resolve_metes_and_bounds_anchor


def main() -> None:
    covid = int(sys.argv[1])
    tract_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    with get_session() as session:
        result = resolve_metes_and_bounds_anchor(session, covid=covid, tract_no=tract_no)
        if result.get("committed"):
            session.commit()
            print(f"COMMITTED: covid={covid} tract={tract_no}")
        else:
            print(f"NOT COMMITTED (nothing cleared the auto-commit bar): covid={covid} tract={tract_no}")
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
