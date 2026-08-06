"""Smoke test for app/llm/anchor_agent.py's own read-only tools -- deliberately
does NOT exercise the agentic loop itself (that needs a live, expensive LLM
call). What's tested is get_covenant_context's sibling-tract reporting: a
real, previously-missing capability (confirmed on covid 4780) -- a tract's
own deed text can tie its Point of Beginning to the exact same physical
corner a SIBLING tract's own deed text describes as ITS Point of Beginning,
but nothing told the agent an already-anchored sibling even existed, so this
strong, already-verified tie was never checked. get_covenant_context now
reports every other tract of the same covid that already carries a real
anchor, plus that tract's own real-world Point of Beginning.

Also tests query_deed_history -- confirmed real and load-bearing on covid
3346 tract 1 and covid 8534 tract 1, both of which burned many
query_gis_parcels turns with no path to success (their deed's own named
declarant has zero textual resemblance to current ownership once the land
was subdivided and resold) before this tool existed.

Usage: python3 scripts/test_anchor_agent.py
"""
import json
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import get_session
from app.llm.anchor_agent import get_covenant_context, query_deed_history

# A ~1-acre square near Montgomery County, TX -- real, valid geometry; only
# its first ring vertex (the POB, by this project's own traverse-to-geojson
# convention) matters for this test.
_SQUARE_A = {
    "type": "MultiPolygon",
    "coordinates": [[[
        [-95.50, 30.30], [-95.50, 30.302], [-95.498, 30.302], [-95.498, 30.30], [-95.50, 30.30],
    ]]],
}
_SQUARE_B = {
    "type": "MultiPolygon",
    "coordinates": [[[
        [-95.51, 30.31], [-95.51, 30.312], [-95.508, 30.312], [-95.508, 30.31], [-95.51, 30.31],
    ]]],
}


def test_get_covenant_context_reports_anchored_siblings() -> None:
    # get_covenant_context opens its OWN session internally, so the setup
    # rows must be committed (a separate `with` block) before calling it --
    # an uncommitted row in this test's own session is invisible to a
    # different transaction, even against the same database.
    try:
        with get_session() as session:
            session.execute(text("""
                INSERT INTO covenant (covid, county_fips, status, legal_description_raw)
                VALUES (999998, '48339', 'ingested', 'TRACT I: ... TRACT II: ...')
                ON CONFLICT (covid) DO UPDATE SET legal_description_raw = EXCLUDED.legal_description_raw
            """))
            # tract 1 left UNANCHORED (geom NULL) -- the one being "resolved". A
            # placeholder approximate_geom is required (tract's own CHECK
            # constraint: geom or approximate_geom must be present) but is
            # irrelevant to this test.
            session.execute(text("""
                INSERT INTO tract (covid, tract_no, geom, approximate_geom)
                VALUES (999998, 1, NULL, ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326))
                ON CONFLICT (covid, tract_no) DO UPDATE SET geom = NULL, approximate_geom = EXCLUDED.approximate_geom
            """), {"g": json.dumps(_SQUARE_A)})
            # tract 2 already anchored -- the sibling the agent should discover.
            session.execute(text("""
                INSERT INTO tract (covid, tract_no, geom, boundary_resolution_method)
                VALUES (999998, 2, ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326), 'metes_and_bounds_traverse')
                ON CONFLICT (covid, tract_no) DO UPDATE SET
                    geom = EXCLUDED.geom, boundary_resolution_method = EXCLUDED.boundary_resolution_method
            """), {"g": json.dumps(_SQUARE_B)})

        result = json.loads(get_covenant_context.func(999998))
        siblings = result["sibling_tracts_already_anchored"]
        assert len(siblings) == 1, siblings
        assert siblings[0]["tract_no"] == 2, siblings
        assert siblings[0]["boundary_resolution_method"] == "metes_and_bounds_traverse", siblings
        # first ring vertex of _SQUARE_B, by this project's own convention
        assert abs(siblings[0]["pob_lon"] - (-95.51)) < 1e-9, siblings
        assert abs(siblings[0]["pob_lat"] - 30.31) < 1e-9, siblings
    finally:
        with get_session() as session:
            session.execute(text("DELETE FROM tract WHERE covid = 999998"))
            session.execute(text("DELETE FROM covenant WHERE covid = 999998"))
    print("PASS: get_covenant_context -> reports an already-anchored sibling tract's own "
          "real Point of Beginning, not just this tract's own (previously unanchored) state")


def test_query_deed_history_live_denton_wolski() -> None:
    """covid 8534's own declarant, real transfers found on Denton's own
    recorder portal (denton.tx.publicsearch.us) -- confirmed live this
    session, including the 2021 lien release naming him directly."""
    result = json.loads(query_deed_history.func(county_fips="48121", name="Wolski Edward F"))
    assert "rows" in result and result["rows"], result
    grantors = {r["GRANTOR"] for r in result["rows"]}
    assert any("WOLSKI" in g for g in grantors), result
    print(f"PASS: query_deed_history (live, Denton, 'Wolski Edward F') -> {len(result['rows'])} rows, "
          f"{result['total_matched']} total matched")


def test_query_deed_history_unknown_county() -> None:
    result = json.loads(query_deed_history.func(county_fips="99999", name="Smith"))
    assert "error" in result and "no recorder-portal registry entry" in result["error"], result
    print("PASS: query_deed_history -> unknown county_fips correctly returns an error, not a guess")


def test_query_deed_history_inactive_county() -> None:
    """County FIPS 48479 (Kofile/CountyFusion, status='needs_review') --
    correctly refused rather than attempting an unverified adapter."""
    result = json.loads(query_deed_history.func(county_fips="48479", name="Smith"))
    assert "error" in result and "not active" in result["error"], result
    print("PASS: query_deed_history -> a registered-but-not-yet-verified county is correctly refused")


def test_query_deed_history_unwired_adapter() -> None:
    """County FIPS 48139 (Ellis, active status) uses the Acclaim adapter, not
    publicsearch -- this tool is only wired to publicsearch so far, and must
    say so rather than silently trying the wrong adapter."""
    result = json.loads(query_deed_history.func(county_fips="48139", name="Smith"))
    assert "error" in result and "different recorder-portal adapter" in result["error"], result
    print("PASS: query_deed_history -> a county on a different (non-publicsearch) adapter is correctly refused")


if __name__ == "__main__":
    test_get_covenant_context_reports_anchored_siblings()
    test_query_deed_history_live_denton_wolski()
    test_query_deed_history_unknown_county()
    test_query_deed_history_inactive_county()
    test_query_deed_history_unwired_adapter()
    print("\nall anchor_agent smoke tests passed")
