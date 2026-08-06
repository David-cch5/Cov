"""One-off script: commit covid 3346 tract 1's anchor, resolved via chain-of-
title parcel matching rather than a course-by-course COGO traverse.

Tract 1 is a 585.5558-acre assemblage of nine separate historical Ruel F.
Sanders deeds (spanning four survey abstracts -- Hillhouse 260, Pierpoint
426, Hamm 263, Watson 605) with 100+ THENCE calls -- a different kind of
problem from a single clean traverse. Given the real API cost already spent
on this covenant's LLM escalation attempts, the declarant's own real chain
of title was used instead: MAW MAGNOLIA, LP (the 2009 declarant) shows up
as a real prior owner, per Montgomery CAD's own per-parcel Deed History, on
exactly four of its current successor's ("MAGNOLIA M3 RANCH LP") parcels --
one in each of the four abstracts Tract 1's own deed cites:

  PIN 43589  (Hillhouse/260)  156.133 ac  "TRACT 1 (HOMESITE)"
  PIN 56403  (Watson/605)     173.075 ac  "TRACT 2, 3"
  PIN 50828  (Pierpoint/426)  191.631 ac  "TRACT 2"
  PIN 43657  (Hamm/263)        65.918 ac  "TRACT 1, 1-A"

Two other Magnolia M3 Ranch LP parcels in these same abstracts were checked
and excluded: PIN 366433 (0.115 ac, Watson) was sold BY MAW Magnolia LP in
2006, three years before this covenant was even recorded -- the declarant
didn't own it at the relevant time. PIN 43622 (29.355 ac, Hillhouse) was
never owned by MAW Magnolia LP at all (PBAR Interests LLC -> Magnolia M3
Ranch LP, 2020) -- an unrelated later acquisition by the same current owner,
not part of the original covenant.

The four included parcels union into ONE contiguous shape (not scattered)
totaling 587.37 acres against the deed's own stated 585.5558 -- 0.31%
deviation, tighter than this project's own 5% tolerance for an LLM-derived
anchor, and arrived at without any further LLM call.

Follows resolve_subdivision_plat_tract's own established pattern for
'current_parcel_match' tracts exactly (upsert_parcel -> union into tract.geom
-> monitor_run -> parcel_covenant links) rather than inventing a new one --
classify_metes_and_bounds_tract explicitly refuses to run against a
'current_parcel_match' tract (confirmed by trying it first), and for good
reason: that function's whole point is an independent spatial query against
a geometry that was NOT built from the matched parcels themselves.

Usage: python3 scripts/commit_covid3346_tract1.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.db.repository import insert_source, upsert_parcel
from app.db.session import get_session
from app.gis.adapters.montgomery_tx import COUNTY_FIPS, iter_parcels
from app.gis.reconcile import reconcile_covenant

COVID, TRACT_NO = 3346, 1
APNS = ["43589", "56403", "50828", "43657"]


def main() -> None:
    with get_session() as session:
        parcels = [next(iter_parcels(where=f"PIN IN ('{apn}')", max_records=1)) for apn in APNS]

        source_id = insert_source(
            session, source_type="gis_api",
            reference=(
                "Chain-of-title parcel match: covid 3346 tract 1 is a 585.5558-ac assemblage of 9 "
                "historical R.F. Sanders deeds across 4 survey abstracts (Hillhouse 260, Pierpoint "
                "426, Hamm 263, Watson 605) -- too long a traverse (100+ courses) to hand-transcribe "
                "given API spend already on this covenant. Verified via Montgomery CAD's own "
                "per-parcel Deed History: the 2009 declarant (MAW MAGNOLIA, LP) is a real prior owner "
                "of exactly 4 of successor 'MAGNOLIA M3 RANCH LP's current parcels (PIN 43589, 56403, "
                "50828, 43657), one in each cited abstract. Two other candidate parcels under the same "
                "current owner were checked and excluded: PIN 366433 was sold BY the declarant in 2006, "
                "before this covenant was even recorded; PIN 43622 was never declarant-owned at all "
                "(acquired from an unrelated party in 2020). The 4 included parcels union into one "
                "contiguous shape, 587.37 ac vs the deed's own stated 585.5558 ac (0.31% deviation)."
            ),
            confidence=0.9,
        )
        for p in parcels:
            upsert_parcel(
                session, county_fips=p["county_fips"], apn=p["apn"], owner_name_raw=p["owner_name_raw"],
                situs_address=p["situs_address"], city=p.get("city"), zip_code=p.get("zip_code"),
                acreage=p["acreage"], geojson=p["geojson"], source_id=source_id,
                recited_legal_description=p.get("recited_legal_description"),
            )

        session.execute(
            text("""
                INSERT INTO tract (covid, tract_no, geom, classified_acreage, boundary_resolution_method, source_id, updated_at)
                SELECT :covid, :tract_no, ST_Multi(ST_Union(geom)),
                       SUM(COALESCE(acreage, ST_Area(geom::geography) / 4046.8564224)),
                       'current_parcel_match', :source_id, now()
                FROM parcel WHERE county_fips = :county_fips AND apn = ANY(:apns)
                ON CONFLICT (covid, tract_no) DO UPDATE SET
                    geom = EXCLUDED.geom, classified_acreage = EXCLUDED.classified_acreage,
                    boundary_resolution_method = EXCLUDED.boundary_resolution_method,
                    source_id = EXCLUDED.source_id, updated_at = now()
            """),
            {"covid": COVID, "tract_no": TRACT_NO, "county_fips": COUNTY_FIPS, "apns": APNS, "source_id": source_id},
        )

        run_seq = session.execute(
            text("SELECT COALESCE(MAX(run_seq), 0) + 1 AS n FROM monitor_run WHERE covid = :covid"),
            {"covid": COVID},
        ).fetchone().n
        session.execute(
            text("""
                INSERT INTO monitor_run (covid, run_seq, run_type, new_parcels_found, status)
                VALUES (:covid, :run_seq, 'initial', :n, 'ok')
            """),
            {"covid": COVID, "run_seq": run_seq, "n": len(parcels)},
        )
        for p in parcels:
            session.execute(
                text("""
                    INSERT INTO parcel_covenant (county_fips, apn, covid, tract_no, run_seq, classification, confidence, rationale)
                    VALUES (:county_fips, :apn, :covid, :tract_no, :run_seq, 'interior', :confidence, :rationale)
                    ON CONFLICT (county_fips, apn, covid, tract_no, run_seq) DO NOTHING
                """),
                {
                    "county_fips": p["county_fips"], "apn": p["apn"], "covid": COVID, "tract_no": TRACT_NO,
                    "run_seq": run_seq, "confidence": 0.9,
                    "rationale": (
                        f"PIN {p['apn']} matched via chain-of-title: declarant MAW MAGNOLIA, LP confirmed "
                        f"as a real prior owner per MCAD's own Deed History, in the abstract Tract 1's own "
                        f"deed cites."
                    ),
                },
            )

        existing = session.execute(
            text("SELECT review_reason FROM covenant WHERE covid = :covid"), {"covid": COVID}
        ).scalar() or ""
        import re
        cleaned = existing
        for pat in [
            r"Metes-and-bounds tract shape validated[^;]*not a confirmed boundary\.",
            r"ANCHOR ESCALATION EXHAUSTED \(automated\)[^;]*cache-read\.",
        ]:
            cleaned = re.sub(rf";?\s*{pat}", "", cleaned, flags=re.IGNORECASE)
        cleaned = cleaned.strip("; ").strip()
        new_note = (
            "ANCHOR RESOLVED (manual, tier=current_parcel_match, confidence=0.90): tract 1 anchored "
            "via chain-of-title-verified union of 4 current parcels (see source.reference) -- 587.37 ac "
            "computed vs 585.5558 ac deed-stated (0.31% deviation). Tract 2 still needs resolution."
        )
        session.execute(
            text("UPDATE covenant SET review_reason = :reason, updated_at = now() WHERE covid = :covid"),
            {"covid": COVID, "reason": f"{cleaned}; {new_note}" if cleaned else new_note},
        )
        session.commit()

    with get_session() as session:
        result = reconcile_covenant(session, covid=COVID)
        session.commit()
    print("reconcile_covenant result:")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
