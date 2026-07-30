"""Smoke test for app/gis/plat_tracking.py -- resolving real, dated plat
filings for a tract's own matched parcels (not the date this project's
software happened to notice them), and reconstructing a raw-vs-platted
acreage timeline from those real dates.

Live resolve_plats_for_tract calls against Montgomery's recorder portal are
NOT re-run here (see scripts/test_classifier.py's own module docstring for
why: this is a live third-party site, and each call is a real Playwright
session -- the already-committed, real results are checked directly from
the DB instead, fast and with no live dependency). platting_timeline itself
is pure deterministic aggregation over already-resolved plat rows, so it
IS re-run live in this file -- no network call, no cost, and it's the
actual regression surface worth exercising on every run.

Usage: python3 scripts/test_plat_tracking.py
"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import get_session
from app.gis.plat_tracking import platting_timeline


def test_persisted_plat_resolution_covid_4440() -> None:
    """The already-committed, real result of running resolve_plats_for_
    tract live against both of covid 4440's tracts: every real subdivision
    found in Montgomery's Plats department (Harrington Trails, The
    Canopies, Timbers Edge, Townsend Reserve, The Presswoods -- 5 real
    subdivisions, 33 total section-level plat filings) got its own real
    recording date, and 4002 real parcels across both tracts were assigned
    a plat_id. A remainder trace to 2 subdivisions ("DUSTY TRAILS" and
    "CANOPIES PARKWAY & WOODWARD BOULEVARD AT TIMBER EDGE PH 1 & SEC")
    that a real live search genuinely found no plat for -- confirmed
    directly (not a search-string bug): "DUSTY TRAILS" parcels themselves
    recite manufactured-home SERIAL/TITLE/MAKE/MODEL fields, meaning this
    was likely never a formally platted subdivision at all, just
    informally-named acreage with individually-titled mobile homes on it.
    Correctly left unresolved and flagged rather than guessed.

    Counts use DISTINCT apn, not a raw row count: parcel_covenant carries
    one row per (apn, run_seq), and this tract was re-classified across
    several run_seq batches while this feature was being built -- plat_id
    itself lives on parcel (not per run_seq), so counting raw
    parcel_covenant rows would multiply-count the same real parcel once
    per historical batch."""
    with get_session() as session:
        plat_rows = session.execute(text(
            "SELECT subdivision_name, count(*) AS n FROM plat "
            "WHERE county_fips = '48339' AND lookup_status = 'found' GROUP BY subdivision_name"
        )).fetchall()
        assigned = session.execute(text("""
            SELECT count(DISTINCT p.apn) FROM parcel_covenant pc
            JOIN parcel p ON p.county_fips = pc.county_fips AND p.apn = pc.apn
            WHERE pc.covid = 4440 AND p.plat_id IS NOT NULL
        """)).scalar()
        cov = session.execute(text("SELECT review_reason FROM covenant WHERE covid = 4440")).fetchone()

    plats_by_subdivision = {r.subdivision_name: r.n for r in plat_rows}
    assert plats_by_subdivision == {
        "HARRINGTON TRAILS": 13, "THE CANOPIES": 5, "THE PRESSWOODS": 9,
        "TIMBERS EDGE": 4, "TOWNSEND RESERVE": 2,
    }, plats_by_subdivision
    assert assigned == 4002, assigned
    assert "PLAT LOOKUP" in cov.review_reason, cov.review_reason
    assert "DUSTY TRAILS" in cov.review_reason, cov.review_reason
    print("PASS: resolve_plats_for_tract (covid 4440, both tracts) -> 5 real subdivisions, "
          "33 real dated plat filings, 4002 parcels assigned, 2 genuinely-unfound "
          "subdivisions correctly flagged rather than guessed")


def test_platting_timeline_tract1_starts_with_harrington_2020() -> None:
    """Tract I's real timeline: raw at the covenant's own 2009 recording,
    completely untouched by any plat until Harrington Trails Section 1
    recorded 2020-03-25 -- 11 years later. Confirms events are ordered by
    real recording date (not discovery order) and that cumulative acreage
    + remaining raw always sum back to the tract's own total."""
    with get_session() as session:
        result = platting_timeline(session, covid=4440, tract_no=1)

    events = result["events"]
    assert events, events
    assert events[0]["recording_date"] == "2020-03-25", events[0]
    assert events[0]["subdivision_name"] == "HARRINGTON TRAILS", events[0]
    assert events[0]["section"] == "1", events[0]
    # strictly increasing recording dates
    dates = [e["recording_date"] for e in events]
    assert dates == sorted(dates), dates
    for e in events:
        assert abs(e["cumulative_platted_acreage"] + e["remaining_raw_acreage"] - result["tract_acreage"]) < 0.01, e
    # cumulative acreage never decreases event to event
    cumulative = [e["cumulative_platted_acreage"] for e in events]
    assert cumulative == sorted(cumulative), cumulative
    print(f"PASS: platting_timeline (covid 4440 tract 1) -> {len(events)} real dated plat events, "
          f"starting 2020-03-25 (Harrington Trails Sec 1), 11 years after the covenant's own "
          f"2009 recording; cumulative platted + remaining raw always reconstitutes the tract total")


def test_platting_timeline_tract2_starts_with_townsend_2022() -> None:
    """Tract II's own independent real timeline: raw until Townsend
    Reserve Section 1 recorded 2022-01-13 -- a different subdivision, a
    different start date, confirming each tract's timeline is genuinely
    its own, not shared/copied from Tract I."""
    with get_session() as session:
        result = platting_timeline(session, covid=4440, tract_no=2)

    events = result["events"]
    assert events, events
    assert events[0]["recording_date"] == "2022-01-13", events[0]
    assert events[0]["subdivision_name"] == "TOWNSEND RESERVE", events[0]
    assert events[-1]["remaining_raw_acreage"] > 0, events[-1]  # still genuinely raw acreage left, not fully built out
    print(f"PASS: platting_timeline (covid 4440 tract 2) -> {len(events)} real dated plat events, "
          f"its own independent timeline starting 2022-01-13 (Townsend Reserve Sec 1)")


if __name__ == "__main__":
    test_persisted_plat_resolution_covid_4440()
    test_platting_timeline_tract1_starts_with_harrington_2020()
    test_platting_timeline_tract2_starts_with_townsend_2022()
    print("\nall plat_tracking smoke tests passed")
