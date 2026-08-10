"""Tests for app/title/release.py -- covenant terminations and buyouts.

The property under test is not "a released parcel is exempt". It is that a
release ENDS the obligation without erasing the period before it: a fee already
collected stays collected and correctly so, because it was owed when it was
taken. Every case below is built around that boundary.

Synthetic fixtures throughout -- no live covenant is mutated.

Usage: python3 scripts/test_release.py
"""
import sys
from datetime import date

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import get_session
from app.title.fee_compute import compute_fee_for_transfer
from app.title.release import (
    apply_releases_to_transfers,
    record_release,
    release_for_transfer,
    released_parcels,
)

COVID = 999991
COUNTY = "48339"
APN_A, APN_B = "TEST-REL-A", "TEST-REL-B"
EFFECTIVE = date(2020, 6, 1)


def _setup(session) -> None:
    session.execute(text("""
        INSERT INTO covenant (covid, county_fips, status, legal_description_raw, fee_percent)
        VALUES (:covid, :cf, 'needs_review', 'release fixture', 1.0)
        ON CONFLICT (covid) DO UPDATE SET status = EXCLUDED.status
    """), {"covid": COVID, "cf": COUNTY})
    for apn in (APN_A, APN_B):
        session.execute(text("""
            INSERT INTO parcel (county_fips, apn, geom, acreage)
            VALUES (:cf, :apn, ST_SetSRID(ST_GeomFromText(
                'POLYGON((-95.5 30.3, -95.4999 30.3, -95.4999 30.3001, -95.5 30.3001, -95.5 30.3))'
            ), 4326), 1.0)
            ON CONFLICT (county_fips, apn) DO NOTHING
        """), {"cf": COUNTY, "apn": apn})
    # transfer.tract_no is NOT NULL and FK'd to tract, so the tract must exist
    session.execute(text("""
        INSERT INTO tract (covid, tract_no, geom, boundary_resolution_method, classified_acreage)
        VALUES (:covid, 1, ST_Multi(ST_SetSRID(ST_GeomFromText(
            'POLYGON((-95.5 30.3, -95.4999 30.3, -95.4999 30.3001, -95.5 30.3001, -95.5 30.3))'
        ), 4326)), 'current_parcel_match', 2.0)
        ON CONFLICT (covid, tract_no) DO NOTHING
    """), {"covid": COVID})
    for apn in (APN_A, APN_B):
        session.execute(text("""
            INSERT INTO monitor_run (covid, run_seq, run_at, run_type, new_parcels_found, status)
            VALUES (:covid, 1, now(), 'initial', 2, 'ok') ON CONFLICT DO NOTHING
        """), {"covid": COVID})
        session.execute(text("""
            INSERT INTO parcel_covenant (county_fips, apn, covid, tract_no, run_seq,
                                         classification, classified_at)
            VALUES (:cf, :apn, :covid, 1, 1, 'interior', now()) ON CONFLICT DO NOTHING
        """), {"cf": COUNTY, "apn": apn, "covid": COVID})

    # one transfer before the release, one after, on the parcel being released
    for inst, when in (("BEFORE-REL", date(2019, 3, 1)), ("AFTER-REL", date(2021, 3, 1))):
        session.execute(text("""
            INSERT INTO transfer (county_fips, instrument_number, instrument_number_type, covid,
                                  tract_no, parcel_county_fips, parcel_apn, recording_date,
                                  instrument_type, consideration_amount)
            VALUES (:cf, :inst, 'modern_instrument', :covid, 1, :cf, :apn, :rd, 'warranty_deed', 100000)
            ON CONFLICT DO NOTHING
        """), {"cf": COUNTY, "inst": inst, "covid": COVID, "apn": APN_A, "rd": when})


def _teardown(session) -> None:
    session.execute(text("DELETE FROM covenant_release_parcel WHERE release_id IN "
                         "(SELECT release_id FROM covenant_release WHERE covid = :c)"), {"c": COVID})
    session.execute(text("DELETE FROM covenant_release WHERE covid = :c"), {"c": COVID})
    session.execute(text("DELETE FROM transfer WHERE covid = :c"), {"c": COVID})
    session.execute(text("DELETE FROM parcel_covenant WHERE covid = :c"), {"c": COVID})
    session.execute(text("DELETE FROM monitor_run WHERE covid = :c"), {"c": COVID})
    session.execute(text("DELETE FROM tract WHERE covid = :c"), {"c": COVID})
    session.execute(text("DELETE FROM covenant WHERE covid = :c"), {"c": COVID})
    session.execute(text("DELETE FROM parcel WHERE county_fips = :cf AND apn = ANY(:apns)"),
                    {"cf": COUNTY, "apns": [APN_A, APN_B]})


def test_release_exempts_only_transfers_on_or_after_its_effective_date() -> None:
    """The whole point. A release ends the obligation going FORWARD -- per these
    covenants a termination takes effect after it is recorded -- and leaves
    everything before it alone: a fee taken on the earlier transfer was owed when
    it was taken, and a later termination does not claw it back."""
    try:
        with get_session() as session:
            _setup(session)
            record_release(session, covid=COVID, release_type="termination",
                           effective_date=EFFECTIVE, scope="partial",
                           parcels=[(COUNTY, APN_A)],
                           recording_instrument="TERM-2020-001")
            session.commit()

            before = release_for_transfer(session, COVID, COUNTY, APN_A, date(2019, 3, 1))
            after = release_for_transfer(session, COVID, COUNTY, APN_A, date(2021, 3, 1))
            on_the_day = release_for_transfer(session, COVID, COUNTY, APN_A, EFFECTIVE)
        assert before is None, before
        assert after is not None and after["exemption_category"] == "post_termination", after
        assert after["releases_transfer"] is True, after
        # Same day: recording sequence decides, which this system does not model,
        # so the fee stays owed and a human reads the instrument numbers.
        assert on_the_day is not None and on_the_day["same_day"] is True, on_the_day
        assert on_the_day["releases_transfer"] is False, on_the_day
        assert on_the_day["needs_review"] is True, on_the_day
        print("PASS: a release exempts transfers recorded AFTER it; same-day is flagged, "
              "not released; earlier transfers untouched")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_partial_release_leaves_other_parcels_encumbered() -> None:
    """A partial release names its parcels. Anything it does not name stays
    encumbered -- releasing a subset must never be read as releasing the
    covenant."""
    try:
        with get_session() as session:
            _setup(session)
            record_release(session, covid=COVID, release_type="buyout",
                           effective_date=EFFECTIVE, scope="partial",
                           parcels=[(COUNTY, APN_A)], consideration_amount=25000)
            session.commit()
            released = release_for_transfer(session, COVID, COUNTY, APN_A, date(2021, 3, 1))
            untouched = release_for_transfer(session, COVID, COUNTY, APN_B, date(2021, 3, 1))
        assert released is not None and released["exemption_category"] == "post_buyout", released
        assert float(released["consideration_amount"]) == 25000.0, released
        assert untouched is None, untouched
        print("PASS: a partial buyout releases only its named parcels; the rest stay encumbered")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_applying_releases_never_overwrites_an_existing_exemption() -> None:
    """A transfer already exempt for its own reason -- a foreclosure, a spousal
    transfer -- keeps that reason. It was exempt at the time for something that
    actually happened, and a later release does not rewrite that history."""
    try:
        with get_session() as session:
            _setup(session)
            session.execute(text("""
                UPDATE transfer SET exemption_category = 'foreclosure',
                                    exemption_basis = 'pre-existing finding'
                WHERE covid = :c AND instrument_number = 'AFTER-REL'
            """), {"c": COVID})
            record_release(session, covid=COVID, release_type="termination",
                           effective_date=EFFECTIVE, scope="covenant")
            result = apply_releases_to_transfers(session, COVID)
            session.commit()
            rows = dict(session.execute(text(
                "SELECT instrument_number, exemption_category FROM transfer WHERE covid = :c"
            ), {"c": COVID}).all())
        assert rows["AFTER-REL"] == "foreclosure", rows
        assert rows["BEFORE-REL"] is None, rows      # predates the release, still fee-bearing
        assert result["transfers_exempted"] == 0, result
        print("PASS: an existing exemption survives a release; a pre-release transfer stays owed")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_covenant_wide_release_covers_every_parcel() -> None:
    """scope='covenant' needs no parcel list and reaches parcels the release
    instrument never enumerated."""
    try:
        with get_session() as session:
            _setup(session)
            record_release(session, covid=COVID, release_type="termination",
                           effective_date=EFFECTIVE, scope="covenant")
            session.commit()
            for apn in (APN_A, APN_B):
                got = release_for_transfer(session, COVID, COUNTY, apn, date(2021, 3, 1))
                assert got is not None and got["scope"] == "covenant", (apn, got)
        print("PASS: a covenant-wide release covers parcels it never had to name")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_malformed_releases_are_refused() -> None:
    """A partial release naming nothing would read as releasing everything or
    nothing depending on which query looked at it. Neither is what anyone meant,
    so it is refused rather than written."""
    with get_session() as session:
        for kwargs, expect in (
            ({"release_type": "expiry", "scope": "covenant"}, "release_type"),
            ({"release_type": "termination", "scope": "someday"}, "scope"),
            ({"release_type": "termination", "scope": "partial"}, "must name the parcels"),
            ({"release_type": "termination", "scope": "covenant",
              "parcels": [(COUNTY, APN_A)]}, "cannot also name"),
        ):
            try:
                record_release(session, covid=COVID, effective_date=EFFECTIVE, **kwargs)
                raise AssertionError(f"expected a ValueError for {kwargs}")
            except ValueError as exc:
                assert expect in str(exc), (kwargs, str(exc))
        session.rollback()
    print("PASS: malformed releases are refused, not written in an ambiguous state")


def test_earliest_effective_release_wins() -> None:
    """The obligation ended the first time it ended. A later instrument covering
    the same land cannot move that date forward."""
    try:
        with get_session() as session:
            _setup(session)
            record_release(session, covid=COVID, release_type="buyout",
                           effective_date=date(2022, 1, 1), scope="partial",
                           parcels=[(COUNTY, APN_A)])
            record_release(session, covid=COVID, release_type="termination",
                           effective_date=EFFECTIVE, scope="partial",
                           parcels=[(COUNTY, APN_A)])
            session.commit()
            got = release_for_transfer(session, COVID, COUNTY, APN_A, date(2023, 1, 1))
        assert got["effective_date"] == EFFECTIVE, got
        assert got["release_type"] == "termination", got
        print("PASS: where several releases apply, the earliest effective one governs")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_fee_compute_honours_a_release_recorded_after_the_fact() -> None:
    """The gate has to sit in fee_compute itself, not only in
    transfer.exemption_category. A release is often recorded long after the
    transfers it affects were already walked and classified, and re-walking every
    chain first should not be a precondition for billing correctly.

    The pre-release transfer must still compute a fee -- that is the history the
    release does not touch."""
    try:
        with get_session() as session:
            _setup(session)
            record_release(session, covid=COVID, release_type="buyout",
                           effective_date=EFFECTIVE, scope="partial",
                           parcels=[(COUNTY, APN_A)], consideration_amount=25000,
                           recording_instrument="BUYOUT-2020-7")
            session.commit()

            after = compute_fee_for_transfer(session, COUNTY, "AFTER-REL",
                                             date(2021, 3, 1), APN_A)
            before = compute_fee_for_transfer(session, COUNTY, "BEFORE-REL",
                                              date(2019, 3, 1), APN_A)
            session.rollback()
        assert after["fee_owed"] is False, after
        assert "buyout" in after["reason"] and "BUYOUT-2020-7" in after["reason"], after
        assert before["fee_owed"] is not False, before   # still owed, or unknown -- never released
        print(f"PASS: fee_compute honours a later-recorded release ({after['reason']}); "
              f"the pre-release transfer is untouched")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_effective_date_defaults_to_recording_and_refuses_to_reach_back() -> None:
    """Per these covenants a termination is effective after the date it is
    recorded, so effective_date is derived from recording_date rather than
    supplied independently. An earlier effective date would void fees already
    owed, so it is refused unless the caller quotes the instrument language that
    permits it -- the answer does depend on the wording, but a retroactive
    release has to be a deliberate, evidenced act rather than a typo."""
    try:
        with get_session() as session:
            _setup(session)
            got = record_release(session, covid=COVID, release_type="termination",
                                 scope="covenant", recording_date=date(2020, 6, 1))
            assert got["effective_date"] == date(2020, 6, 1), got

            try:
                record_release(session, covid=COVID, release_type="termination",
                               scope="covenant", recording_date=date(2020, 6, 1),
                               effective_date=date(2019, 1, 1))
                raise AssertionError("expected a ValueError for a retroactive effective date")
            except ValueError as exc:
                assert "retroactive_basis" in str(exc), exc

            back = record_release(session, covid=COVID, release_type="termination",
                                  scope="covenant", recording_date=date(2020, 6, 1),
                                  effective_date=date(2019, 1, 1),
                                  retroactive_basis="instrument recites it is effective nunc pro tunc")
            assert back["effective_date"] == date(2019, 1, 1), back
            session.rollback()
        print("PASS: effective_date defaults to the recording date; reaching back needs a "
              "quoted basis and is otherwise refused")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


if __name__ == "__main__":
    test_release_exempts_only_transfers_on_or_after_its_effective_date()
    test_partial_release_leaves_other_parcels_encumbered()
    test_applying_releases_never_overwrites_an_existing_exemption()
    test_covenant_wide_release_covers_every_parcel()
    test_malformed_releases_are_refused()
    test_earliest_effective_release_wins()
    test_fee_compute_honours_a_release_recorded_after_the_fact()
    test_effective_date_defaults_to_recording_and_refuses_to_reach_back()
    print("\nall covenant-release tests passed")
