"""Probe test: pull a small, bounded sample of real Montgomery County parcels via the
live ArcGIS adapter and write them into covenant.parcel. Deliberately capped (max_records)
-- this is a connectivity/mapping proof, not a county-wide pull.

Usage: python3 scripts/test_montgomery_gis.py
"""
import sys

sys.path.insert(0, ".")

from app.db.session import get_session
from app.db.repository import upsert_parcel, insert_source
from app.gis.adapters.montgomery_tx import iter_parcels, BASE_URL, COUNTY_FIPS

SAMPLE_SIZE = 25


def run():
    with get_session() as session:
        source_id = insert_source(
            session, source_type="gis_api", reference=BASE_URL, engine=None, confidence=None,
        )
        n = 0
        for p in iter_parcels(max_records=SAMPLE_SIZE):
            upsert_parcel(
                session, county_fips=p["county_fips"], apn=p["apn"],
                owner_name_raw=p["owner_name_raw"], situs_address=p["situs_address"],
                acreage=p["acreage"], geojson=p["geojson"], source_id=source_id,
            )
            n += 1
            print(f"  {p['apn']:>10}  {p['owner_name_raw']:<30} acreage={p['acreage']}  "
                  f"recited={p['recited_acreage']}  situs={p['situs_address']!r}")
        print(f"\n{n} parcels upserted for county_fips={COUNTY_FIPS}")


if __name__ == "__main__":
    run()
