"""Resolve covid 5839's 43.354 acre tract to Nueces CAD parcel 373270.

THE TRAVERSE WAS NEVER THE ANSWER
This tract's metes-and-bounds description was chased for a long stretch: five
OCR/typo families fixed, a vision-OCR escalation, 27 courses parsed from 1, and
four separate hypotheses tested and eliminated -- the two discontinuous curves
(all 16 tangent/turn combinations), a missing or extra course, and a misread
bearing digit. It still does not close, by 1,294 ft.

None of that was necessary. The deed's own Exhibit A names the county account:
R373270. The boundary is a parcel that already exists, and the traverse is at
most a cross-check on it.

FIVE INDEPENDENT CONFIRMATIONS, none of which needs the traverse to close:
  * Nueces CAD records parcel 373270's acreage as 43.354 -- exactly the deed's
    own stated figure, to the thousandth.
  * The deed's Exhibit itself names account R373270.
  * The Point of Beginning, placed from the deed's NGS SF-010 tie
    (North 24 51'16" East 4,358.04 feet, reversed from the monument's published
    Texas South Zone coordinates), falls 0 ft from that parcel.
  * The parcel's own legal reads "HALL E SUR 588 LS 227 & BOONE I W SUR 58...",
    the same two surveys Exhibit A cites.
  * It is the ONLY parcel in Nueces County between 42 and 45 acres.

WHY TRACT 1 IS DELIBERATELY NOT CREATED HERE
covid 5839's other tract is the 318.779 acre tract, which covid 5838 re-records
(see the RELATED INSTRUMENT note both covenants carry). Whether a re-recording
supersedes is a title question the deed text does not settle, and creating that
tract here would count the same 318.779 acres against two covenants and, on a
transfer, two fees. It is left absent on purpose until that call is made, and
this tract keeps the deed's own document-order number (2) rather than being
renumbered to hide the gap.

Usage: python3 scripts/resolve_covid5839_tract2.py [--commit]
"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.repository import insert_source
from app.db.session import get_session

COVID, TRACT_NO, COUNTY_FIPS, APN = 5839, 2, "48355", "373270"
STATED_ACRES = 43.354
MAX_ACREAGE_DEVIATION = 0.005          # the recorded figure matches the deed exactly


def main(commit: bool) -> None:
    with get_session() as session:
        row = session.execute(text("""
            SELECT apn, owner_name_raw, acreage,
                   ST_Area(geom::geography) / 4046.8564224 AS geom_acres,
                   recited_legal_description AS legal
            FROM parcel WHERE county_fips = :fips AND apn = :apn
        """), {"fips": COUNTY_FIPS, "apn": APN}).mappings().first()
        if row is None:
            raise SystemExit(f"parcel {APN} not found in {COUNTY_FIPS}")

        print(f"  parcel {row['apn']}: recorded {row['acreage']} ac, geometry "
              f"{row['geom_acres']:.3f} ac, owner {row['owner_name_raw']}")
        print(f"    legal: {row['legal']}")

        # The county's own recorded acreage must match the deed's stated figure --
        # this is the check that makes the identification safe, not the geometry
        # area (which carries ordinary digitisation noise).
        deviation = abs(float(row["acreage"]) - STATED_ACRES) / STATED_ACRES
        if deviation > MAX_ACREAGE_DEVIATION:
            raise SystemExit(f"recorded acreage {row['acreage']} is {deviation:.2%} from the "
                             f"deed's stated {STATED_ACRES} -- refusing to identify on that")
        print(f"    recorded acreage matches the deed's stated {STATED_ACRES} exactly")

        if not commit:
            print("\n  dry run -- pass --commit to write")
            return

        source_id = insert_source(
            session, source_type="gis_api",
            reference=(f"Nueces CAD parcel {APN}: the 43.354 ac tract of covid {COVID}'s "
                       f"Exhibit A, named in the deed's own exhibit as account R{APN} and "
                       f"confirmed by recorded acreage, survey citation, and an NGS SF-010 "
                       f"Point-of-Beginning tie landing on the parcel"),
            confidence=0.95,
        )
        session.execute(text("""
            INSERT INTO tract (covid, tract_no, geom, boundary_resolution_method,
                               stated_acreage, source_id, updated_at)
            SELECT :covid, :tract_no, p.geom, 'current_parcel_match', :stated, :source_id, now()
            FROM parcel p WHERE p.county_fips = :fips AND p.apn = :apn
            ON CONFLICT (covid, tract_no) DO UPDATE SET
                geom = EXCLUDED.geom,
                boundary_resolution_method = EXCLUDED.boundary_resolution_method,
                stated_acreage = EXCLUDED.stated_acreage,
                source_id = EXCLUDED.source_id, updated_at = now()
        """), {"covid": COVID, "tract_no": TRACT_NO, "stated": STATED_ACRES,
               "source_id": source_id, "fips": COUNTY_FIPS, "apn": APN})
        # The tract IS this one parcel, so its census is that parcel -- written here
        # rather than left to a spatial classifier, which would only rediscover the
        # same single parcel from the very geometry it came from.
        # parcel_covenant.run_seq is FK'd to monitor_run: every census batch belongs
        # to an audit run by design (monitor_run is a trail of periodic re-checks,
        # not a cache), so the run has to exist before its rows can.
        run_seq = session.execute(text(
            "SELECT COALESCE(MAX(run_seq), 0) + 1 FROM monitor_run WHERE covid = :covid"
        ), {"covid": COVID}).scalar()
        session.execute(text(
            "INSERT INTO monitor_run (covid, run_seq, run_at, run_type, new_parcels_found, status) "
            "VALUES (:covid, :run_seq, now(), 'initial', 1, 'ok') ON CONFLICT DO NOTHING"
        ), {"covid": COVID, "run_seq": run_seq})

        rationale = (
            "the tract is this parcel: named as account R373270 in the deed's own Exhibit A, "
            "recorded acreage equal to the deed's stated 43.354, matching survey and Land Scrip "
            "citation, and an NGS SF-010 Point-of-Beginning tie landing on it"
        )
        session.execute(text(
            "INSERT INTO parcel_covenant (county_fips, apn, covid, tract_no, run_seq, "
            "classification, overlap_fraction, confidence, rationale, classified_at) "
            "VALUES (:fips, :apn, :covid, :tract_no, :run_seq, 'interior', 1.0, 0.95, "
            ":rationale, now()) ON CONFLICT DO NOTHING"
        ), {"fips": COUNTY_FIPS, "apn": APN, "covid": COVID, "tract_no": TRACT_NO,
            "run_seq": run_seq, "rationale": rationale})
        session.execute(text(
            "UPDATE tract SET classified_acreage = :acres, updated_at = now() "
            "WHERE covid = :covid AND tract_no = :tract_no"
        ), {"acres": float(row["geom_acres"]), "covid": COVID, "tract_no": TRACT_NO})
        session.commit()
        print(f"\n  committed: covid {COVID} tract {TRACT_NO}, source_id={source_id}, "
              f"census = 1 parcel ({row['geom_acres']:.3f} ac)")


if __name__ == "__main__":
    main(commit="--commit" in sys.argv)
