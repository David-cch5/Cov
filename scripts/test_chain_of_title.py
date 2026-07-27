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
from app.title.chain import (
    _address_seed,
    _affidavit_gate_note,
    _anchor_lot_is_unreliable,
    _classify_pre_effective_date,
    _normalize_doc_type,
    _row_lots,
    _subdivisions_match,
    _walk_hop1_candidates,
    walk_chain_of_title,
)


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


def test_address_seed_directional_prefix() -> None:
    """Confirmed real on covid 3297/Montgomery: "16790 N THRASHER DR" -- a
    plain first-two-tokens seed gives "16790 N", a near-useless second
    token, and the portal's own quick search (apparently exact-phrase) came
    back with 0 rows for "16790 THRASHER" (dropping the "N" entirely) but 3
    rows -- including the real target deed -- for "16790 N THRASHER"."""
    assert _address_seed("16790 N THRASHER DR") == "16790 N THRASHER"
    assert _address_seed("456 SW OAK LN") == "456 SW OAK"
    assert _address_seed("9910 CRESCENT COVE DR") == "9910 CRESCENT"  # unaffected: no directional
    assert _address_seed("123 MAIN ST") == "123 MAIN"
    print("PASS: _address_seed -> pulls in a 3rd token only when the 2nd is a directional prefix")


def test_row_lots_high_low_lot_range() -> None:
    """Montgomery's own quirk: HIGH LOT/LOW LOT columns instead of a single
    "LOT" column (confirmed real on covid 4780) -- before this, a Montgomery
    row's row.get("LOT") was always None, making lot-based anchor filtering
    a silent no-op for this county."""
    assert _row_lots({"LOT": "5, 6"}) == {"5", "6"}
    assert _row_lots({"HIGH LOT": "8", "LOW LOT": "5"}) == {"5", "6", "7", "8"}
    assert _row_lots({"HIGH LOT": "263", "LOW LOT": "263"}) == {"263"}
    assert _row_lots({"HIGH LOT": "N/A", "LOW LOT": "N/A"}) == set()
    assert _row_lots({"BLOCK": "1"}) == set()  # neither LOT nor HIGH/LOW LOT present
    print("PASS: _row_lots -> handles both the plain LOT column and Montgomery's HIGH/LOW LOT range")


def test_normalize_doc_type_vendors_lien() -> None:
    """Confirmed real on covid 3297/Montgomery: "WARRANTY DEED W/VENDORS
    LIEN" -- an extremely common Texas owner/builder-financing deed
    variant -- didn't match CONVEYANCE_DOC_TYPES' exact strings at all, so
    it was silently treated as a non-conveyance."""
    assert _normalize_doc_type("WARRANTY DEED W/VENDORS LIEN") == "WARRANTY DEED"
    assert _normalize_doc_type("SPECIAL WARRANTY DEED W/VENDOR'S LIEN") == "SPECIAL WARRANTY DEED"
    assert _normalize_doc_type("GENERAL WARRANTY DEED WITH VENDORS LIEN") == "GENERAL WARRANTY DEED"
    assert _normalize_doc_type("WARRANTY DEED") == "WARRANTY DEED"  # unaffected
    assert _normalize_doc_type("DEED OF TRUST") == "DEED OF TRUST"  # unaffected -- not a vendor's-lien suffix
    print("PASS: _normalize_doc_type -> strips a vendor's-lien financing suffix, not a distinct instrument type")


def test_anchor_lot_is_unreliable_and_subdivisions_match() -> None:
    """Confirmed real on covid 3297/Montgomery: the anchor (the covenant's
    own DECLARATION) is indexed with HIGH LOT/LOW LOT both "263", but its
    own COMMENT reads "L263 GLENEAGLES S4A A583 ET AL" -- the recorder's
    index only captured the FIRST lot mentioned in a subdivision-wide
    document, "ET AL" signaling there's (many) more. Using that single lot
    as a strict filter silently rejected every other real lot (e.g. 281)
    the same declaration actually covers."""
    assert _anchor_lot_is_unreliable({"COMMENT": "L263 GLENEAGLES S4A A583 ET AL"}) is True
    assert _anchor_lot_is_unreliable({"COMMENT": "L263 GLENEAGLES S4A A583"}) is False
    assert _anchor_lot_is_unreliable({"COMMENT": None}) is False

    assert _subdivisions_match("GLENEAGLES", "GLENEAGLES") is True
    assert _subdivisions_match("GLENEAGLES", "THE RESERVE ON LAKE CONROE") is False
    assert _subdivisions_match(None, "GLENEAGLES") is True  # can't compare -- don't reject
    assert _subdivisions_match("N/A", "GLENEAGLES") is True
    print("PASS: _anchor_lot_is_unreliable / _subdivisions_match -> "
          "ET AL detected, subdivision used as the fallback correlator")


def test_walk_hop1_candidates_relaxed_fallback() -> None:
    """Confirmed real on covid 3297/Montgomery (Gleneagles): the declarant
    almost never conveys directly to an individual lot buyer, instead bulk-
    selling through an intermediate developer/builder not surfaced by
    either seed search -- so hop 1's strict grantor-must-match-declarant
    check finds nothing, and the (declarant-link-unconfirmed) fallback
    picks the earliest real conveyance for this lot/block instead of
    silently returning an empty chain. Subsequent hops are NOT relaxed --
    only is_first_hop=True ever falls back."""
    covenant = SimpleNamespace(recording_date=date(2009, 8, 20))
    pool = {
        "2012093064": {"DOC NUMBER": "2012093064", "DOC TYPE": "WARRANTY DEED W/VENDORS LIEN",
                        "RECORDED DATE": "9/25/2012", "GRANTOR": "LONG LAKE LTD",
                        "GRANTEE": "SOUTHERLAND MARK ANTHONY"},
    }

    # hop 1: declarant doesn't match any real grantor in the pool -> relaxed fallback fires
    candidates, unconfirmed = _walk_hop1_candidates(
        pool, consumed=set(), current_holder="HFG-CENTERRA DEVELOPMENT, LP", covenant=covenant, is_first_hop=True,
    )
    assert unconfirmed is True, unconfirmed
    assert len(candidates) == 1 and candidates[0][1]["DOC NUMBER"] == "2012093064", candidates

    # a later hop (is_first_hop=False) must NOT relax -- an unmatched grantor there is a real gap, not a fallback
    candidates, unconfirmed = _walk_hop1_candidates(
        pool, consumed=set(), current_holder="SOMEONE ELSE ENTIRELY", covenant=covenant, is_first_hop=False,
    )
    assert candidates == [] and unconfirmed is False, (candidates, unconfirmed)

    # a real grantor match never needs the fallback in the first place
    candidates, unconfirmed = _walk_hop1_candidates(
        pool, consumed=set(), current_holder="LONG LAKE LTD", covenant=covenant, is_first_hop=True,
    )
    assert unconfirmed is False and len(candidates) == 1, (candidates, unconfirmed)
    print("PASS: _walk_hop1_candidates -> relaxed fallback only on hop 1, and only when nothing "
          "actually matches the declarant")


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


def test_walk_chain_montgomery_3297() -> None:
    """covid 3297 (Montgomery, TX, template V01, HFG-Centerra Development /
    Gleneagles subdivision), 5 of its 43 matched parcels (max_parcels cap).
    The recorder-portal name-walk fallback -- this is the test case that
    drove most of this module's real bug fixes: Montgomery's HIGH LOT/LOW
    LOT columns, a vendor's-lien deed-type variant, an anchor document
    whose own indexed lot ("263... ET AL") undersold its true subdivision-
    wide scope, and a declarant who bulk-sold through an intermediate
    builder (Long Lake Ltd) never surfaced by either seed search -- see
    this module's own docstrings for each fix's specifics.

    Built from the actual persisted results of a live run (not fully
    deterministic re-run to re-run: this is many independent live searches
    against a real portal, not a single deterministic bulk query like the
    CAD/assessor paths above) -- if a live re-run finds a different but
    still-real chain (e.g. an extra hop the portal's result ordering
    happened to surface differently), that's the fallback path working as
    designed, not necessarily a regression; recheck against the DB before
    assuming a real break."""
    with get_session() as session:
        outer = walk_chain_of_title(session, covid=3297, tract_no=1, max_parcels=5)

    assert outer["walked"], outer
    assert outer["method"] == "recorder_portal_name_walk", outer
    assert outer["parcel_count"] == 5, outer

    expected_apns = {"93070", "93088", "93089", "93090", "93091"}
    assert set(outer["parcels"]) == expected_apns, outer["parcels"].keys()

    # 93070: single hop, correctly exempt under V01's own fixed pre_effective_date cutoff
    # (2013-01-01) -- despite the declarant-link being unconfirmed, this must NOT be forced
    # fee-owed, since pre_effective_date is a pure recording-date fact, unaffected by who
    # the grantor is (the actual bug this test guards against).
    r93070 = outer["parcels"]["93070"]
    assert len(r93070["chain"]) == 1, r93070["chain"]
    link = r93070["chain"][0]
    assert link["instrument_number"] == "2012093064", link
    assert (link["grantor"], link["grantee"]) == ("LONG LAKE LTD", "SOUTHERLAND MARK ANTHONY"), link
    assert link["exemption_category"] == "pre_effective_date", link
    assert link["review_flag"] is False, link  # confirmed exemption stands despite the unconfirmed declarant link
    assert r93070["holder_matches_current_owner"] is True, r93070

    # every parcel's walk must end on a holder matching its current owner of record, and
    # every real link found must be review-flagged unless independently confirmed exempt
    # (pre_effective_date, checked above) -- never silently assumed either way.
    for apn, result in outer["parcels"].items():
        assert result["gap_note"] is None, (apn, result)
        assert result["holder_matches_current_owner"] is True, (apn, result)
        for link in result["chain"]:
            assert link["review_flag"] or link["exemption_category"] is not None, (apn, link)

    print(f"PASS: chain-of-title walk (Montgomery covid 3297, via {outer['method']}) -> "
          f"5 parcels, each resolving to a final holder matching current owner of record")


if __name__ == "__main__":
    test_classify_pre_effective_date_fixed_date()
    test_classify_pre_effective_date_recording_date_basis()
    test_classify_pre_effective_date_no_rule()
    test_affidavit_gate_note()
    test_address_seed_directional_prefix()
    test_row_lots_high_low_lot_range()
    test_normalize_doc_type_vendors_lien()
    test_anchor_lot_is_unreliable_and_subdivisions_match()
    test_walk_hop1_candidates_relaxed_fallback()
    test_walk_chain_bexar_2497()
    test_walk_chain_douglas_co_3595()
    test_walk_chain_montgomery_3297()
    print("\nall chain-of-title smoke tests passed")
