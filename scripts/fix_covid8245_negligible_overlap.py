"""One-off correction: remove covid 8245 tract 1's three square-centimetre
"matches" from the encumbered-parcel census, and supersede the chain-of-title
transfers recorded against them.

WHAT WAS WRONG
covid 8245 tract 1 (the "Alore Center" commercial tract, Montgomery County) had
6 matched parcels. Three of them intersect the tract polygon by 0.01-0.06 m2 --
square centimetres, where two independently-surveyed boundaries cross by a
hair:

    apn 129592  0.0104 m2  OAK RIDGE NORTH 05, LOT 529   ZAPH, STANLEY & LINDA
    apn 129590  0.0517 m2  OAK RIDGE NORTH 05, LOT 527   MILLWEE, JOSEPH L & MISTY L
    apn 129591  0.0639 m2  Oak Ridge North 05, Lot 528   FKH SFR C2 LP-FIRSTKEY HOMES

These are residential lots in a different subdivision, and they were recorded
as subject to this covenant's 1% transfer fee. Two of them had already been
chain-walked, producing 6 recorded, NON-exempt transfers -- i.e. conveyances
staged to become fee liabilities against homeowners whose land this covenant
does not encumber. No fee_collection, price_estimate, event or
fee_payoff_statement rows had been created yet, so nothing was ever claimed.

WHY IT WASN'T CAUGHT
overlap_fraction alone cannot distinguish these from a real narrow clip: a 2%
clip off a 100-acre parcel is also a small fraction, but ~3,200 m2 of genuinely
encumbered land. Only the absolute overlap AREA separates them, and it wasn't
being computed. classify_metes_and_bounds_tract now flags anything under
_MIN_OVERLAP_AREA_M2 (10 m2) as negligible_overlap_parcels. The
subdivision-cluster check could never have caught these: it groups by recited
legal description, and these parcels' descriptions put them in a subdivision
whose other members legitimately straddle nothing at all here.

WHAT THIS DOES
  1. exclude_non_tract_parcels for the three APNs -- the same reviewed,
     documented path every other correction in this project uses, which also
     recomputes classified_acreage/residual_geom from the remaining parcels.
  2. Marks their 6 transfers superseded_at, never deleting them: per
     _mark_superseded_transfers' own rule (and migration 0031), a transfer row
     is a real recorded document and real fee history can hang off it.
     fee_compute already excludes superseded rows, so this removes the fee
     exposure without destroying the audit trail.
  3. Re-runs reconciliation.

The two genuine matches (apn 451910 at 94.4% and apn 41116 at 99.1%, the real
Alore Center Reserve A/B parcels) and the partial 363641 (2.8%, ~431 m2 -- a
real clip, well above the threshold) are untouched.

Usage: python3 scripts/fix_covid8245_negligible_overlap.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.db.session import get_session
from app.gis.classifier import exclude_non_tract_parcels
from app.gis.reconcile import reconcile_covenant

COVID, TRACT_NO = 8245, 1
ARTIFACT_APNS = ["129590", "129591", "129592"]


def main() -> None:
    with get_session() as session:
        before = session.execute(
            text("""SELECT apn, overlap_fraction FROM parcel_covenant
                    WHERE covid=:c AND tract_no=:t
                      AND run_seq=(SELECT MAX(run_seq) FROM parcel_covenant WHERE covid=:c AND tract_no=:t)
                    ORDER BY overlap_fraction"""),
            {"c": COVID, "t": TRACT_NO},
        ).fetchall()
        print(f"before: {len(before)} matched parcels")

        result = exclude_non_tract_parcels(
            session, covid=COVID, tract_no=TRACT_NO, apns=ARTIFACT_APNS,
            reason=(
                "Three Oak Ridge North 05 residential lots (529, 527, 528) intersect this tract by "
                "0.0104, 0.0517 and 0.0639 square metres respectively -- a digitization artifact where "
                "two independently-surveyed boundaries cross by a hair, not encumbered land. An "
                "absolute overlap area this small cannot represent a real property interest (contrast "
                "apn 363641, kept: 2.8% overlap but ~431 m2 of real area). Auto-flagged by "
                "classify_metes_and_bounds_tract as negligible_overlap_parcels. Their chain-of-title "
                "transfers are superseded in the same correction, since each was a non-exempt "
                "conveyance staged to become a fee liability against a homeowner whose land this "
                "covenant does not encumber."
            ),
        )
        print("exclude_non_tract_parcels:", json.dumps(result, indent=2, default=str))

        superseded = session.execute(
            text("""UPDATE transfer SET superseded_at = now()
                    WHERE covid=:c AND parcel_apn = ANY(:apns) AND superseded_at IS NULL"""),
            {"c": COVID, "apns": ARTIFACT_APNS},
        ).rowcount
        print(f"transfers marked superseded (never deleted): {superseded}")
        session.commit()

    with get_session() as session:
        recon = reconcile_covenant(session, covid=COVID)
        session.commit()
    print("reconcile_covenant:", json.dumps(recon, indent=2, default=str))

    with get_session() as session:
        after = session.execute(
            text("""SELECT pc.apn, pc.overlap_fraction FROM parcel_covenant pc
                    WHERE pc.covid=:c AND pc.tract_no=:t
                      AND pc.run_seq=(SELECT MAX(run_seq) FROM parcel_covenant WHERE covid=:c AND tract_no=:t)
                    ORDER BY pc.overlap_fraction"""),
            {"c": COVID, "t": TRACT_NO},
        ).fetchall()
        print(f"\nafter: {len(after)} matched parcels")
        for r in after:
            print(f"   {r.apn:8s} overlap_fraction={float(r.overlap_fraction):.4f}")
        live = session.execute(
            text("""SELECT COUNT(*) FROM transfer WHERE covid=:c AND parcel_apn = ANY(:apns)
                    AND superseded_at IS NULL"""),
            {"c": COVID, "apns": ARTIFACT_APNS},
        ).scalar()
        print(f"   non-superseded transfers still on the artifact parcels: {live} (expected 0)")


if __name__ == "__main__":
    main()
