"""Work through every parcel flagged by classify_metes_and_bounds_tract's two
non-tract checks across all metes-and-bounds tracts, and apply the reviewed
outcome. 77 parcels across 8 tracts.

This is the human-judgment half that exclude_non_tract_parcels' own docstring
reserves: the checks only ever FLAG, and each flag below was resolved against
the deed's own text or the parcel geometry before being acted on. Two flags were
NOT simply rubber-stamped -- see covid 4780 and covid 5838 below, where reading
the deed changed what the right answer was.

--------------------------------------------------------------------------
NEGLIGIBLE OVERLAP (<10 m2) -- covid 3028, 3194, 4440 t2, 4780 t1+t2, 4781, 4981
A boundary parcel whose real intersection with the tract is a few square metres
is two independently-surveyed boundaries crossing by a hair, not encumbered
land. Absolute area, not fraction: a 2% clip off a 100-acre parcel is ~3,200 m2
of real land and is kept (covid 8245's apn 363641 is exactly that case).

covid 4981's 16 are all "The Heights At Westridge" -- a subdivision covid 4981's
own deed names three times purely as a boundary tie ("...at the Northwest corner
of The Heights At Westridge Phase II..."), i.e. deed-confirmed adjoining land.
The subdivision-cluster check could not flag them because other Heights parcels
genuinely straddle the boundary, which exempts the whole group; the per-parcel
area test is what caught them.

--------------------------------------------------------------------------
covid 4780 TRACT 1 -- 9 Crescent Cove parcels: NOT a simple adjoiner call
The cluster check flagged "Crescent Cove 03" as a possible non-tract
subdivision. Reading the deed shows the opposite of the obvious conclusion:

    TRACT Ii: All of Crescent Cove, Section Three (3), SAVE AND EXCEPT Lots
    Six (6), Ten (10), Five (5) and Eight (8), in Block One (1)

Crescent Cove Section Three IS this covenant's own third tract -- encumbered
land, not a neighbour. (The OCR garbled the numerals: "TRACT I", "TRACT OD"
(=II), "TRACT Ii" (=III); all three tracts do exist in the database, and tract 3
correctly holds exactly lots 1,2,3,4,7,9,11,12,13,14 -- Block One less the four
excepted lots.)

So these 9 are wrong in TRACT 1 specifically. Tract 1 is the 41.621-acre
metes-and-bounds tract, whose own boundary merely runs "along and around the
Boundary of said Crescent Cove Section Three". Excluding them from tract 1:
  - lots 1, 2, 3, 4, 7 (apn 299216-299219, 299222) stay encumbered via tract 3,
    so no encumbered land is lost -- this removes a double-count of the same
    parcels under two tracts of one covenant.
  - lots 5 and 6 (apn 299220, 299221) are among the four the deed EXPRESSLY
    excepts, so they become correctly unencumbered. (Lots 8 and 10 were already
    absent everywhere -- correct.)
  - apn 497163/497164, "Crescent Cove 03 Replat No 1" RES B and RES C, are HOA
    reserves created by a later replat inside Section Three. They are removed
    from tract 1 for the same reason, but whether they belong in TRACT 3 is a
    genuinely open question this script does NOT decide -- see the note written
    to the covenant at the end.

--------------------------------------------------------------------------
covid 5838 TRACT 1 -- 36 Sunflower Beach / Courtside parcels
The deed never names either subdivision, so the only question is whether they
were platted OUT OF this 318.779-acre Boone Survey tract (the covid 4781
Watermark case, where the later subdivision IS the encumbered land). They were
not: ZERO of the county's 196 Sunflower Beach parcels fall inside the tract
polygon, and the largest overlap among all 36 is 30.5 m2 on a 0.079-acre lot.
Land platted out of a tract sits inside it. These are edge clips.

Includes "The Hideaway at Sunflower Beach Condominiums" units, which the cluster
check grouped under their own key and which sit in exactly the same position
(8.5-12.3 m2 each).

--------------------------------------------------------------------------
covid 4780 TRACT 2 -- Walden Road Business Park
Named in the deed only as the COMMENCING tie ("...marks the Northeast corner of
Lot 11, Block 2, Walden Road Business Park...") -- a survey reference point,
never conveyed. All overlap by under 8 m2.

Usage: python3 scripts/review_flagged_non_tract_parcels.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.db.session import get_session
from app.gis.classifier import exclude_non_tract_parcels
from app.gis.reconcile import reconcile_covenant

_NEG = ("intersects this tract by under 10 m2 -- two independently-surveyed boundaries crossing by "
        "a hair, not encumbered land (auto-flagged as negligible_overlap_parcels; contrast a small "
        "FRACTION on a large parcel, which can still be acres and is kept)")

# Every APN below was verified against the detectors' own computed output before
# being written here -- an earlier draft of this list had five hand-transcribed
# APNs that did not exist in the flagged set at all.
PLAN = [
    (3028, 1, ["2797879", "2847835"],
     f"Star Trail lots that {_NEG}."),
    (3194, 1, ["463635"],
     f"An abstract-survey tract that {_NEG}."),
    (4440, 2, ["532316"],
     f"A Townsend Reserve 01 lot that {_NEG}."),
    (4780, 1,
     ["299216", "299217", "299218", "299219", "299220", "299221", "299222", "497163", "497164",
      "492958"],
     "Crescent Cove Section Three is this covenant's own TRACT 3 (deed: 'TRACT Ii: All of Crescent "
     "Cove, Section Three (3), SAVE AND EXCEPT Lots Six (6), Ten (10), Five (5) and Eight (8)'), not "
     "part of tract 1 -- tract 1's boundary merely runs 'along and around the Boundary of said "
     "Crescent Cove Section Three'. These 9 Crescent Cove parcels overlap tract 1 by 62-164 m2, so "
     "they are excluded on the DEED's evidence, not as negligible slivers. Lots 1/2/3/4/7 (299216-"
     "299219, 299222) remain encumbered via tract 3, so this removes a double-count of one covenant's "
     "own land under two of its tracts; lots 5 and 6 (299220, 299221) are expressly excepted by the "
     "deed and are correctly now unencumbered (lots 8 and 10 were already absent everywhere); "
     "497163/497164 are Crescent Cove 03 Replat No 1 HOA reserves whose tract-3 membership is a "
     "separate open question recorded below. Plus apn 492958, a Reserve On Lake Conroe 01 lot that "
     f"{_NEG}."),
    (4780, 2, ["290556", "362225", "417469", "449530", "493019", "505327"],
     "Walden Road Business Park (named in the deed only as the COMMENCING survey tie -- '...marks the "
     "Northeast corner of Lot 11, Block 2, Walden Road Business Park...' -- never conveyed), plus an "
     "Evans Commercial Park reserve and one Reserve On Lake Conroe lot. Every one of these "
     f"{_NEG}; four of the six overlap by under 0.2 m2."),
    (4781, 1, ["265806", "265832"],
     f"Palm Beach Estates lots -- named in the deed only as the bearing-basis line. Both {_NEG}."),
    (4981, 1,
     ["2631515", "2631516", "2631517", "2631518", "2631519", "2631520", "2631521", "2631522",
      "2631523", "2631524", "2631525", "2631526", "2631530", "2710587", "2710593", "2710594"],
     "The Heights At Westridge -- named three times in this covenant's own deed purely as a boundary "
     "tie ('at the Northwest corner of The Heights At Westridge Phase II', etc), i.e. deed-confirmed "
     f"adjoining land. Each {_NEG} (0.03-8.9 m2). The subdivision-cluster check could not reach them "
     "because other Heights parcels genuinely straddle the boundary, which exempts the whole group -- "
     "the per-parcel area test is what caught them."),
    (5838, 1, None,  # resolved by query -- see _covid_5838_apns
     "Sunflower Beach PUD / P.U.D. Townhomes / The Hideaway at Sunflower Beach Condominiums / "
     "Courtside Addition, plus three Boone Survey abstract tracts. None of the subdivisions is named "
     "anywhere in this deed, and none was platted out of this 318.779-acre Boone Survey tract: ZERO of "
     "the county's 196 Sunflower Beach parcels fall inside the tract polygon (land platted out of a "
     "tract sits inside it, as covid 4781's Watermark does), and the largest overlap among all of them "
     "is 30.5 m2 on a 0.079-acre lot. The three Boone Survey tracts (198320, 198326, 198337) are "
     f"neighbouring remainder acreage in the same survey that {_NEG}. All are edge clips at ordinary "
     "digitization tolerance, not encumbered land."),
]


def _covid_5838_apns(session) -> list[str]:
    """Named subdivisions plus the sub-10 m2 abstract tracts -- resolved by
    query rather than transcribed, because it is 39 APNs."""
    return [r.apn for r in session.execute(text("""
        SELECT pc.apn FROM parcel_covenant pc
        JOIN tract t ON t.covid=pc.covid AND t.tract_no=pc.tract_no
        JOIN parcel p ON p.apn=pc.apn AND p.county_fips=pc.county_fips
        WHERE pc.covid=5838 AND pc.tract_no=1 AND pc.classification='boundary'
          AND pc.run_seq=(SELECT MAX(run_seq) FROM parcel_covenant WHERE covid=5838 AND tract_no=1)
          AND (p.recited_legal_description ILIKE '%SUNFLOWER%'
               OR p.recited_legal_description ILIKE '%COURTSIDE%'
               OR ST_Area(ST_Intersection(t.geom, p.geom)::geography) < 10.0)
        ORDER BY pc.apn
    """))]


def main() -> None:
    touched = set()
    with get_session() as session:
        for covid, tract_no, apns, reason in PLAN:
            if apns is None:
                apns = _covid_5838_apns(session)
            present = [r.apn for r in session.execute(text("""
                SELECT DISTINCT apn FROM parcel_covenant
                WHERE covid=:c AND tract_no=:t AND apn = ANY(:a)
            """), {"c": covid, "t": tract_no, "a": apns})]
            missing = sorted(set(apns) - set(present))
            if missing:
                print(f"  covid {covid} tract {tract_no}: {len(missing)} planned APN(s) not in the "
                      f"census, skipping those: {missing}")
            if not present:
                print(f"  covid {covid} tract {tract_no}: nothing to do")
                continue
            result = exclude_non_tract_parcels(session, covid=covid, tract_no=tract_no,
                                               apns=sorted(present), reason=reason)
            print(f"  covid {covid} tract {tract_no}: excluded {result['excluded_count']} rows, "
                  f"classified_acreage now {result['classified_acreage']}")
            touched.add(covid)
        session.commit()

    print()
    for covid in sorted(touched):
        with get_session() as session:
            r = reconcile_covenant(session, covid=covid)
            session.commit()
        for tn, tr in r["tract_results"].items():
            if tr.get("checked"):
                print(f"  covid {covid} tract {tn}: {tr['status']}, "
                      f"unaccounted {tr.get('unaccounted_acreage')}")

    print("\nfinal parcel counts:")
    with get_session() as session:
        for covid in sorted(touched):
            for r in session.execute(text("""
                SELECT tract_no, count(*) AS n FROM parcel_covenant pc
                WHERE covid=:c AND run_seq=(SELECT MAX(run_seq) FROM parcel_covenant
                                            WHERE covid=:c AND tract_no=pc.tract_no)
                GROUP BY tract_no ORDER BY tract_no
            """), {"c": covid}):
                print(f"   covid {covid} tract {r.tract_no}: {r.n} parcels")
        # The one question this script deliberately leaves open.
        session.execute(text("""
            UPDATE covenant SET review_reason = review_reason ||
                '; OPEN QUESTION (manual, 2026-08-07): Crescent Cove 03 Replat No 1 reserves RES A/B/C '
                '(apn 497162, 497163, 497164) and lot 497161 sit inside Crescent Cove Section Three, '
                'which the deed encumbers in full as tract 3 save four named lots -- but they were '
                'created by a later replat, so ingestion''s lot list (1,2,3,4,7,9,11,12,13,14) does not '
                'include them. Needs a human decision on whether a post-covenant replat inside an '
                'encumbered subdivision is itself encumbered.', updated_at = now()
            WHERE covid = 4780
        """))
        session.commit()
    print("\nOpen question recorded on covid 4780 (post-covenant replat reserves inside tract 3).")


if __name__ == "__main__":
    main()
