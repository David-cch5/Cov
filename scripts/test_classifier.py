"""Smoke test for app/gis/classifier.py's classify_metes_and_bounds_tract --
the spatial-first parcel census CLAUDE.md requires for metes-and-bounds-
resolved tracts ("enumerate every parcel whose geometry falls in the covenant
polygon"; "NEVER a bounding-box approximation"). Confirmed real before this
was built: every metes_and_bounds_traverse tract in this project had ZERO
parcel_covenant rows.

The live-classification tests are rolled back, not committed -- unlike
reconcile_tract/reconcile_covenant (idempotent UPDATEs), each call here
INSERTs a new monitor_run/parcel_covenant batch (run_seq = MAX+1), by design
(monitor_run is an audit trail of periodic re-checks, not a cache) -- so
re-running this file live would accumulate a fresh duplicate batch every time.
The already-real, already-committed results of running this live exactly once
against Montgomery's ArcGIS service (covid 3194 tracts 1 & 2, covid 8245
tract 1) are instead checked directly from the DB, which is fast, has no
network dependency, and exercises reconcile.py's own consumption of this
table for free.

Usage: python3 scripts/test_classifier.py
"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import SessionLocal, get_session
from app.config import DB_SCHEMA
from app.gis.classifier import classify_metes_and_bounds_tract, resolve_subdivision_plat_tract


def test_classify_wrong_boundary_method_raises() -> None:
    """covid 3297 tract 1 is boundary_resolution_method='current_parcel_match'
    (a subdivision-plat tract, resolved by resolve_subdivision_plat_tract) --
    calling the metes-and-bounds classifier on it must fail loudly rather than
    silently doing a spatial query against a tract.geom that's already just
    the union of its own matched parcels (nothing independent to classify)."""
    with get_session() as session:
        try:
            classify_metes_and_bounds_tract(session, covid=3297, tract_no=1)
            raise AssertionError("expected RuntimeError, but classify_metes_and_bounds_tract returned normally")
        except RuntimeError as e:
            assert "current_parcel_match" in str(e), e
    print("PASS: classify_metes_and_bounds_tract -> refuses to run against a "
          "current_parcel_match tract")


def test_resolve_subdivision_plat_tract_per_tract_reference() -> None:
    """Confirmed real bug: legal_description_parsed used to hold a single
    subdivision/lot reference for the WHOLE covenant, applied identically
    regardless of tract_no. Covid 4123's own legal description describes two
    genuinely different tracts under different subdivisions (Lots 5-8 of
    "Country Meadows Square" for tract 1; Lot 2 of the same base name -- a
    different platted phase in the county's own records -- for tract 2). With
    a single shared reference, resolving tract 1 silently reused tract 2's
    own lots (or vice versa) whichever happened to be cached, corrupting
    classification. legal_description_parsed is now a LIST, one entry per
    tract_no (1-indexed against document order), and resolving one tract_no
    can never reach into a different tract's own entry.

    This checks the already-committed, already-corrected real classification
    directly from the DB (no live network call needed for the test itself,
    matching this file's own convention) -- tract 1 and tract 2 have
    genuinely different parcel counts/acreages, proving each was resolved
    against its OWN tract-specific reference, not a shared one."""
    with get_session() as session:
        parsed = session.execute(
            text("SELECT legal_description_parsed FROM covenant WHERE covid = 4123")
        ).scalar()
        assert isinstance(parsed, list) and len(parsed) == 2, parsed
        assert parsed[0]["lots"] == ["5", "6", "7", "8"], parsed[0]
        assert parsed[1]["lots"] == ["2"], parsed[1]

        rows = session.execute(
            text("SELECT tract_no, classified_acreage FROM tract WHERE covid = 4123 ORDER BY tract_no")
        ).fetchall()
        acreages = {r.tract_no: float(r.classified_acreage) for r in rows}
        assert abs(acreages[1] - 6.457) < 0.01, acreages  # 4 real parcels (lots 5/6/7/8)
        assert abs(acreages[2] - 1.261) < 0.01, acreages  # 1 real parcel (lot 2)

        # A tract_no beyond how many distinct references were actually parsed
        # must fail loudly, never silently fall back to a different tract's
        # own entry -- this path never reaches the network (fails before any
        # adapter call), so it needs no live GIS dependency to test.
        try:
            resolve_subdivision_plat_tract(session, covid=4123, tract_no=3)
            raise AssertionError("expected RuntimeError for an out-of-range tract_no")
        except RuntimeError as e:
            assert "only 2 distinct tract reference" in str(e), e
    print("PASS: resolve_subdivision_plat_tract -> each tract_no resolves against its OWN "
          "parsed subdivision/lot reference (covid 4123: 6.457 ac / 4 parcels for tract 1, "
          "1.261 ac / 1 parcel for tract 2), and an out-of-range tract_no fails loudly "
          "rather than reusing a different tract's reference")


def test_resolve_subdivision_plat_tract_acreage_handles_null_rows() -> None:
    """Confirmed real bug (task #82): the acreage aggregation inside
    resolve_subdivision_plat_tract used to read
    COALESCE(SUM(acreage), <geometry fallback>) -- but SQL's SUM() silently
    skips NULL rows rather than making the whole aggregate NULL, so it only
    falls back to geometry when EVERY matched parcel has NULL acreage. A
    tract with a MIX of populated and NULL acreage (e.g. Harris County, for
    smaller/commercial parcels) silently undercounted: the NULL rows' real
    area was dropped entirely rather than computed from their own geometry.
    The fix moves the COALESCE inside the SUM so each row's own fallback
    applies individually, regardless of how many other rows are populated.

    Exercises the exact aggregation query directly against two synthetic
    parcel rows (no live GIS call, no covenant/tract dependency needed) --
    one with a real acreage value, one with acreage=NULL and only geometry --
    and confirms the fixed query's total includes both, while the old buggy
    form provably would not have."""
    # parcel.county_fips has a real FK to the county table, so this must be a
    # real county_fips (Montgomery, already used throughout this project's own
    # synthetic tests) -- the fake APNs below are what keep this from ever
    # colliding with real parcel data.
    test_county = "48339"
    apn_a, apn_b = "TEST-ACREAGE-A", "TEST-ACREAGE-B"
    geom_a = "POLYGON((-95.50 30.30, -95.4999 30.30, -95.4999 30.3001, -95.50 30.3001, -95.50 30.30))"
    geom_b = "POLYGON((-95.51 30.30, -95.5099 30.30, -95.5099 30.3001, -95.51 30.3001, -95.51 30.30))"
    with get_session() as session:
        # Scoped to these exact fake APNs, never the whole county -- test_county
        # is a REAL county_fips carrying real Montgomery parcel data.
        session.execute(
            text("DELETE FROM parcel WHERE county_fips = :cf AND apn = ANY(:apns)"),
            {"cf": test_county, "apns": [apn_a, apn_b]},
        )
        session.execute(text("""
            INSERT INTO parcel (county_fips, apn, geom, acreage) VALUES
            (:cf, :apn_a, ST_SetSRID(ST_GeomFromText(:geom_a), 4326), 100.0),
            (:cf, :apn_b, ST_SetSRID(ST_GeomFromText(:geom_b), 4326), NULL)
        """), {"cf": test_county, "apn_a": apn_a, "geom_a": geom_a, "apn_b": apn_b, "geom_b": geom_b})

        expected_b_acres = session.execute(text("""
            SELECT ST_Area(geom::geography) / 4046.8564224 FROM parcel WHERE county_fips = :cf AND apn = :apn
        """), {"cf": test_county, "apn": apn_b}).scalar()
        assert expected_b_acres and expected_b_acres > 0, expected_b_acres

        # The OLD buggy form: SUM() silently skips the NULL row, so the outer
        # COALESCE's fallback never triggers even though one row IS null.
        buggy_total = session.execute(text("""
            SELECT COALESCE(SUM(acreage), ST_Area(ST_Union(geom)::geography) / 4046.8564224)
            FROM parcel WHERE county_fips = :cf AND apn = ANY(:apns)
        """), {"cf": test_county, "apns": [apn_a, apn_b]}).scalar()
        assert abs(float(buggy_total) - 100.0) < 1e-6, buggy_total  # silently drops apn_b entirely

        # The FIXED form, exactly as resolve_subdivision_plat_tract now runs it.
        fixed_total = session.execute(text("""
            SELECT SUM(COALESCE(acreage, ST_Area(geom::geography) / 4046.8564224))
            FROM parcel WHERE county_fips = :cf AND apn = ANY(:apns)
        """), {"cf": test_county, "apns": [apn_a, apn_b]}).scalar()
        assert abs(float(fixed_total) - (100.0 + expected_b_acres)) < 1e-6, (fixed_total, expected_b_acres)

        session.execute(
            text("DELETE FROM parcel WHERE county_fips = :cf AND apn = ANY(:apns)"),
            {"cf": test_county, "apns": [apn_a, apn_b]},
        )
    print("PASS: resolve_subdivision_plat_tract's acreage SQL -> a mix of populated/NULL "
          "acreage parcels sums correctly (the NULL row falls back to its OWN geometry), "
          "confirmed against the old buggy form which silently dropped it entirely")


def test_classify_live_montgomery_3194_tract1() -> None:
    """Live spatial query against Montgomery's ArcGIS service for covid 3194
    tract 1 (934.58-ac metes-and-bounds tract). Rolled back -- see module
    docstring. Bounds-checked rather than exact-matched against parcel counts
    (the county's own live parcel roll can shift slightly between runs, same
    live-data-drift risk documented for chain-of-title's own tests), but
    classified_acreage + residual acreage must always sum to the tract's own
    polygon area exactly, by construction (ST_Difference against the same
    tract.geom) -- that internal consistency is the real regression check."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        result = classify_metes_and_bounds_tract(session, covid=3194, tract_no=1)
        assert result["matched_parcels"] > 0, result
        assert result["candidates_in_bbox"] >= result["matched_parcels"], result
        assert result["interior"] + result["boundary"] == result["matched_parcels"], result

        row = session.execute(text("""
            SELECT classified_acreage, ST_Area(geom::geography) / 4046.8564224 AS tract_acreage,
                   ST_Area(residual_geom::geography) / 4046.8564224 AS residual_acreage,
                   ST_IsValid(residual_geom) AS residual_valid
            FROM tract WHERE covid = 3194 AND tract_no = 1
        """)).fetchone()
        assert row.residual_valid, row
        assert abs(float(row.classified_acreage) + float(row.residual_acreage) - float(row.tract_acreage)) < 0.001, row
    finally:
        session.rollback()
        session.close()
    print("PASS: classify_metes_and_bounds_tract (live, covid 3194 tract 1) -> "
          "classified_acreage + residual always reconstitutes the tract's own polygon area")


def test_persisted_montgomery_3194_real_classification() -> None:
    """The already-committed, real result of running classify_metes_and_
    bounds_tract live against covid 3194's two tracts (see reconcile.py's own
    tests for the reconciliation-level consequence): 327 parcels matched for
    tract 1 (265 interior, 62 boundary), 856.418 ac classified against a
    934.58-ac tract, a 78.159-ac real geometric residual."""
    with get_session() as session:
        row = session.execute(text("""
            SELECT classified_acreage, ST_Area(residual_geom::geography) / 4046.8564224 AS residual_acreage
            FROM tract WHERE covid = 3194 AND tract_no = 1
        """)).fetchone()
        counts = dict(session.execute(text("""
            SELECT classification, count(*) AS n FROM parcel_covenant
            WHERE covid = 3194 AND tract_no = 1 GROUP BY classification
        """)).fetchall())
    assert counts["interior"] == 265, counts
    assert counts["boundary"] == 62, counts
    assert abs(float(row.classified_acreage) - 856.418) < 0.01, row
    assert abs(float(row.residual_acreage) - 78.159) < 0.01, row
    print("PASS: parcel_covenant (covid 3194 tract 1) -> real, committed spatial "
          "classification (265 interior / 62 boundary) matches the live run's own result")


def test_persisted_montgomery_8245_real_classification() -> None:
    """The already-committed result for covid 8245 tract 1 -- corrected
    2026-07-28/29 after a real georeferencing error was found (the tract's
    original geom, likely built from an incomplete _textcache_final copy of
    the deed's Exhibit A missing its opening courses, was shifted enough to
    miss its true parcels and instead spatially catch 8 unrelated ones).
    Re-derived from the deed's complete metes-and-bounds text (found in
    _textcache) and anchored to 4 real corners of the adjoining Oak Ridge
    North Sec. 5 lots the deed itself ties to. The corrected polygon
    dominantly matches exactly 2 real parcels -- APN 451910 (94% overlap,
    the "Alore Center" Reserve A equivalent) and APN 41116 (99% overlap,
    Reserve B) -- classified_acreage lands within 0.01 ac of the deed's own
    stated 4.6055 ac. A few negligible sliver matches (<3% overlap, low
    confidence) from the polygon's own small residual imprecision may also
    appear -- not asserted on by exact count, since that's sensitive to
    live GIS data and floating-point noise at a sub-acre scale; the two
    real, dominant matches are the actual regression check."""
    with get_session() as session:
        row = session.execute(text("""
            SELECT classified_acreage, ST_Area(residual_geom::geography) / 4046.8564224 AS residual_acreage
            FROM tract WHERE covid = 8245 AND tract_no = 1
        """)).fetchone()
        overlaps = dict(session.execute(text("""
            SELECT apn, overlap_fraction FROM parcel_covenant
            WHERE covid = 8245 AND tract_no = 1 AND apn IN ('451910', '41116')
        """)).fetchall())
    assert float(overlaps["451910"]) > 0.9, overlaps
    assert float(overlaps["41116"]) > 0.9, overlaps
    assert abs(float(row.classified_acreage) - 4.6055) < 0.05, row
    print("PASS: parcel_covenant (covid 8245 tract 1) -> corrected classification "
          "dominantly matches the real Alore Center Reserve A/B parcels (>90% overlap each)")


def test_persisted_montgomery_4440_real_classification() -> None:
    """The already-committed result for BOTH of covid 4440's tracts (a raw,
    unplatted-at-recording ~1928-acre assemblage carved from a former
    International Paper timberland parcel, no subdivision name in its own
    legal description): Tract I (1092.15 ac, anchored to 2 real Henderson-
    owned parcel corners the deed itself ties to) and Tract II (835.84 ac,
    registered onto Tract I's own already-anchored shared corners, since
    the two tracts share a POB and a second corner via an exactly-
    reciprocal boundary call -- recovered the identical 0.982 scale factor
    independently both times, a real consistency check, not a coincidence).

    Tract I was the first tract with a large enough bounding box (2491
    candidates) to surface a real, previously-latent bug in classify_
    metes_and_bounds_tract: some candidates have genuinely invalid
    geometry in Montgomery's own live GIS service ("Nested shells" -- a
    self-intersecting-ring topology error), which crashed the whole
    batch's ST_Intersection with a GEOS TopologyException rather than just
    excluding the bad ones. Fixed by adding an ST_IsValid(p.geom) filter to
    the matched-parcels query and surfacing the excluded APNs both in the
    return value and in covenant.review_reason -- never silently dropped.
    Tract II hit 5 more (3 overlapping Tract I's own list, since their
    bounding boxes overlap near the shared boundary).

    Counts here are POST-correction (see test_exclude_non_tract_parcels_
    covid_4440 below), refined across TWO passes. An initial pass excluded
    22 parcels in Tract I and 6 in Tract II by owner/name pattern alone --
    but the stakeholder caught that this heuristic was wrong for several
    parcels held through generically-named land-banking SPVs (Andiron
    Multistate 1 LLC, Millrose Properties Texas LLC, Apogee Peak #1 LLC,
    Horizon Park LLC, Forestar, and a CBA Strategic Fund I LP-held lot
    cluster) that DO trace back to the covenant's own declarant, JM Texas
    Land Fund No. 5 LP, per each parcel's full MCAD deed history -- 13 of
    the 28 were restored on that basis. But checking each restored
    parcel's own overlap_fraction (already on file) then caught a second,
    subtler error: 7 of those 13 -- a "DIRECTOR LOT"/"TRACT DIR LOT"
    cluster, all 0.1148-ac slivers under abstract survey A0494 -- have only
    0.4%-8.7% of their own area actually inside the tract polygon, unlike
    this tract's OWN confirmed MUD-director cluster ("TRACT ME13 DIR LOT
    1-5") at 97.8%-98.2% overlap -- a different MUD's director lots, only
    clipped by calibration imprecision, not genuinely part of this tract.
    Those 7 were re-excluded. Net: only 6 of the original 28 are genuinely
    restored, 22 remain excluded (17 Tract I, 5 Tract II) -- 4069 real
    parcels remain (Tract I: 2429 -- 2301 interior/128 boundary; Tract II:
    1640 -- 1544 interior/96 boundary), still closely matching the
    stakeholder's own recall of an early Fable-driven pass returning "more
    than 4000 parcels" for this same covenant (BUILD_SPEC.md Sec.4). ~60.0%
    of the anchored area is now covered by any matched parcel; the rest is
    honestly unaccounted rather than claimed."""
    # Counts use each tract's own LATEST run_seq only, not a raw row count: this
    # covenant's own classify_metes_and_bounds_tract was re-run more than once per
    # tract (once to backfill recited_legal_description for the plat-tracking work),
    # and each run INSERTs a fresh run_seq batch by design (monitor_run is an audit
    # trail, not a cache -- see this file's own module docstring) -- a raw count
    # across every batch would multiply-count the same real parcel once per re-run.
    with get_session() as session:
        rows = {
            tn: session.execute(text(
                "SELECT classified_acreage FROM tract WHERE covid = 4440 AND tract_no = :tn"
            ), {"tn": tn}).fetchone()
            for tn in (1, 2)
        }
        counts = {
            tn: dict(session.execute(text("""
                SELECT classification, count(*) AS n FROM parcel_covenant
                WHERE covid = 4440 AND tract_no = :tn
                  AND run_seq = (SELECT MAX(run_seq) FROM parcel_covenant WHERE covid = 4440 AND tract_no = :tn)
                GROUP BY classification
            """), {"tn": tn}).fetchall())
            for tn in (1, 2)
        }
        cov = session.execute(text("SELECT review_reason FROM covenant WHERE covid = 4440")).fetchone()

    assert counts[1]["interior"] == 2301 and counts[1]["boundary"] == 128, counts[1]
    assert counts[2]["interior"] == 1544 and counts[2]["boundary"] == 96, counts[2]
    assert abs(float(rows[1].classified_acreage) - 633.599) < 0.5, rows[1]
    assert abs(float(rows[2].classified_acreage) - 482.615) < 0.5, rows[2]
    assert "4069" in cov.review_reason, cov.review_reason
    for apn in ("505224", "321958", "321960", "51921", "334709", "502901"):
        assert apn in cov.review_reason, (apn, cov.review_reason)
    print("PASS: parcel_covenant (covid 4440, both tracts) -> 4069 real parcels matched "
          "total (2429 + 1640) after correction, 6 distinct invalid-geometry parcels "
          "correctly excluded and flagged rather than crashing classification")


def test_exclude_non_tract_parcels_covid_4440() -> None:
    """The final correction to covid 4440's own spatial classification,
    reached in TWO passes. Pass 1: an owner/name-pattern-only exclusion
    first dropped 28 parcels across both tracts as apparent adjoiners --
    but the stakeholder flagged that Andiron Multistate and Millrose
    Properties tracts visibly fall inside the original tract boundary on
    the assessor's own map, and a full MCAD deed-history trace for every
    excluded parcel confirmed it: 13 of the 28 (Andiron Multistate 1 LLC,
    Forestar, Millrose Properties Texas LLC x2, Apogee Peak #1 LLC, Horizon
    Park LLC, and a CBA Strategic Fund I LP-held DIRECTOR LOT/TRACT DIR LOT
    cluster of 7) trace back through intermediate land-banking entities to
    JM Texas Land Fund No. 5 LP and were restored.

    Pass 2 caught an overreach in pass 1: the DIRECTOR LOT/TRACT DIR LOT
    cluster of 7 was restored purely on the strength of its sellers
    (Forestar, CBA Strategic Fund I LP) being confirmed JM Texas Land Fund
    grantees elsewhere -- without checking each parcel's own already-
    computed overlap_fraction, which tells a different story: each is a
    0.1148-ac sliver (abstract survey A0494) with only 0.4%-8.7% of its own
    area actually inside the tract polygon, unlike this tract's OWN
    confirmed MUD-director cluster ("TRACT ME13 DIR LOT 1-5", kept
    throughout at 97.8%-98.2% overlap). A different MUD's director lots,
    only clipped by calibration imprecision -- re-excluded. Net: only 6 of
    the original 28 are genuinely restored; 22 (Henderson/Bowdoin, Carroll,
    two separate Duke tracts, Splendora ISD's two unrelated holdings, the
    entire Dusty Trails cluster of 8, and the non-ME13 DIRECTOR LOT/TRACT
    DIR LOT cluster of 7) remain excluded."""
    with get_session() as session:
        excluded = {r[0] for r in session.execute(text("""
            SELECT apn FROM parcel_covenant WHERE covid = 4440 AND apn = ANY(:apns)
        """), {"apns": [
            "51956", "51958", "289288", "339424", "87489", "87490", "87492", "87493",
            "87494", "87495", "51959", "51961", "51962", "280047", "52070",
            "495596", "495597", "502120", "502121", "502122", "502123", "502124",
        ]}).fetchall()}
        # deed-history-confirmed as genuinely part of the original tract (trace back
        # to JM Texas Land Fund No. 5 LP) AND with a high overlap_fraction (not a
        # boundary-clip sliver) -- must be present, not excluded.
        restored = {r[0] for r in session.execute(text("""
            SELECT apn FROM parcel_covenant WHERE covid = 4440 AND apn = ANY(:apns)
        """), {"apns": [
            "495142", "496068", "802624", "802625", "834881", "498132",
        ]}).fetchall()}
        # confirmed kept throughout: a MUD-district director-lot cluster tied to an
        # already-confirmed MUD number elsewhere in this same tract (MUD ME13, 97.8%-
        # 98.2% overlap), not a blanket exclusion of every unplatted "director lot"-
        # shaped parcel -- and not a blanket restoration of every one either (contrast
        # the non-ME13 DIRECTOR LOT/TRACT DIR LOT cluster above, correctly excluded).
        kept = {r[0] for r in session.execute(text("""
            SELECT apn FROM parcel_covenant WHERE covid = 4440
              AND apn = ANY(ARRAY['530716','530719','530720','530721','551969'])
        """)).fetchall()}
        cov = session.execute(text("SELECT review_reason FROM covenant WHERE covid = 4440")).fetchone()

    assert not excluded, f"these should have been excluded but are still matched: {excluded}"
    assert restored == {
        "495142", "496068", "802624", "802625", "834881", "498132",
    }, restored
    assert kept == {"530716", "530719", "530720", "530721", "551969"}, kept
    assert "NON-TRACT PARCEL EXCLUSION (automated, tract 1)" in cov.review_reason, cov.review_reason
    assert "NON-TRACT PARCEL EXCLUSION (automated, tract 2)" in cov.review_reason, cov.review_reason
    print("PASS: exclude_non_tract_parcels (covid 4440) -> 22 deed-history/overlap-"
          "CONFIRMED adjoiner/unconnected/sliver parcels remain excluded across both "
          "tracts; 6 wrongly-excluded parcels restored after deed-history verification; "
          "a confirmed MUD-tied director-lot cluster correctly kept throughout")


def test_detect_sliver_subdivision_clusters_covid_8534() -> None:
    """Confirmed real (covid 8534 tract 1, Denton County, 2026-08-06): 40
    boundary-classified parcels from two subdivisions the deed never
    describes as part of this tract (Forman Williamsburg Square -- the
    deed's own Exhibit A names it only as adjoining, tying the NW corner
    to its south line -- and Hercules West Addition, never named at all)
    showed a uniformly low overlap_fraction (10.9%-26.4% and 6.2%-48.9%),
    cleanly separated from Sherman Crossing's own genuinely-platted-from-
    this-tract parcels (87.0%-97.3%). classify_metes_and_bounds_tract must
    surface both clusters as possible_non_tract_subdivisions -- a review
    flag, not a silent auto-exclusion (that judgment call stays with
    exclude_non_tract_parcels)."""
    session = SessionLocal()
    try:
        session.execute(text(f"SET search_path TO {DB_SCHEMA}, public"))
        result = classify_metes_and_bounds_tract(session, covid=8534, tract_no=1)
    finally:
        session.rollback()
        session.close()
    groups = {g["subdivision"]: g for g in result["possible_non_tract_subdivisions"]}
    # Keys carry a trailing generic descriptor stripped ("HERCULES WEST ADDITION"
    # -> "HERCULES WEST"), so one real subdivision the CAD spells both ways
    # groups once -- see _GENERIC_DESCRIPTOR_RE.
    assert "FORMAN WILLIAMSBURG SQUARE" in groups, result["possible_non_tract_subdivisions"]
    assert len(groups["FORMAN WILLIAMSBURG SQUARE"]["apns"]) == 15, groups["FORMAN WILLIAMSBURG SQUARE"]
    assert "HERCULES WEST" in groups, result["possible_non_tract_subdivisions"]
    assert len(groups["HERCULES WEST"]["apns"]) == 25, groups["HERCULES WEST"]
    for g in groups.values():
        assert g["max_overlap"] < 0.5, g
    # The deed names Forman Williamsburg Square and not Hercules West, so only
    # the first gets textual corroboration.
    assert groups["FORMAN WILLIAMSBURG SQUARE"]["evidence"] == "deed_names_as_adjoining", groups
    assert groups["HERCULES WEST"]["evidence"] == "geometry_only", groups
    print("PASS: classify_metes_and_bounds_tract (live, covid 8534 tract 1) -> flags both "
          "real sliver-overlap subdivision clusters (Forman Williamsburg Square, deed-"
          "corroborated; Hercules West, geometry-only) as possible_non_tract_subdivisions")


if __name__ == "__main__":
    test_classify_wrong_boundary_method_raises()
    test_resolve_subdivision_plat_tract_per_tract_reference()
    test_resolve_subdivision_plat_tract_acreage_handles_null_rows()
    test_classify_live_montgomery_3194_tract1()
    test_persisted_montgomery_3194_real_classification()
    test_persisted_montgomery_8245_real_classification()
    test_persisted_montgomery_4440_real_classification()
    test_exclude_non_tract_parcels_covid_4440()
    test_detect_sliver_subdivision_clusters_covid_8534()
    print("\nall classifier smoke tests passed")
