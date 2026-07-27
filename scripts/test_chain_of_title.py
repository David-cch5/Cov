"""Smoke test for app/title/chain.py -- the chain-of-title walker.

Runs live against covid 2497 (Bexar, template V11, 0.452 ac single-lot
assemblage): the smallest-acreage Texas covenant with both an active GIS
match and an active recorder adapter, picked specifically to test this
walker against a short, real chain. Not rolled back -- upserts are
idempotent (transfer PK, and the covenant.review_reason gap note is
guarded against duplication), so re-running this is safe and simply
reconfirms the same result against the live portal.

Usage: python3 scripts/test_chain_of_title.py
"""
import sys
from datetime import date
from types import SimpleNamespace

sys.path.insert(0, ".")

from app.db.session import get_session
from app.title.chain import _affidavit_gate_note, _classify_pre_effective_date, walk_chain_of_title


def test_classify_pre_effective_date_fixed_date() -> None:
    """V01's real rule: fixed cutoff 2013-01-01, well after this covenant's
    own 2009 recording -- the actual gap that motivated this feature (a
    transfer in that multi-year window was previously left unclassified)."""
    covenant = SimpleNamespace(recording_date=date(2009, 9, 18))
    rules = {"pre_effective_date": {"cutoff_date": date(2013, 1, 1), "cutoff_basis": "fixed_date",
                                     "clause_reference": "6(i)"}}

    category, basis = _classify_pre_effective_date(date(2012, 12, 31), covenant, rules)
    assert category == "pre_effective_date", (category, basis)

    category, basis = _classify_pre_effective_date(date(2013, 1, 1), covenant, rules)
    assert category is None, (category, basis)  # cutoff date itself is not "before" it

    category, basis = _classify_pre_effective_date(date(2013, 1, 2), covenant, rules)
    assert category is None, (category, basis)

    print("PASS: _classify_pre_effective_date (fixed_date) -> correct on both sides of the boundary")


def test_classify_pre_effective_date_recording_date_basis() -> None:
    """V11's rule (Bexar covid 2497's own template): cutoff_basis=
    'recording_date' means exempt only before the COVENANT's own recording
    date -- in practice never reachable in the live walkers today (they
    already skip anything before covenant.recording_date before
    classification runs at all), but must still be correct in isolation."""
    covenant = SimpleNamespace(recording_date=date(2009, 3, 11))
    rules = {"pre_effective_date": {"cutoff_date": None, "cutoff_basis": "recording_date",
                                     "clause_reference": "6(j)"}}

    category, basis = _classify_pre_effective_date(date(2009, 3, 10), covenant, rules)
    assert category == "pre_effective_date", (category, basis)

    category, basis = _classify_pre_effective_date(date(2009, 3, 11), covenant, rules)
    assert category is None, (category, basis)

    print("PASS: _classify_pre_effective_date (recording_date basis) -> correct on both sides of the boundary")


def test_classify_pre_effective_date_no_rule() -> None:
    """A template with no pre_effective_date row at all (or a fixed_date
    rule missing its own cutoff_date) must never fabricate a cutoff."""
    covenant = SimpleNamespace(recording_date=date(2009, 1, 1))
    assert _classify_pre_effective_date(date(2000, 1, 1), covenant, {}) == (None, None)
    broken_rules = {"pre_effective_date": {"cutoff_date": None, "cutoff_basis": "fixed_date",
                                            "clause_reference": "6(i)"}}
    assert _classify_pre_effective_date(date(2000, 1, 1), covenant, broken_rules) == (None, None)
    print("PASS: _classify_pre_effective_date (no rule) -> never assumes a cutoff that isn't there")


def test_affidavit_gate_note() -> None:
    """V02/V03/V12's own real text (migration 0030): death_probate,
    foreclosure, affiliate_transaction, and trustee_unidentified only
    apply if a Grantor's affidavit was filed -- confirmed absent from
    V01/V06/V08/V11/V13/V18's own text, so this must not fire for a
    category/template combination that isn't actually gated."""
    gated_rules = {"foreclosure": {"requires_grantor_affidavit": True}}
    note = _affidavit_gate_note("foreclosure", gated_rules)
    assert note is not None and "affidavit" in note, note

    ungated_rules = {"foreclosure": {"requires_grantor_affidavit": False}}
    assert _affidavit_gate_note("foreclosure", ungated_rules) is None

    assert _affidavit_gate_note("declarant_sale", gated_rules) is None  # not in the gated rules dict
    assert _affidavit_gate_note(None, gated_rules) is None  # no category classified at all yet
    print("PASS: _affidavit_gate_note -> fires only for a category the template actually gates")


def test_walk_chain_bexar_2497() -> None:
    """Three real Transfers of Title since the covenant was recorded, per
    Bexar CAD's own deed history (the recorder-portal name-walk this
    module falls back to for counties without that CAD API missed the
    last two entirely -- see this module's docstring):
      1. 2017 declarant sale (Abramoff -> Oggnim LLC) -- exempt, declarant_sale.
      2. 2021 foreclosure (Oggnim LLC -> BHA Bandera Road LLC) -- exempt, foreclosure.
      3. 2021 resale (BHA Bandera Road LLC -> GS Ventures Group LLC) -- fee owed,
         not auto-classifiable, correctly flagged for review.
    The walk's final holder now matches Bexar CAD's current owner exactly."""
    with get_session() as session:
        outer = walk_chain_of_title(session, covid=2497)

    assert outer["walked"], outer
    assert outer["method"] == "cad_deed_history", outer
    assert outer["parcel_count"] == 1, outer
    result = outer["parcels"]["447638"]
    assert len(result["chain"]) == 3, result["chain"]
    l1, l2, l3 = result["chain"]

    assert l1["instrument_number"] == "20170019251", l1
    assert (l1["grantor"], l1["grantee"]) == ("ABRAMOFF EFRAIM", "OGGNIM LLC"), l1
    assert l1["exemption_category"] == "declarant_sale", l1
    assert l1["review_flag"] is False, l1

    assert l2["instrument_number"] == "20210002202", l2
    assert (l2["grantor"], l2["grantee"]) == ("OGGNIM LLC", "BHA BANDERA ROAD LLC"), l2
    assert l2["exemption_category"] == "foreclosure", l2
    assert l2["review_flag"] is False, l2

    assert l3["instrument_number"] == "20210145173", l3
    assert (l3["grantor"], l3["grantee"]) == ("BHA BANDERA ROAD LLC", "GS VENTURES GROUP LLC"), l3
    assert l3["exemption_category"] is None, l3
    assert l3["review_flag"] is True, l3

    assert not result["ambiguous"], result["ambiguous"]
    assert result["holder_matches_current_owner"] is True, result
    assert result["gap_note"] is None, result["gap_note"]
    print(f"PASS: chain-of-title walk (Bexar covid 2497, via {outer['method']}) -> "
          f"3 transfers found (2 exempt, 1 fee-owed and correctly flagged for review), "
          f"final holder matches current owner ({result['final_holder_found']})")


def test_walk_chain_douglas_co_3595() -> None:
    """covid 3595 (Douglas County, CO, template V01): 6 parcels (Lots 9-14,
    Block 2, The Fairways at Lone Tree Filing No. 2), all sharing the exact
    same 2 historical sales -- the real case that required widening
    transfer's key to (county_fips, instrument_number, recording_date,
    parcel_apn), since the old 2-column key couldn't hold 6 rows for one
    instrument. Only the 2021 declarant sale to Summit Firestone LLC is
    within scope (the 1992 FDIC sale predates the covenant); it carries an
    ACTUAL (not estimated) disclosed price of $0 straight from Douglas
    County's own assessor sales-history table -- the actual reason this
    covenant was picked as the disclosure-state price-extraction test
    case."""
    with get_session() as session:
        outer = walk_chain_of_title(session, covid=3595)

    assert outer["walked"], outer
    assert outer["method"] == "assessor_sales_data", outer
    assert outer["parcel_count"] == 6, outer

    expected_apns = {"R0334407", "R0334409", "R0334411", "R0334412", "R0334414", "R0334416"}
    assert set(outer["parcels"]) == expected_apns, outer["parcels"].keys()

    for apn, result in outer["parcels"].items():
        assert len(result["chain"]) == 1, (apn, result["chain"])
        link = result["chain"][0]
        assert link["instrument_number"] == "2021070554", (apn, link)
        assert (link["grantor"], link["grantee"]) == ("SUMMIT INVESTMENTS INC", "SUMMIT FIRESTONE LLC"), (apn, link)
        assert link["exemption_category"] == "declarant_sale", (apn, link)
        assert link["consideration_amount"] == 0.0, (apn, link)
        assert result["holder_matches_current_owner"] is True, (apn, result)
        assert result["gap_note"] is None, (apn, result)

    print(f"PASS: chain-of-title walk (Douglas Co CO covid 3595, via {outer['method']}) -> "
          f"6 parcels, each with 1 exempt transfer and an actual (not estimated) disclosed price")


if __name__ == "__main__":
    test_classify_pre_effective_date_fixed_date()
    test_classify_pre_effective_date_recording_date_basis()
    test_classify_pre_effective_date_no_rule()
    test_affidavit_gate_note()
    test_walk_chain_bexar_2497()
    test_walk_chain_douglas_co_3595()
    print("\nall chain-of-title smoke tests passed")
