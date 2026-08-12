"""Tests for app/gis/formation.py's formation-date plausibility check.

Every case is a wrong date that actually reached this database, and each was caught
by a human noticing the YEAR rather than by anything in the code:

  1800-01-01   Nueces' index sentinel, taken as a real plat date
  1900-01-01   Collin's sentinel on HEIGHTS WESTRIDGE #1, platted 2004
  2008-10-16   doc 46201 (the unit 4 plat) sitting on 11 unit-4A lots, in a
               subdivision whose earliest real filing is 2013 -- and it survived a
               correction pass because one row was fixed and no duplicate was sought

Synthetic rows are written inside a transaction that is rolled back, so the checks
run against the real schema without leaving anything behind.

Usage: python3 scripts/test_formation_dates.py
"""
import sys
from datetime import date, timedelta

from sqlalchemy import text

sys.path.insert(0, ".")

from app.db.session import get_session
from app.gis.formation import check_formation_date_plausibility


def _a_parcel_with_a_plat(session):
    row = session.execute(text("""
        SELECT p.county_fips, p.apn, p.plat_id, p.formed_date, p.formed_by_instrument,
               pl.subdivision_name, pl.recording_date
          FROM parcel p JOIN plat pl ON pl.plat_id = p.plat_id
         WHERE p.formed_date IS NOT NULL AND pl.recording_date IS NOT NULL
         LIMIT 1
    """)).fetchone()
    assert row is not None, "no dated, platted parcel to test against"
    return row


def _set_date(session, county_fips, apn, when):
    session.execute(
        text("UPDATE parcel SET formed_date = :d WHERE county_fips = :cf AND apn = :apn"),
        {"d": when, "cf": county_fips, "apn": apn})


def test_a_sentinel_date_is_caught() -> None:
    """1/1/1800 and 1/1/1900 are a county index saying "unknown". plat_tracking guards
    them at parse time, but that cannot see a row written by another path -- and two of
    the three real errors came from another path."""
    with get_session() as session:
        p = _a_parcel_with_a_plat(session)
        for sentinel in (date(1800, 1, 1), date(1900, 1, 1), date(1799, 6, 1)):
            _set_date(session, p.county_fips, p.apn, sentinel)
            got = check_formation_date_plausibility(session)
            hits = [f for f in got["sentinel_date"] if f["apn"] == p.apn]
            assert hits, f"{sentinel} must be caught, got {got['implausible']} findings"
            assert not got["plausible"]
        session.rollback()
    print("PASS: 1800-01-01, 1900-01-01 and a pre-1850 date are all caught")


def test_a_lot_dated_before_its_own_subdivision_is_caught() -> None:
    """The unit-4A error: a lot cannot be platted into a subdivision that did not yet
    exist. This is the check that would have caught it without a human reading years."""
    with get_session() as session:
        p = _a_parcel_with_a_plat(session)
        _set_date(session, p.county_fips, p.apn, p.recording_date - timedelta(days=3000))
        got = check_formation_date_plausibility(session)
        hits = [f for f in got["before_subdivision"] if f["apn"] == p.apn]
        assert hits, f"a date 3000 days before the subdivision's first filing must be caught"
        # Measured against the SUBDIVISION's earliest filing, which can precede this
        # parcel's own plat -- so the gap is smaller than the 3000 days shifted, and
        # that is the right reference: the question is whether the subdivision existed.
        assert hits[0]["days_early"] > 0, hits[0]
        assert hits[0]["first_filing"] <= p.recording_date, hits[0]

        # And the tolerance holds: a same-day amending plat is not a finding.
        _set_date(session, p.county_fips, p.apn, p.recording_date - timedelta(days=1))
        got = check_formation_date_plausibility(session)
        assert not [f for f in got["before_subdivision"] if f["apn"] == p.apn], \
            "one day early is indexing noise, not a finding"
        session.rollback()
    print(f"PASS: a lot dated 3000 days before its subdivision's first filing is caught; "
          f"one day early is not")


def test_a_future_formation_date_is_caught() -> None:
    with get_session() as session:
        p = _a_parcel_with_a_plat(session)
        _set_date(session, p.county_fips, p.apn, date.today() + timedelta(days=30))
        got = check_formation_date_plausibility(session)
        assert [f for f in got["future_date"] if f["apn"] == p.apn], got["future_date"]
        session.rollback()
    print("PASS: a formation date in the future is caught")


def test_the_spine_explains_the_pre_plat_conveyances() -> None:
    """The resolution, not a silencing. Collin 2766013/2766016 were platted 2017-10-03
    and carry 2011 transfers -- pre-plat conveyances of the ANCESTOR tract, which the
    lot could not have been party to because it did not exist. record_split recorded
    that ancestry (root -> 2011-01-04 -> 2011-12-30 -> the 2017 plat), and a transfer
    whose instrument sits on an ancestor is accounted for."""
    from app.title.tract_spine import walk_up

    with get_session() as session:
        for apn, expected_deeds in (("2766013", ["20171003010004680", "20111230001411880",
                                                  "20110104000015390"]),
                                     ("2766016", ["20171003010004680", "20110104000015390"])):
            node = session.execute(text("""
                SELECT node_id FROM tract_node WHERE covid = 3028 AND apn = :apn
            """), {"apn": apn}).scalar()
            assert node is not None, f"{apn} has no spine node"
            chain = walk_up(session, node)
            deeds = [c["split_instrument_number"] for c in chain if c["split_instrument_number"]]
            assert deeds == expected_deeds, (apn, deeds)
            assert chain[-1]["disposition"] == "root", chain[-1]

        got = check_formation_date_plausibility(session)
        assert not got["conveyed_before_formed"], got["conveyed_before_formed"]
    print("PASS: both lots walk back through their 2011 conveyances to the tract root, "
          "and neither is reported any more")


def test_an_unexplained_conveyance_before_formation_is_still_caught() -> None:
    """The check must not have been weakened into uselessness: a transfer with no
    matching ancestor is still a finding."""
    with get_session() as session:
        # 2766013's 2021-12-28 sale is a conveyance OF the lot, so it is on no ancestor.
        # Move formation after it and the parcel becomes unexplained again.
        _set_date(session, "48085", "2766013", date(2022, 6, 1))
        got = check_formation_date_plausibility(session)
        hits = [f for f in got["conveyed_before_formed"] if f["apn"] == "2766013"]
        assert hits, "a transfer on no ancestor must still be reported"
        assert hits[0]["earliest_transfer"] < date(2022, 6, 1), hits[0]
        assert "ancestor tract" in hits[0]["check_first"], hits[0]
        session.rollback()
    print("PASS: a conveyance explained by no ancestor is still caught")


def test_the_live_database_has_no_impossible_dates() -> None:
    """The standing assertion, now on all four classes. conveyed_before_formed was
    allowed to be non-empty while the two Collin lots' ancestry was unrecorded; the
    spine records it, so nothing is outstanding and this holds every class at zero."""
    with get_session() as session:
        got = check_formation_date_plausibility(session)
        for kind in ("sentinel_date", "before_subdivision",
                     "conveyed_before_formed", "future_date"):
            assert not got[kind], f"{kind}: {got[kind][:3]}"
        assert got["plausible"], got["implausible"]
    print(f"PASS: all {got['checked']:,} formation dates on record are plausible -- no "
          f"sentinel, none before its own subdivision, none conveyed before it existed, "
          f"none in the future")


if __name__ == "__main__":
    test_a_sentinel_date_is_caught()
    test_a_lot_dated_before_its_own_subdivision_is_caught()
    test_a_future_formation_date_is_caught()
    test_the_spine_explains_the_pre_plat_conveyances()
    test_an_unexplained_conveyance_before_formation_is_still_caught()
    test_the_live_database_has_no_impossible_dates()
    print("\nall formation-date plausibility tests passed")
