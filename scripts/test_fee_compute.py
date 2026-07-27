"""Smoke test for app/title/fee_compute.py.

Runs live against covid 2497 (Bexar) and covid 3595 (Douglas Co, CO) --
both already chain-walked (app/title/chain.py) earlier this project.
Between them they cover every real branch this module has hit so far:
confirmed-exempt (no fee_collection row at all), and fee-owed with no
known price (Texas is non-disclosure -- base_amount stays null, never
guessed). Neither real covenant has a fee-owed transfer with a KNOWN
price, so that arithmetic is covered by a synthetic, rolled-back transfer
instead (never persisted).

Usage: python3 scripts/test_fee_compute.py
"""
import sys
from datetime import date

sys.path.insert(0, ".")

from app.db.session import SessionLocal, get_session
from app.config import DB_SCHEMA
from app.db.repository import upsert_transfer
from app.title.fee_compute import compute_fee_for_transfer, compute_fees_for_covid
from sqlalchemy import text


def test_confirmed_exempt_transfers_get_no_fee_row() -> None:
    """Bexar covid 2497's declarant_sale and foreclosure transfers, and
    Douglas Co covid 3595's 6 declarant_sale transfers -- all confirmed
    exempt by chain.py, none of them should produce a fee_collection row."""
    with get_session() as session:
        result_2497 = compute_fees_for_covid(session, 2497)
        result_3595 = compute_fees_for_covid(session, 3595)

    assert result_2497["20170019251/447638"]["fee_owed"] is False, result_2497
    assert result_2497["20210002202/447638"]["fee_owed"] is False, result_2497
    assert all(r["fee_owed"] is False for r in result_3595.values()), result_3595
    print("PASS: confirmed-exempt transfers (declarant_sale, foreclosure) -> no fee owed, no fee_collection row")


def test_pre_effective_date_exempt_despite_unconfirmed_declarant_link() -> None:
    """Montgomery covid 3297's parcel 93070: chain.py's recorder-portal walk
    couldn't confirm the chain traces back to the declarant (an intermediate
    builder, Long Lake Ltd, isn't surfaced by either seed search -- a real,
    common pattern, see app/title/chain.py's own docstring), so this
    transfer's review_flag started out forced True purely from that
    uncertainty. That was a real bug: the transfer is ALSO independently
    classified pre_effective_date (a pure recording-date fact, unaffected by
    who the grantor is), and fee_compute's own exemption check
    (`exemption_category is not None and not review_flag`) was silently
    losing that confirmed exemption and computing a fee that isn't actually
    owed. chain.py now only sets review_flag from the unconfirmed-declarant-
    link case when no independent classification exists -- this guards the
    fee-compute-visible outcome, not just chain.py's own output."""
    with get_session() as session:
        result = compute_fees_for_covid(session, 3297, tract_no=1)

    link = result["2012093064/93070"]
    assert link["fee_owed"] is False, link
    assert link["reason"] == "confirmed exempt (pre_effective_date)", link
    print("PASS: pre_effective_date exemption still holds even when the chain's declarant link is unconfirmed")


def test_fee_owed_with_unknown_price_leaves_base_amount_null() -> None:
    """Bexar covid 2497's BHA Bandera Road LLC -> GS Ventures Group LLC
    transfer: not auto-classifiable as exempt, so fee_owed -- but Texas is
    non-disclosure, so there's no known price. base_amount must stay null,
    never estimated, while fee_percent_applied (a real, known fact from
    the covenant's own text) is still recorded."""
    with get_session() as session:
        result = compute_fees_for_covid(session, 2497)

    link = result["20210145173/447638"]
    assert link["fee_owed"] is True, link
    assert link["fee_percent_applied"] == 1.0, link
    assert link["base_amount"] is None, link
    assert link["amount_due"] is None, link
    assert link["needs_review"] is True, link
    print("PASS: fee-owed transfer with unknown price -> fee_percent_applied recorded, "
          "base_amount correctly left null (never estimated)")


def test_fee_owed_with_known_price_computes_correct_amount() -> None:
    """No real covenant in this project's sample has both a fee-owed
    transfer AND a known price at once (Colorado's is exempt; Bexar's
    fee-owed transfer has no disclosed price) -- so the actual dollar
    arithmetic is verified here with a synthetic transfer instead, rolled
    back and never persisted."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        upsert_transfer(
            session, county_fips="08035", instrument_number="TEST-SYNTHETIC-0001",
            covid=3595, tract_no=1, parcel_county_fips="08035", parcel_apn="R0334407",
            prior_county_fips=None, prior_instrument_number=None,
            instrument_type="Warranty Deed", recording_date="2022-06-01", book=None, page=None,
            grantor_contact_id=None, grantee_contact_id=None,
            consideration_amount=250000.00, legal_description_snapshot=None, recorder_source_id=None,
            review_flag=True, review_reason="synthetic test transfer, not a real conveyance",
            exemption_category=None, exemption_basis=None, exemption_confidence=None,
        )
        result = compute_fee_for_transfer(session, "08035", "TEST-SYNTHETIC-0001", date(2022, 6, 1), "R0334407")
    finally:
        session.rollback()  # never persisted
        session.close()

    assert result["fee_owed"] is True, result
    assert result["fee_percent_applied"] == 1.0, result
    assert result["base_amount"] == 250000.0, result
    assert result["amount_due"] == 2500.0, result
    print("PASS: fee-owed transfer with known price -> 1% of $250,000.00 = $2,500.00")


if __name__ == "__main__":
    test_confirmed_exempt_transfers_get_no_fee_row()
    test_pre_effective_date_exempt_despite_unconfirmed_declarant_link()
    test_fee_owed_with_unknown_price_leaves_base_amount_null()
    test_fee_owed_with_known_price_computes_correct_amount()
    print("\nall fee-compute smoke tests passed")
