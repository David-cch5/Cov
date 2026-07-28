"""Smoke test for app/title/payoff.py -- the payoff/resale-certificate
generator fee_compute.py's own docstring explicitly deferred, and the reason
migration 0029 (parcel_lineage) exists at all: a fee owed on a bulk transfer
attaches to the land, not to whatever apn exists today, so a payoff request
against a lot several splits removed from that transfer has to walk
parcel_lineage backward to find it.

The direct case (no lineage hop at all -- the queried apn IS the ancestor
that owes the fee) is run for real against real, already-computed
fee_collection rows (covid 4123 / Douglas Co, apn R0460303, four separate
historical transfers -- confirmed real via a direct query before this was
built). Generating a payoff statement is itself the real, intended output of
this capability, the same way a clean reconcile_tract run or a "nothing new"
monitor re-check is -- not something to roll back.

The multi-hop case can't be exercised against a genuine multi-split chain
yet (no real parcel has ever actually split in this project's data), so it's
tested with synthetic parcel_lineage rows chaining a fake descendant apn,
through one real intermediate apn, up to the same real R0460303 obligation
-- proving the recursive walk finds an ancestor several hops away, not just
the immediate parent. Rolled back.

Usage: python3 scripts/test_payoff.py
"""
import sys
from datetime import date

sys.path.insert(0, ".")

from sqlalchemy import text

from app.config import DB_SCHEMA
from app.db.session import SessionLocal, get_session
from app.title.payoff import find_lineage_ancestors, generate_payoff_statement

GOOD_THROUGH = date(2026, 7, 28)


def test_generate_payoff_statement_direct_real_obligations() -> None:
    """Real, already-computed fee_collection rows for Douglas Co apn
    R0460303 (covid 4123): four separate historical transfers, each with its
    own known base_amount, fee_percent_applied=1%, unpaid_interest_percent=
    18% -- one payoff statement per obligation, simple interest from each
    one's own due_date through 2026-07-28. Run for real -- generating these
    statements is the actual point of this function, not test scaffolding."""
    with get_session() as session:
        statements = generate_payoff_statement(
            session, county_fips="08035", apn="R0460303", good_through_date=GOOD_THROUGH,
        )
    assert len(statements) == 4, statements
    by_instrument = {s["instrument_number"]: s for s in statements}

    # 2021118509: $4,811,000 base x 1% = $48,110 principal, due 2021-10-18 ->
    # 2026-07-28 is 1745 days at 18%/yr simple interest.
    s = by_instrument["2021118509"]
    assert abs(s["principal_amount"] - 48110.0) < 0.01, s
    days = (GOOD_THROUGH - date(2021, 10, 18)).days
    expected_interest = 48110.0 * 0.18 / 365 * days
    assert abs(s["accrued_interest_amount"] - expected_interest) < 0.01, s
    assert abs(s["total_payoff_amount"] - (48110.0 + expected_interest)) < 0.01, s

    # 2019042081: base_amount = $0.00 -- a real, known price of zero (not a missing
    # price), so it still gets a statement, correctly computed as all zeros.
    zero = by_instrument["2019042081"]
    assert zero["principal_amount"] == 0.0 and zero["total_payoff_amount"] == 0.0, zero
    print("PASS: generate_payoff_statement (Douglas Co apn R0460303) -> 4 real "
          "obligations, each correctly computed with simple interest from its own due_date")


def test_generate_payoff_statement_skips_unknown_price() -> None:
    """Montgomery covid 3297's parcel 93088 has two real 'owed' fee_collection
    rows, both with base_amount NULL (a non-disclosure-state covenant --
    price was never on file). Never guessed -- no statement for either,
    matching compute_fee_for_transfer's own "leave it null, don't estimate"
    rule. Rolled back (nothing to commit -- this asserts an ABSENCE)."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        statements = generate_payoff_statement(
            session, county_fips="48339", apn="93088", good_through_date=GOOD_THROUGH,
        )
        assert statements == [], statements
    finally:
        session.rollback()
        session.close()
    print("PASS: generate_payoff_statement (Montgomery apn 93088) -> no known base_amount "
          "means no statement generated, never a guessed one")


def test_find_lineage_ancestors_multi_hop() -> None:
    """Synthetic 2-hop chain: TESTDESCENDANT -> R0497417 (a real Douglas Co
    apn, standing in as an intermediate node) -> R0460303 (the real apn that
    actually carries the unpaid obligation). Confirms the recursive walk
    finds an ancestor several hops away, not just the immediate parent.
    Rolled back -- no real parcel has ever actually split in this project's
    data yet to test this against directly."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        # TESTDESCENDANT needs its own row in `parcel` to satisfy parcel_lineage's FK --
        # reuses R0497417's own real geometry rather than fabricating new shape data.
        session.execute(text("""
            INSERT INTO parcel (county_fips, apn, owner_name_raw, situs_address, acreage, geom)
            SELECT county_fips, 'TESTDESCENDANT', owner_name_raw, situs_address, acreage, geom
            FROM parcel WHERE county_fips = '08035' AND apn = 'R0497417'
        """))
        session.execute(text("""
            INSERT INTO parcel_lineage (county_fips, apn, parent_county_fips, parent_apn, lineage_type)
            VALUES ('08035', 'TESTDESCENDANT', '08035', 'R0497417', 'subdivision_split')
        """))
        session.execute(text("""
            INSERT INTO parcel_lineage (county_fips, apn, parent_county_fips, parent_apn, lineage_type)
            VALUES ('08035', 'R0497417', '08035', 'R0460303', 'subdivision_split')
        """))

        ancestors = find_lineage_ancestors(session, "08035", "TESTDESCENDANT")
        assert ("08035", "R0497417") in ancestors, ancestors
        assert ("08035", "R0460303") in ancestors, ancestors

        statements = generate_payoff_statement(
            session, county_fips="08035", apn="TESTDESCENDANT", good_through_date=GOOD_THROUGH,
        )
        by_instrument = {s["instrument_number"]: s for s in statements}
        assert "2021118509" in by_instrument, statements
        assert by_instrument["2021118509"]["ancestor_apn"] == "R0460303", statements
    finally:
        session.rollback()
        session.close()
    print("PASS: find_lineage_ancestors / generate_payoff_statement -> a lot 2 splits "
          "removed correctly traces back to its real ancestor's unpaid obligation")


if __name__ == "__main__":
    test_generate_payoff_statement_direct_real_obligations()
    test_generate_payoff_statement_skips_unknown_price()
    test_find_lineage_ancestors_multi_hop()
    print("\nall payoff smoke tests passed")
