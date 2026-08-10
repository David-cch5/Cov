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
    is_fully_released,
    settle_prior_fees,
    termination_fee_conflicts,
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
            record_release(session, covid=COVID, release_type="termination", validity_status="valid",
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
            record_release(session, covid=COVID, release_type="buyout", validity_status="valid",
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
            record_release(session, covid=COVID, release_type="termination", validity_status="valid",
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
            record_release(session, covid=COVID, release_type="termination", validity_status="valid",
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
            record_release(session, covid=COVID, release_type="buyout", validity_status="valid",
                           effective_date=date(2022, 1, 1), scope="partial",
                           parcels=[(COUNTY, APN_A)])
            record_release(session, covid=COVID, release_type="termination", validity_status="valid",
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
            record_release(session, covid=COVID, release_type="buyout", validity_status="valid",
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
    supplied independently. An earlier effective date on a PROSPECTIVE release is
    a contradiction and is refused outright: an instrument that really reaches
    back is recorded as effect='void_ab_initio', which has its own evidence
    requirement. One expression for reaching back, so the code and migration
    0038's CHECK constraint cannot disagree."""
    try:
        with get_session() as session:
            _setup(session)
            got = record_release(session, covid=COVID, release_type="termination", validity_status="valid",
                                 scope="covenant", recording_date=date(2020, 6, 1))
            assert got["effective_date"] == date(2020, 6, 1), got

            try:
                record_release(session, covid=COVID, release_type="termination", validity_status="valid",
                               scope="covenant", recording_date=date(2020, 6, 1),
                               effective_date=date(2019, 1, 1))
                raise AssertionError("expected a ValueError for a retroactive effective date")
            except ValueError as exc:
                assert "void_ab_initio" in str(exc), exc

            # Reaching back is expressed as effect='void_ab_initio', never as a
            # prospective release with an earlier date -- one expression, so the
            # code and the schema CHECK cannot disagree.
            back = record_release(session, covid=COVID, release_type="termination", validity_status="valid",
                                  scope="covenant", effect="void_ab_initio",
                                  recording_date=date(2020, 6, 1),
                                  no_intervening_conveyance_affidavit=True)
            assert back["effect"] == "void_ab_initio", back
            session.rollback()
        print("PASS: effective_date defaults to the recording date; a prospective release "
              "cannot predate its own recording -- reaching back is void_ab_initio")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_void_ab_initio_release_reaches_the_covenants_whole_life() -> None:
    """Read from a real recorded instrument -- Transylvania County NC 2010004621,
    French Broad Place LLC: "The Instrument shall be terminated and declared to be
    null and void, IN THE SAME MANNER AS IF IT HAD NEVER BEEN RECORDED ... has
    never constituted a lawful restriction upon the property."

    That reaches back to inception, so even a transfer recorded BEFORE the
    termination is released -- the opposite of the prospective form, and both are
    real. What licenses it is the sworn statement in the same instrument that
    nothing was conveyed since the covenant was filed, so there is no accrued fee
    to void."""
    try:
        with get_session() as session:
            _setup(session)
            record_release(
                session, covid=COVID, release_type="termination", validity_status="valid", scope="covenant",
                effect="void_ab_initio", recording_date=date(2010, 9, 16),
                execution_date=date(2010, 9, 10),
                no_intervening_conveyance_affidavit=True,
                terminates_instrument="Book 529 Page 410",
                terminated_under="Paragraph 25",
                recording_instrument="2010004621")
            session.commit()
            before = release_for_transfer(session, COVID, COUNTY, APN_A, date(2019, 3, 1))
        assert before is not None and before["void_ab_initio"] is True, before
        assert before["releases_transfer"] is True, before   # reaches back
        assert before["needs_review"] is False, before       # affidavit present
        print("PASS: a void-ab-initio release reaches transfers predating it, on the strength "
              "of the sworn no-conveyance statement")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_retroactive_without_the_affidavit_reports_but_does_not_apply() -> None:
    """Reaching back without the sworn statement may be voiding a fee that was
    genuinely collected, so the release is reported and flagged rather than
    applied. Recording it needs the language quoted instead."""
    try:
        with get_session() as session:
            _setup(session)
            try:
                record_release(session, covid=COVID, release_type="termination", validity_status="valid",
                               scope="covenant", effect="void_ab_initio",
                               recording_date=date(2010, 9, 16))
                raise AssertionError("expected a ValueError with neither affidavit nor basis")
            except ValueError as exc:
                assert "no_intervening_conveyance_affidavit" in str(exc), exc

            record_release(session, covid=COVID, release_type="termination", validity_status="valid",
                           scope="covenant", effect="void_ab_initio",
                           recording_date=date(2010, 9, 16),
                           retroactive_basis='"null and void ... as if it had never been recorded"')
            session.commit()
            got = release_for_transfer(session, COVID, COUNTY, APN_A, date(2019, 3, 1))
        assert got["releases_transfer"] is False, got
        assert got["needs_review"] is True, got
        assert any("sworn" in r for r in got["review_reasons"]), got
        print("PASS: retroactive without the sworn statement is reported and flagged, not applied")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_unexecuted_acknowledgement_is_not_an_effective_termination() -> None:
    """The Williamson County instrument (2019003560) is only "fully effective" once
    the Trustee acknowledges it, and in the copy on hand that acknowledgement is
    UNEXECUTED -- blank day, no signature. A pending acknowledgement must not read
    as an effective termination."""
    try:
        with get_session() as session:
            _setup(session)
            record_release(session, covid=COVID, release_type="termination", validity_status="valid",
                           scope="covenant", recording_date=date(2019, 1, 15),
                           execution_date=date(2019, 1, 3),
                           acknowledgement_required=True, acknowledged_date=None,
                           recording_instrument="2019003560",
                           terminates_instrument="2009082853",
                           referenced_instruments=["2012005120", "2015005262", "2018004487"],
                           terminated_under="Paragraph 25")
            session.commit()
            pending = release_for_transfer(session, COVID, COUNTY, APN_A, date(2021, 3, 1))
            session.execute(text("UPDATE covenant_release SET acknowledged_date = :d "
                                 "WHERE covid = :c"), {"d": date(2019, 1, 20), "c": COVID})
            session.commit()
            signed = release_for_transfer(session, COVID, COUNTY, APN_A, date(2021, 3, 1))
        assert pending["releases_transfer"] is False and pending["needs_review"] is True, pending
        assert any("acknowledgement" in r for r in pending["review_reasons"]), pending
        assert signed["releases_transfer"] is True, signed
        print("PASS: an unexecuted acknowledgement is not an effective termination; signing it "
              "makes the release apply")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def _add_fee(session, instrument, when, invoiced=None, collected=None) -> None:
    session.execute(text("""
        INSERT INTO fee_collection (county_fips, instrument_number, collection_seq,
                                    recording_date, parcel_apn, fee_percent_applied,
                                    base_amount, invoiced_amount, collected_amount, status)
        VALUES (:cf, :inst, 1, :rd, :apn, 1.0, 100000, :inv, :col,
                CASE WHEN :col IS NOT NULL THEN 'paid'
                     WHEN :inv IS NOT NULL THEN 'invoiced' ELSE 'owed' END)
        ON CONFLICT DO NOTHING
    """), {"cf": COUNTY, "inst": instrument, "rd": when, "apn": APN_A,
           "inv": invoiced, "col": collected})


def test_buyout_settles_prior_fees_by_linking_not_deleting() -> None:
    """A buyout's consideration may cover fees accrued before it, depending on the
    agreement. When it does, those fees are DISCHARGED, not erased: the
    fee_collection row keeps its base_amount and what it was owed on, and gains
    settled_by_release_id plus status='paid' -- because it was paid, as part of a
    larger consideration. 'waived' would say the opposite, that a still-owed fee
    was forgone."""
    try:
        with get_session() as session:
            _setup(session)
            _add_fee(session, "BEFORE-REL", date(2019, 3, 1))
            got = record_release(session, covid=COVID, release_type="buyout", validity_status="valid",
                                 scope="covenant", recording_date=EFFECTIVE,
                                 consideration_amount=250000, settles_prior_fees=True,
                                 settlement_note="agreement recites the price includes "
                                                 "all fees accrued to closing")
            result = settle_prior_fees(session, got["release_id"])
            session.commit()
            row = session.execute(text("""
                SELECT base_amount, status, settled_by_release_id FROM fee_collection
                WHERE county_fips = :cf AND instrument_number = 'BEFORE-REL'
            """), {"cf": COUNTY}).mappings().one()
        assert result["settled"] == 1, result
        assert row["settled_by_release_id"] == got["release_id"], dict(row)
        assert row["status"] == "paid", dict(row)
        assert float(row["base_amount"]) == 100000.0, dict(row)   # the record survives
        print("PASS: a buyout discharges prior fees by linking them, preserving what each "
              "was owed on")
    finally:
        with get_session() as session:
            session.execute(text("DELETE FROM fee_collection WHERE county_fips = :cf "
                                 "AND instrument_number IN ('BEFORE-REL','AFTER-REL')"),
                            {"cf": COUNTY})
            _teardown(session); session.commit()


def test_a_termination_cannot_settle_prior_fees_and_reports_them_instead() -> None:
    """A validly terminated covenant had no prior sales -- these instruments swear
    to it. So there are no accrued fees for a termination to settle, and a fee
    predating one means either the termination is not valid as to that land or the
    fee record is wrong. Both are conflicts for a human; a termination pays for
    nothing, so it cannot absorb them."""
    try:
        with get_session() as session:
            _setup(session)
            _add_fee(session, "BEFORE-REL", date(2019, 3, 1))
            try:
                record_release(session, covid=COVID, release_type="termination", validity_status="valid",
                               scope="covenant", recording_date=EFFECTIVE,
                               settles_prior_fees=True)
                raise AssertionError("expected a ValueError: a termination settles nothing")
            except ValueError as exc:
                assert "only a buyout" in str(exc), exc

            record_release(session, covid=COVID, release_type="termination", validity_status="valid",
                           scope="covenant", recording_date=EFFECTIVE)
            session.commit()
            conflicts = termination_fee_conflicts(session, COVID)
        assert len(conflicts) == 1, conflicts
        assert conflicts[0]["instrument_number"] == "BEFORE-REL", conflicts
        print(f"PASS: a termination refuses to settle prior fees and reports them as a "
              f"conflict ({len(conflicts)} found)")
    finally:
        with get_session() as session:
            session.execute(text("DELETE FROM fee_collection WHERE county_fips = :cf "
                                 "AND instrument_number IN ('BEFORE-REL','AFTER-REL')"),
                            {"cf": COUNTY})
            _teardown(session); session.commit()


def test_a_fee_with_payment_history_is_a_conflict_not_a_settlement() -> None:
    """A fee already invoiced or partly collected has a payment history of its own,
    which a buyout cannot silently absorb -- the money moved, and the agreement has
    to be reconciled against that rather than papered over."""
    try:
        with get_session() as session:
            _setup(session)
            _add_fee(session, "BEFORE-REL", date(2019, 3, 1), invoiced=1000)
            got = record_release(session, covid=COVID, release_type="buyout", validity_status="valid",
                                 scope="covenant", recording_date=EFFECTIVE,
                                 consideration_amount=250000, settles_prior_fees=True)
            result = settle_prior_fees(session, got["release_id"])
            session.commit()
            row = session.execute(text("""
                SELECT status, settled_by_release_id FROM fee_collection
                WHERE county_fips = :cf AND instrument_number = 'BEFORE-REL'
            """), {"cf": COUNTY}).mappings().one()
        assert result["settled"] == 0, result
        assert len(result["conflicts"]) == 1, result
        assert row["settled_by_release_id"] is None, dict(row)
        assert row["status"] == "invoiced", dict(row)          # untouched
        print("PASS: an already-invoiced fee is reported as a conflict, never silently "
              "absorbed into a buyout")
    finally:
        with get_session() as session:
            session.execute(text("DELETE FROM fee_collection WHERE county_fips = :cf "
                                 "AND instrument_number IN ('BEFORE-REL','AFTER-REL')"),
                            {"cf": COUNTY})
            _teardown(session); session.commit()


def test_a_fully_released_covenant_is_historic_not_research() -> None:
    """A fully released covenant is worth recording and not worth researching --
    anchoring is the most expensive thing this project does, and spending it to
    locate land whose covenant no longer exists is pure waste.

    The three negative cases matter as much as the positive one. A PARTIAL release
    leaves land encumbered. An unexecuted acknowledgement, or a retroactive release
    with no sworn no-conveyance statement, is exactly the situation where the
    covenant may still be live -- skipping research there would assume the answer
    rather than establish it."""
    try:
        with get_session() as session:
            _setup(session)
            assert is_fully_released(session, COVID) is None, "nothing released yet"

            # partial -- land still encumbered, so still worth working
            record_release(session, covid=COVID, release_type="buyout", validity_status="valid", scope="partial",
                           parcels=[(COUNTY, APN_A)], recording_date=EFFECTIVE)
            session.commit()
            assert is_fully_released(session, COVID) is None, "a partial release is not historic"

            # covenant-wide but acknowledgement pending -- may still be live
            record_release(session, covid=COVID, release_type="termination", validity_status="valid", scope="covenant",
                           recording_date=EFFECTIVE, acknowledgement_required=True,
                           acknowledged_date=None, recording_instrument="PENDING-ACK")
            session.commit()
            assert is_fully_released(session, COVID) is None, "a pending acknowledgement is not historic"

            # covenant-wide and effective
            record_release(session, covid=COVID, release_type="termination", validity_status="valid", scope="covenant",
                           recording_date=EFFECTIVE, recording_instrument="TERM-FULL")
            session.commit()
            got = is_fully_released(session, COVID)
        assert got is not None and got["recording_instrument"] == "TERM-FULL", got
        print("PASS: only an effective covenant-wide release makes a covenant historic; "
              "partial and needs-review releases do not")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_anchor_resolution_skips_a_released_covenant_by_default() -> None:
    """The guard where the money is. resolve_metes_and_bounds_anchor escalates to
    Opus and then Fable; it must not reach either for a covenant that no longer
    exists, and must say so rather than failing silently."""
    from app.gis.anchor_resolver import resolve_metes_and_bounds_anchor
    try:
        with get_session() as session:
            _setup(session)
            record_release(session, covid=COVID, release_type="termination", validity_status="valid", scope="covenant",
                           recording_date=EFFECTIVE, recording_instrument="TERM-FULL")
            session.commit()
            got = resolve_metes_and_bounds_anchor(session, covid=COVID, tract_no=1)
        assert got["committed"] is False, got
        assert got["tier"] == "skipped_released", got
        assert "research_released=True" in got["reason"], got
        print("PASS: anchor resolution skips a released covenant before any paid tier, and "
              "names the override")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_a_found_termination_asserts_nothing_until_adjudicated() -> None:
    """The default, and the reason it is the default. Every covenant ingested here
    is valid as of today, and some terminations on record are invalid -- answered by
    recording a rescission, not by treating the covenant as over.

    So a discovered termination is captured and NOTHING follows from it: no fee
    exemption, no settlement, and it does not make the covenant historic. Marking it
    valid is a separate, deliberate act. Were it the other way round, a termination
    later held invalid would have silently stopped collection on a live covenant in
    the meantime."""
    try:
        with get_session() as session:
            _setup(session)
            found = record_release(session, covid=COVID, release_type="termination",
                                  scope="covenant", recording_date=EFFECTIVE,
                                  recording_instrument="FOUND-IN-RECORDS")
            session.commit()
            assert found["validity_status"] == "pending_review", found

            # nothing follows from it
            assert release_for_transfer(session, COVID, COUNTY, APN_A, date(2021, 3, 1)) is None
            assert is_fully_released(session, COVID) is None
            assert apply_releases_to_transfers(session, COVID)["transfers_exempted"] == 0

            # adjudicating it valid is what makes it operate
            session.execute(text("UPDATE covenant_release SET validity_status = 'valid', "
                                 "adjudicated_at = now() WHERE covid = :c"), {"c": COVID})
            session.commit()
            assert release_for_transfer(session, COVID, COUNTY, APN_A, date(2021, 3, 1)) is not None
            assert is_fully_released(session, COVID) is not None
        print("PASS: a found termination asserts nothing until adjudicated valid")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_an_invalid_termination_releases_nothing_and_carries_its_rescission() -> None:
    """A termination held invalid is answered by recording a rescission that voids
    it. It never releases anything, and the rescission is stored against the
    instrument it voids -- a rescission read without its termination says nothing.

    The database also refuses a rescission on a release that is valid or still
    pending, because either would be a contradiction."""
    try:
        with get_session() as session:
            _setup(session)
            record_release(session, covid=COVID, release_type="termination",
                           scope="covenant", recording_date=EFFECTIVE,
                           recording_instrument="BAD-TERM-2020",
                           validity_status="invalid",
                           validity_note="executed by a party with no authority to terminate")
            session.execute(text("""
                UPDATE covenant_release
                SET rescission_instrument = 'RESCIND-2021-14',
                    rescission_recording_date = :d
                WHERE covid = :c
            """), {"d": date(2021, 2, 1), "c": COVID})
            session.commit()
            assert release_for_transfer(session, COVID, COUNTY, APN_A, date(2021, 3, 1)) is None
            assert is_fully_released(session, COVID) is None
            row = session.execute(text("SELECT validity_status, rescission_instrument "
                                       "FROM covenant_release WHERE covid = :c"),
                                  {"c": COVID}).mappings().one()
            assert row["rescission_instrument"] == "RESCIND-2021-14", dict(row)

            # a rescission cannot attach to a release that is not invalid
            try:
                session.execute(text("UPDATE covenant_release SET validity_status = 'valid' "
                                     "WHERE covid = :c"), {"c": COVID})
                session.commit()
                raise AssertionError("expected the CHECK to refuse a rescission on a valid release")
            except Exception as exc:
                session.rollback()
                assert "rescission_only_when_invalid" in str(exc), exc
        print("PASS: an invalid termination releases nothing, carries its rescission, and the "
              "rescission cannot be attached to a valid release")
    finally:
        with get_session() as session:
            _teardown(session); session.commit()


def test_a_buyout_is_always_prospective() -> None:
    """A buyout is negotiated to stop FUTURE collection -- after its effective date
    those parcels no longer collect fees. That makes it inherently prospective.

    Reaching back to inception is a TERMINATION shape: the covenant declared never
    to have been a lawful restriction at all. Nobody negotiates and pays for that,
    so a void-ab-initio buyout is refused in code and by a CHECK constraint."""
    try:
        with get_session() as session:
            _setup(session)
            # Refused before any SQL runs, so the fixture is untouched -- no rollback,
            # which would take _setup with it.
            try:
                record_release(session, covid=COVID, release_type="buyout", scope="covenant",
                               effect="void_ab_initio", recording_date=EFFECTIVE,
                               no_intervening_conveyance_affidavit=True,
                               validity_status="valid")
                raise AssertionError("expected a ValueError: a buyout cannot be void ab initio")
            except ValueError as exc:
                assert "always prospective" in str(exc), exc

            # A termination MAY reach back -- that asymmetry is the point.
            record_release(session, covid=COVID, release_type="termination", scope="partial",
                           parcels=[(COUNTY, APN_B)], effect="void_ab_initio",
                           recording_date=EFFECTIVE,
                           no_intervening_conveyance_affidavit=True, validity_status="valid")
            # The buyout's own effect: fees stop AFTER the date, nothing before.
            record_release(session, covid=COVID, release_type="buyout", scope="partial",
                           parcels=[(COUNTY, APN_A)], recording_date=EFFECTIVE,
                           consideration_amount=250000, validity_status="valid")
            session.commit()

            after = release_for_transfer(session, COVID, COUNTY, APN_A, date(2021, 3, 1))
            before = release_for_transfer(session, COVID, COUNTY, APN_A, date(2019, 3, 1))
            reaches_back = release_for_transfer(session, COVID, COUNTY, APN_B, date(2019, 3, 1))
        assert after is not None and after["releases_transfer"] is True, after
        assert before is None, before                      # the buyout leaves the past alone
        assert reaches_back is not None and reaches_back["void_ab_initio"] is True, reaches_back
        print("PASS: a buyout is always prospective -- it stops future collection and leaves "
              "earlier transfers alone; only a termination may reach back")
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
    test_void_ab_initio_release_reaches_the_covenants_whole_life()
    test_retroactive_without_the_affidavit_reports_but_does_not_apply()
    test_unexecuted_acknowledgement_is_not_an_effective_termination()
    test_buyout_settles_prior_fees_by_linking_not_deleting()
    test_a_termination_cannot_settle_prior_fees_and_reports_them_instead()
    test_a_fee_with_payment_history_is_a_conflict_not_a_settlement()
    test_a_fully_released_covenant_is_historic_not_research()
    test_anchor_resolution_skips_a_released_covenant_by_default()
    test_a_found_termination_asserts_nothing_until_adjudicated()
    test_an_invalid_termination_releases_nothing_and_carries_its_rescission()
    test_a_buyout_is_always_prospective()
    print("\nall covenant-release tests passed")
