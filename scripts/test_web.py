"""Tests for app/web -- the read model and the traversal, without a server.

The traversal is the thing BUILD_SPEC section 4 calls the point ("any lot must be
traceable back to its covenant"), so it is tested directly rather than through
HTTP. The routes are thin enough that a working read model plus a smoke request
per page is the honest coverage.

Usage: python3 scripts/test_web.py
"""
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from app.db.session import get_session
from app.web import queries


def test_read_model_is_read_only() -> None:
    """Navigation must never be the thing that changed a covenant. Checked by
    reading the source rather than by trusting the docstring."""
    import ast
    import inspect

    # Only the SQL actually handed to text() is checked, not prose. A naive
    # substring scan flagged the word "truncated" in a docstring, which is the
    # kind of false alarm that gets a guard deleted rather than fixed.
    tree = ast.parse(inspect.getsource(queries))
    statements = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "text"
                and node.args and isinstance(node.args[0], ast.Constant)):
            statements.append(str(node.args[0].value))
    assert statements, "no text() SQL found -- the check would pass vacuously"
    for sql in statements:
        head = sql.strip().upper()
        for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER", "DROP", "CREATE"):
            assert not head.startswith(verb), f"the read model must not {verb}: {sql[:70]}"
        assert head.startswith(("SELECT", "WITH")), f"not a read: {sql[:70]}"

    from app.web import app as app_module
    routes_src = inspect.getsource(app_module)
    assert "methods=" not in routes_src, "no route should accept a non-GET method"
    print(f"PASS: all {len(statements)} SQL statements in the read model are reads; "
          f"no route accepts a non-GET method")


def test_covenant_list_and_coverage() -> None:
    with get_session() as session:
        covs = queries.covenant_list(session)
        assert len(covs) >= 20, len(covs)
        row = next(c for c in covs if c["covid"] == 4956)
        assert row["parcels"] >= 1 and row["tracts"] == 1, row
        cov = queries.lineage_coverage(session)
        assert cov["parcels_formed"] > 4000, cov
        assert cov["parcels_total"] > cov["parcels_formed"], (
            "some parcels must legitimately have no formation date")
    print(f"PASS: {len(covs)} covenants listed; {cov['parcels_formed']:,} of "
          f"{cov['parcels_total']:,} parcels have a recorded formation date, "
          f"{cov['edges']} lineage edge(s)")


def test_covenant_to_parcel_and_back() -> None:
    """The round trip, which is the requirement: covenant -> tract -> parcel, and
    from that parcel back to every covenant that encumbers it."""
    with get_session() as session:
        cov = queries.covenant(session, 4956)
        assert cov is not None and cov["tracts"], cov
        tract_no = cov["tracts"][0]["tract_no"]

        census = queries.tract_parcels(session, 4956, tract_no)
        assert census, "covid 4956 must have a census"
        apn = census[0]["apn"]

        p = queries.parcel(session, census[0]["county_fips"], apn)
        assert p is not None, apn
        back = [c["covid"] for c in p["covenants"]]
        assert 4956 in back, f"the parcel must lead back to covid 4956, got {back}"
        # And the formation citation survives the round trip.
        assert p["formed_date"] is not None and p["formed_by_instrument"], p
        assert p["formation_source"] == "plat", p["formation_source"]
    print(f"PASS: covid 4956 -> tract {tract_no} -> parcel {apn} -> back to "
          f"covid {back}, formation {p['formed_date']} by {p['formed_by_instrument']}")


def test_excluded_parcels_are_shown_not_hidden() -> None:
    """A human's exclusion decision is the kind of thing navigation exists to
    surface. Hiding excluded parcels would make the census look unanimous."""
    with get_session() as session:
        excluded = session.execute(text("""
            SELECT covid, tract_no, county_fips, apn FROM parcel_covenant_exclusion LIMIT 1
        """)).fetchone()
        if excluded is None:
            print("SKIP: no exclusions recorded")
            return
        census = queries.tract_parcels(session, excluded.covid, excluded.tract_no)
        row = next((r for r in census if r["apn"] == excluded.apn), None)
        assert row is not None, (
            f"excluded parcel {excluded.apn} must still appear in the census view")
        assert row["excluded_reason"], "and must carry its reason"

        p = queries.parcel(session, excluded.county_fips, excluded.apn)
        link = next(c for c in p["covenants"] if c["covid"] == excluded.covid)
        assert link["excluded_reason"], "the parcel page must say it was excluded"
    print(f"PASS: excluded parcel {excluded.apn} is shown with its reason, both ways")


def test_lineage_walk_is_depth_agnostic_and_honest_when_empty() -> None:
    """Returns nothing today because parcel_lineage holds nothing -- and that has
    to read as an absence of evidence, not as a parcel with no history. The walk
    itself takes any depth, so it deepens when edges are recorded without being
    rewritten."""
    with get_session() as session:
        edges = session.execute(text("SELECT count(*) FROM parcel_lineage")).scalar()
        up = queries.lineage_walk(session, "48113", "24123500010140000", "up")
        down = queries.lineage_walk(session, "48113", "24123500010140000", "down")
        if edges == 0:
            assert up == [] and down == [], (up, down)
        try:
            queries.lineage_walk(session, "48113", "x", "sideways")
        except ValueError as e:
            assert "up" in str(e)
        else:
            raise AssertionError("an unknown direction must be refused")
    print(f"PASS: lineage walk runs both directions over {edges} edge(s) and refuses "
          f"an unknown direction")


def test_pages_render() -> None:
    """A smoke request per page, through the real WSGI app."""
    from app.web.app import app

    client = app.test_client()
    with get_session() as session:
        apn_row = session.execute(text("""
            SELECT county_fips, apn FROM parcel WHERE geom IS NOT NULL LIMIT 1
        """)).fetchone()

    for path, expect in [
        ("/", b"Covenants"),
        ("/covenant/4956", b"24123500010140000"),
        (f"/parcel/{apn_row.county_fips}/{apn_row.apn}", b"Formation"),
        ("/search?q=4956", b"4956"),
        ("/covenant/999999", None),
    ]:
        r = client.get(path, follow_redirects=True)
        if expect is None:
            assert r.status_code == 404, f"{path} should 404, got {r.status_code}"
            continue
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        assert expect in r.data, f"{path} missing {expect!r}"
    print("PASS: home, covenant, parcel and search all render; an unknown covid 404s")


if __name__ == "__main__":
    test_read_model_is_read_only()
    test_covenant_list_and_coverage()
    test_covenant_to_parcel_and_back()
    test_excluded_parcels_are_shown_not_hidden()
    test_lineage_walk_is_depth_agnostic_and_honest_when_empty()
    test_pages_render()
    print("\nall web tests passed")
