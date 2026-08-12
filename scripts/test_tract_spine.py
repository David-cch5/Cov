"""Tests for app/title/tract_spine.py -- the tract-forward land spine.

Every case here is a rule the stakeholder stated, not a hypothetical:

  two children per split   a deed creates the piece conveyed AND the piece retained;
                           nobody records a document for the remainder, so a
                           document-driven walk would never make it
  APN at the leaf          Texas keeps no retired APNs, so a node is keyed on its own
                           minted id and acquires an APN only when it becomes a real
                           parcel
  acreage is a ledger      one deed can convey an encumbered tract AND an
                           unencumbered one, so its stated acreage is a fact about
                           the deed -- not about the covenant's land, and not safe to
                           subtract from the parent

Runs inside a transaction that is rolled back, so it exercises the real schema
(constraints included) without leaving rows behind.

Usage: python3 scripts/test_tract_spine.py
"""
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from app.db.session import get_session
from app.title import tract_spine as spine


def _a_real_tract(session):
    """A covenant tract that exists, plus one of its own parcels -- the spine's FKs
    require both to be real, which is the point: a node cannot claim an APN this
    project has never read."""
    row = session.execute(text("""
        SELECT t.covid, t.tract_no, p.county_fips, p.apn
          FROM tract t
          JOIN parcel_covenant pc ON pc.covid = t.covid AND pc.tract_no = t.tract_no
          JOIN parcel p ON p.county_fips = pc.county_fips AND p.apn = pc.apn
         WHERE NOT EXISTS (SELECT 1 FROM tract_node n
                            WHERE n.covid = t.covid AND n.tract_no = t.tract_no)
         LIMIT 1
    """)).fetchone()
    assert row is not None, "no covenant tract with parcels to test against"
    return row


def test_root_is_idempotent_and_labelled() -> None:
    with get_session() as session:
        t = _a_real_tract(session)
        root = spine.create_root(session, t.covid, t.tract_no, stated_acreage=100.0,
                                 encumbered_acreage=100.0)
        again = spine.create_root(session, t.covid, t.tract_no)
        assert root == again, "a covenant tract has exactly one root"
        label = session.execute(text("SELECT node_label FROM tract_node WHERE node_id = :n"),
                                {"n": root}).scalar()
        assert label == f"{t.covid}-T{t.tract_no}", label
        assert spine.acreages(session, root) == {"stated": 100.0, "encumbered": 100.0}
        session.rollback()
    print("PASS: one root per covenant tract, readable label, acreage ledger seeded")


def test_a_split_always_creates_the_retained_piece_too() -> None:
    """The remainder has no instrument of its own, so it can only exist because the
    split created it deliberately."""
    with get_session() as session:
        t = _a_real_tract(session)
        root = spine.create_root(session, t.covid, t.tract_no, encumbered_acreage=100.0)
        made = spine.record_split(session, root, county_fips=t.county_fips,
                                  instrument_number="2011-0001", recording_date="2011-03-04",
                                  conveyed_stated_acreage=40.0,
                                  conveyed_encumbered_acreage=40.0)
        assert set(made) == {"conveyed", "retained"}, made
        kids = spine.walk_down(session, root)
        assert len(kids) == 3, kids  # the root plus both children
        dispositions = sorted(k["disposition"] for k in kids)
        assert dispositions == ["conveyed", "retained", "root"], dispositions
        # The remainder is derived, because this conveyance stayed inside the tract.
        assert spine.acreages(session, made["retained"]) == {"derived": 60.0}
        # Both children cite the deed that made them.
        for k in kids:
            if k["disposition"] != "root":
                assert k["split_instrument_number"] == "2011-0001", k
        session.rollback()
    print("PASS: one deed -> conveyed + retained, remainder derived at 60.0 of 100.0")


def test_a_deed_conveying_land_outside_the_tract_derives_nothing() -> None:
    """The stakeholder's case, and the reason acreage is a ledger: a seller conveys
    two tracts in one deed, one encumbered and one not. The deed says 70 acres; only
    30 of them are this covenant's land. Subtracting 70 from a 100-acre parent would
    report a 30-acre remainder when 70 acres are actually left."""
    with get_session() as session:
        t = _a_real_tract(session)
        root = spine.create_root(session, t.covid, t.tract_no, encumbered_acreage=100.0)
        made = spine.record_split(session, root, county_fips=t.county_fips,
                                  instrument_number="2015-0002",
                                  conveyed_stated_acreage=70.0,
                                  conveyed_encumbered_acreage=30.0)
        conveyed = spine.acreages(session, made["conveyed"])
        assert conveyed == {"stated": 70.0, "encumbered": 30.0}, conveyed
        # The remainder follows the ENCUMBERED figure, never the deed's total.
        assert spine.acreages(session, made["retained"]) == {"derived": 70.0}, \
            "the remainder must be parent minus the encumbered part, not minus the deed"

        # And the disagreement between bases is reported, not smoothed.
        report = spine.reconcile(session, t.covid, t.tract_no)
        conflicts = [c for c in report["basis_conflict"] if c["node_id"] == made["conveyed"]]
        assert conflicts and conflicts[0]["difference_acres"] == 40.0, report
        assert not report["over_conveyed"], report["over_conveyed"]
        session.rollback()
    print("PASS: a two-tract deed states 70 ac, encumbers 30 ac -> remainder 70 ac, "
          "and the 40 ac basis gap is reported")


def test_over_conveyance_is_reported_with_its_likely_cause() -> None:
    """When only the deed's own figure is known and it exceeds the parent, nothing is
    derived and the finding names the usual cause rather than inventing a remainder."""
    with get_session() as session:
        t = _a_real_tract(session)
        root = spine.create_root(session, t.covid, t.tract_no, encumbered_acreage=50.0)
        made = spine.record_split(session, root, county_fips=t.county_fips,
                                  instrument_number="2016-0003",
                                  conveyed_stated_acreage=80.0)
        assert spine.acreages(session, made["retained"]) == {}, \
            "a negative remainder must not be manufactured"
        report = spine.reconcile(session, t.covid, t.tract_no)
        assert report["over_conveyed"], report
        finding = report["over_conveyed"][0]
        assert finding["excess_acres"] == 30.0, finding
        assert "outside it" in finding["likely_cause"], finding
        assert report["reconciles"] is False
        session.rollback()
    print(f"PASS: 80 ac conveyed out of a 50 ac tract -> reported as 30 ac excess, "
          f"no remainder invented")


def test_apn_arrives_at_the_leaf_and_the_round_trip_works() -> None:
    """Covenant -> tract -> child -> child's child -> lot, and back again."""
    with get_session() as session:
        t = _a_real_tract(session)
        root = spine.create_root(session, t.covid, t.tract_no, encumbered_acreage=100.0)
        first = spine.record_split(session, root, county_fips=t.county_fips,
                                   instrument_number="2011-0001",
                                   conveyed_encumbered_acreage=40.0)
        second = spine.record_split(session, first["conveyed"], county_fips=t.county_fips,
                                    instrument_number="2019-0009",
                                    conveyed_encumbered_acreage=10.0)
        leaf = second["conveyed"]
        spine.attach_parcel(session, leaf, t.county_fips, t.apn, gis_acreage=10.0)

        # Down: three generations below the root.
        down = spine.walk_down(session, root)
        assert max(d["depth"] for d in down) == 2, [(d["node_label"], d["depth"]) for d in down]
        # Up: from the lot back to the covenant's own tract, naming both deeds.
        up = spine.walk_up(session, leaf)
        assert up[-1]["disposition"] == "root", up
        assert [u["split_instrument_number"] for u in up[:2]] == ["2019-0009", "2011-0001"], up
        assert up[-1]["covid"] == t.covid

        # And the parcel finds its way back to the covenant.
        found = spine.node_for_parcel(session, t.county_fips, t.apn)
        assert any(f["node_id"] == leaf and f["covid"] == t.covid for f in found), found
        labels = [d["node_label"] for d in down]
        assert f"{t.covid}-T{t.tract_no}.1C.1C" in labels, labels
        session.rollback()
    print(f"PASS: covid {t.covid} tract -> 2 splits -> lot {t.apn}, and the walk back "
          f"names both deeds")


def test_the_schema_refuses_a_node_with_no_provenance() -> None:
    """A non-root node exists because an instrument split its parent. The database
    enforces that, so no code path can leave a floating node."""
    with get_session() as session:
        t = _a_real_tract(session)
        root = spine.create_root(session, t.covid, t.tract_no)
        try:
            session.execute(text("""
                INSERT INTO tract_node (node_label, covid, tract_no, parent_node_id,
                                        disposition)
                VALUES ('bogus', :covid, :tract_no, :root, 'conveyed')
            """), {"covid": t.covid, "tract_no": t.tract_no, "root": root})
            session.flush()
        except Exception as e:
            assert "tract_node_root_shape" in str(e), str(e)[:200]
        else:
            raise AssertionError("a conveyed node with no instrument must be refused")
        session.rollback()

    with get_session() as session:
        t = _a_real_tract(session)
        root = spine.create_root(session, t.covid, t.tract_no)
        try:  # an apn with no county identifies nothing
            session.execute(text("UPDATE tract_node SET apn = 'X' WHERE node_id = :n"),
                            {"n": root})
            session.flush()
        except Exception as e:
            assert "tract_node_apn_needs_county" in str(e), str(e)[:200]
        else:
            raise AssertionError("an apn without its county must be refused")
        session.rollback()
    print("PASS: the schema refuses a node with no splitting instrument, and an APN "
          "with no county")


def test_live_state_is_clean() -> None:
    with get_session() as session:
        counts = session.execute(text(
            "SELECT count(*) AS nodes, count(DISTINCT covid) AS tracts FROM tract_node")).fetchone()
        orphan_labels = session.execute(text("""
            SELECT count(*) FROM tract_node WHERE node_label IS NULL OR node_label = ''
        """)).scalar()
        assert orphan_labels == 0
    print(f"PASS: {counts.nodes} node(s) on record across {counts.tracts} covenant(s), "
          f"every one labelled")


if __name__ == "__main__":
    test_root_is_idempotent_and_labelled()
    test_a_split_always_creates_the_retained_piece_too()
    test_a_deed_conveying_land_outside_the_tract_derives_nothing()
    test_over_conveyance_is_reported_with_its_likely_cause()
    test_apn_arrives_at_the_leaf_and_the_round_trip_works()
    test_the_schema_refuses_a_node_with_no_provenance()
    test_live_state_is_clean()
    print("\nall tract-spine tests passed")
