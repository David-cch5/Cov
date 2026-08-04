"""Resolve a covenant's tract boundary and classify parcels against it.

For subdivision_plat descriptions this is a union of already-existing county parcel
geometry, not a from-scratch computation -- the county already surveyed each lot when
its plat was recorded. This also gives the boundary a durable, name-independent anchor:
once tract.geom is set, later monitor runs re-query by spatial overlap against whatever
the county's current parcel layer shows, regardless of any later replat/rename -- see
the discussion this was designed around.
"""
import json
import re

from sqlalchemy import text

from app.db.repository import insert_source, upsert_parcel
from app.gis.adapters import (
    bexar_tx, collin_tx, dallas_tx, denton_tx, douglas_co, harris_tx, hunt_tx, kerr_tx, llano_tx,
    montgomery_tx, nueces_tx, tarrant_tx, travis_tx,
)
from app.parsing.legal_description.subdivision_plat import parse_subdivision_reference
from app.queue.job_queue import run_with_job_queue

COUNTY_ADAPTERS = {
    "48339": montgomery_tx,
    "48439": tarrant_tx,
    "48113": dallas_tx,
    "48299": llano_tx,
    "48029": bexar_tx,
    "48201": harris_tx,
    "48453": travis_tx,
    "48085": collin_tx,
    "48355": nueces_tx,
    "48121": denton_tx,
    "48231": hunt_tx,
    "48265": kerr_tx,
    "08035": douglas_co,
}

# Adapters without a structured Lot/Block field can only text-match the legal
# description, so a "match" there is inherently less certain than Montgomery's
# exact attribute filter -- cap confidence accordingly rather than reporting
# false certainty on a fuzzy match. Llano/Bexar/Nueces/Hunt/Kerr have nominal
# tract_or_lot/Block-style columns, but they're inconsistently populated (see
# each adapter's docstring), so they belong in this set too despite the schema
# looking structured. Harris/Collin/Denton/Travis have genuinely reliable
# structured lot/tract columns and are NOT in this set. Douglas Co (CO) only
# has a structured Block field (no Lot field at all -- see douglas_co.py), so
# it belongs here too.
_TEXT_MATCH_ONLY_COUNTIES = {"48439", "48113", "48299", "48029", "48355", "48453", "48231", "48265", "08035"}


def resolve_subdivision_plat_tract(session, covid: int, tract_no: int = 1) -> dict:
    row = session.execute(
        text("SELECT county_fips, legal_description_raw, legal_description_parsed FROM covenant WHERE covid = :covid"),
        {"covid": covid},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} not found")

    county_fips = row.county_fips
    parsed = row.legal_description_parsed
    if parsed is None:
        # Confirmed real, previously-undetected gap: parse_subdivision_reference()
        # existed and was correct in isolation but had no caller anywhere in the
        # pipeline, so legal_description_parsed was never populated by anything --
        # this raised "has no legal_description_parsed to resolve from" on
        # essentially every subdivision_plat covenant rather than actually
        # resolving one. Parse and persist it here, on first need, instead of
        # requiring a separate ingestion-time step.
        if not row.legal_description_raw:
            raise RuntimeError(f"covid {covid} has no legal_description_raw to parse a subdivision reference from")
        parsed = parse_subdivision_reference(row.legal_description_raw)
        session.execute(
            text("UPDATE covenant SET legal_description_parsed = (:parsed)::jsonb, updated_at = now() "
                 "WHERE covid = :covid"),
            {"covid": covid, "parsed": json.dumps(parsed)},
        )
    adapter = COUNTY_ADAPTERS.get(county_fips)
    if adapter is None:
        raise RuntimeError(f"no GIS adapter registered for county_fips={county_fips}")

    # a Texas-abstract deed names a unique abstract code (e.g. "A0166") --
    # prefer matching on that directly, when the adapter supports it, over a
    # name-text search: the code is unambiguous where a survey grantee's name
    # (used as the "subdivision name" for this legal_description_type) is not.
    use_abstract_code = bool(parsed.get("abstract_code") and hasattr(adapter, "query_by_abstract_code"))
    if use_abstract_code:
        def _call():
            return adapter.query_by_abstract_code(parsed["abstract_code"], parsed["lots"])
    else:
        def _call():
            return adapter.query_by_subdivision_and_lots(parsed["subdivision_name"], parsed["lots"], block=parsed.get("block"))

    # The live network call (an ArcGIS REST query) gets the same
    # retry-with-backoff + durable job_queue failure logging as the
    # recorder-portal adapters -- it's the exact same class of problem (a
    # third-party endpoint that can be transiently slow/unreachable or
    # genuinely down/changed), just over a stable REST API instead of a
    # rendered page. A covenant with no parcels matched is NOT retried here
    # -- that's a real "nothing found" result, not a network failure, and
    # retrying it would just ask the same well-formed query five more times.
    parcels = run_with_job_queue(
        _call, job_type="gis_classifier_query", county_fips=county_fips, covid=covid,
        payload={
            "adapter": adapter.__name__, "base_url": adapter.BASE_URL,
            "method": "query_by_abstract_code" if use_abstract_code else "query_by_subdivision_and_lots",
            "abstract_code": parsed.get("abstract_code"), "subdivision_name": parsed.get("subdivision_name"),
            "lots": parsed.get("lots"), "block": parsed.get("block"),
        },
    )
    if not parcels:
        raise RuntimeError(f"no parcels matched subdivision={parsed['subdivision_name']!r} lots={parsed['lots']}")

    requested_lots = set(parsed["lots"])
    matched_lots = {p["lot"] for p in parcels if p["lot"]}
    missing_lots = sorted(requested_lots - matched_lots, key=lambda x: (len(x), x))

    match_rate = (len(matched_lots) / len(requested_lots)) if requested_lots else None
    text_match_only = county_fips in _TEXT_MATCH_ONLY_COUNTIES
    confidence = (match_rate * 0.7) if (match_rate is not None and text_match_only) else match_rate

    gis_source_id = insert_source(
        session, source_type="gis_api", reference=adapter.BASE_URL, confidence=confidence,
    )

    apns = []
    for p in parcels:
        upsert_parcel(
            session, county_fips=p["county_fips"], apn=p["apn"], owner_name_raw=p["owner_name_raw"],
            situs_address=p["situs_address"], city=p.get("city"), zip_code=p.get("zip_code"),
            acreage=p["acreage"], geojson=p["geojson"], source_id=gis_source_id,
            recited_legal_description=p.get("recited_legal_description"),
        )
        apns.append(p["apn"])

    # SUM(acreage) is the county's own attribute where populated; some
    # counties (e.g. Harris, for smaller/commercial parcels) leave it NULL
    # even though the parcel has real geometry, so fall back to computing
    # acreage from the unioned geometry itself rather than leaving a real,
    # geometrically-resolved tract with no acreage to reconcile against.
    session.execute(
        text("""
            INSERT INTO tract (covid, tract_no, geom, classified_acreage, boundary_resolution_method, source_id, updated_at)
            SELECT :covid, :tract_no, ST_Multi(ST_Union(geom)),
                   COALESCE(SUM(acreage), ST_Area(ST_Union(geom)::geography) / 4046.8564224),
                   'current_parcel_match', :source_id, now()
            FROM parcel WHERE county_fips = :county_fips AND apn = ANY(:apns)
            ON CONFLICT (covid, tract_no) DO UPDATE SET
                geom = EXCLUDED.geom, classified_acreage = EXCLUDED.classified_acreage,
                boundary_resolution_method = EXCLUDED.boundary_resolution_method,
                source_id = EXCLUDED.source_id, updated_at = now()
        """),
        {"covid": covid, "tract_no": tract_no, "county_fips": county_fips, "apns": apns, "source_id": gis_source_id},
    )

    run_seq = session.execute(
        text("SELECT COALESCE(MAX(run_seq), 0) + 1 AS n FROM monitor_run WHERE covid = :covid"),
        {"covid": covid},
    ).fetchone().n

    session.execute(
        text("""
            INSERT INTO monitor_run (covid, run_seq, run_type, new_parcels_found, status)
            VALUES (:covid, :run_seq, 'initial', :n, 'ok')
        """),
        {"covid": covid, "run_seq": run_seq, "n": len(parcels)},
    )

    match_method = "text match against legal description" if text_match_only else "exact county GIS attribute filter"
    for p in parcels:
        parcel_confidence = (1.0 if p["lot"] in requested_lots else 0.5) * (0.7 if text_match_only else 1.0)
        session.execute(
            text("""
                INSERT INTO parcel_covenant (county_fips, apn, covid, tract_no, run_seq, classification, confidence, rationale)
                VALUES (:county_fips, :apn, :covid, :tract_no, :run_seq, 'interior', :confidence, :rationale)
                ON CONFLICT (county_fips, apn, covid, tract_no, run_seq) DO NOTHING
            """),
            {
                "county_fips": p["county_fips"], "apn": p["apn"], "covid": covid, "tract_no": tract_no,
                "run_seq": run_seq, "confidence": parcel_confidence,
                "rationale": f"lot {p['lot']} matched subdivision '{parsed['subdivision_name']}' via {match_method}",
            },
        )

    return {
        "matched_parcels": len(parcels),
        "requested_lots": len(requested_lots),
        "missing_lots": missing_lots,
        "run_seq": run_seq,
    }


def classify_metes_and_bounds_tract(session, covid: int, tract_no: int = 1) -> dict:
    """Spatial-first parcel census for a tract whose boundary came from a metes-and-bounds
    traverse (parsed courses/distances -> closed polygon), not from unioning already-existing
    plat lots. Unlike resolve_subdivision_plat_tract, tract.geom here is an independent
    geometric fact -- so this queries live county parcels, does a TRUE polygon intersection
    test against it in PostGIS, and computes a real residual (the part of the tract no
    matched parcel covers) rather than assuming full coverage by construction.
    """
    row = session.execute(
        text("""
            SELECT c.county_fips, t.boundary_resolution_method,
                   ST_XMin(t.geom) AS xmin, ST_YMin(t.geom) AS ymin,
                   ST_XMax(t.geom) AS xmax, ST_YMax(t.geom) AS ymax
            FROM tract t JOIN covenant c ON c.covid = t.covid
            WHERE t.covid = :covid AND t.tract_no = :tract_no
        """),
        {"covid": covid, "tract_no": tract_no},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} tract {tract_no} not found")
    if row.boundary_resolution_method != "metes_and_bounds_traverse":
        raise RuntimeError(
            f"covid {covid} tract {tract_no} is {row.boundary_resolution_method!r}, not a metes-and-bounds "
            f"traverse -- use resolve_subdivision_plat_tract instead"
        )

    county_fips = row.county_fips
    adapter = COUNTY_ADAPTERS.get(county_fips)
    if adapter is None:
        raise RuntimeError(f"no GIS adapter registered for county_fips={county_fips}")
    if not hasattr(adapter, "iter_parcels"):
        raise RuntimeError(f"{adapter.__name__} has no iter_parcels -- can't run a spatial query against it")

    envelope = {"xmin": row.xmin, "ymin": row.ymin, "xmax": row.xmax, "ymax": row.ymax,
                "spatialReference": {"wkid": 4326}}

    # Same job_queue retry/logging treatment as the name-first path's network call above --
    # this bbox query is only a cheap candidate prefilter (never pull a whole county's roll,
    # per iter_all_features's own docstring); the real classification decision below is a true
    # polygon test in PostGIS, never the bbox itself (CLAUDE.md: no bounding-box approximation).
    candidates = run_with_job_queue(
        lambda: list(adapter.iter_parcels(geometry=envelope)),
        job_type="gis_classifier_spatial_query", county_fips=county_fips, covid=covid,
        payload={"adapter": adapter.__name__, "base_url": adapter.BASE_URL, "envelope": envelope},
    )
    if not candidates:
        raise RuntimeError(
            f"no parcels found within covid {covid} tract {tract_no}'s bounding box at all -- "
            f"check the tract's own geometry before assuming the area is truly empty"
        )

    gis_source_id = insert_source(
        session, source_type="gis_api", reference=f"{adapter.BASE_URL} (spatial query)", confidence=None,
    )
    for p in candidates:
        upsert_parcel(
            session, county_fips=p["county_fips"], apn=p["apn"], owner_name_raw=p["owner_name_raw"],
            situs_address=p["situs_address"], city=p.get("city"), zip_code=p.get("zip_code"),
            acreage=p["acreage"], geojson=p["geojson"], source_id=gis_source_id,
            recited_legal_description=p.get("recited_legal_description"),
        )

    # Confirmed real (covid 4440, a ~2500-candidate bounding box -- the first tract large
    # enough to surface this): a small number of a county's OWN live GIS parcels can have
    # genuinely invalid geometry (e.g. "Nested shells" -- self-intersecting rings), which
    # crashes ST_Intersection with a GEOS TopologyException for the WHOLE batch, not just
    # that one parcel. Never seen on the small candidate pools every prior tract had.
    # Excluded from the real intersection test below (nothing correct can be computed
    # against a broken polygon) but surfaced in the return value rather than silently
    # dropped -- CLAUDE.md: never fabricate, flag for review instead.
    invalid_geometry_apns = [
        r.apn for r in session.execute(
            text("""
                SELECT apn FROM parcel
                WHERE county_fips = :county_fips AND apn = ANY(:apns) AND NOT ST_IsValid(geom)
            """),
            {"county_fips": county_fips, "apns": [p["apn"] for p in candidates]},
        ).fetchall()
    ]

    matched = session.execute(
        text("""
            SELECT p.apn,
                   ST_Contains(t.geom, p.geom) AS is_interior,
                   ST_Area(ST_Intersection(t.geom, p.geom)::geography)
                       / NULLIF(ST_Area(p.geom::geography), 0) AS overlap_fraction
            FROM parcel p, tract t
            WHERE p.county_fips = :county_fips AND p.apn = ANY(:apns)
              AND t.covid = :covid AND t.tract_no = :tract_no
              AND ST_IsValid(p.geom)
              AND ST_Intersects(t.geom, p.geom)
        """),
        {"county_fips": county_fips, "apns": [p["apn"] for p in candidates], "covid": covid, "tract_no": tract_no},
    ).fetchall()
    if not matched:
        raise RuntimeError(
            f"covid {covid} tract {tract_no}: {len(candidates)} candidates found in the bounding box, but none "
            f"actually intersect the tract's own polygon -- check the tract's geometry (may be mis-anchored) "
            f"before assuming the area is truly empty"
        )

    run_seq = session.execute(
        text("SELECT COALESCE(MAX(run_seq), 0) + 1 AS n FROM monitor_run WHERE covid = :covid"),
        {"covid": covid},
    ).fetchone().n

    session.execute(
        text("""
            INSERT INTO monitor_run (covid, run_seq, run_type, new_parcels_found, status)
            VALUES (:covid, :run_seq, 'initial', :n, 'ok')
        """),
        {"covid": covid, "run_seq": run_seq, "n": len(matched)},
    )

    for m in matched:
        classification = "interior" if m.is_interior else "boundary"
        confidence = 1.0 if m.is_interior else float(m.overlap_fraction or 0)
        session.execute(
            text("""
                INSERT INTO parcel_covenant (county_fips, apn, covid, tract_no, run_seq, classification,
                                              overlap_fraction, confidence, rationale)
                VALUES (:county_fips, :apn, :covid, :tract_no, :run_seq, :classification,
                        :overlap_fraction, :confidence, :rationale)
                ON CONFLICT (county_fips, apn, covid, tract_no, run_seq) DO NOTHING
            """),
            {
                "county_fips": county_fips, "apn": m.apn, "covid": covid, "tract_no": tract_no,
                "run_seq": run_seq, "classification": classification,
                "overlap_fraction": float(m.overlap_fraction) if m.overlap_fraction is not None else None,
                "confidence": confidence,
                "rationale": (
                    f"parcel geometry {'fully within' if m.is_interior else 'partially overlaps'} the covenant's "
                    f"own metes-and-bounds tract polygon (real spatial intersection, not a name/lot match)"
                ),
            },
        )

    # Real geometric residual -- the part of the tract's own independently-derived polygon
    # that no matched parcel actually covers. A single CTE so the union is computed once and
    # reused for both residual_geom and classified_acreage, rather than a naive sum of each
    # matched parcel's own acreage (which would over-count a 'boundary' parcel's area lying
    # outside the tract).
    session.execute(
        text("""
            WITH matched_union AS (
                SELECT ST_Union(p.geom) AS geom
                FROM parcel p
                JOIN parcel_covenant pc ON pc.county_fips = p.county_fips AND pc.apn = p.apn
                WHERE pc.covid = :covid AND pc.tract_no = :tract_no AND pc.run_seq = :run_seq
            )
            UPDATE tract SET
                residual_geom = ST_Multi(ST_Difference(tract.geom, matched_union.geom)),
                classified_acreage = (
                    ST_Area(tract.geom::geography) - ST_Area(ST_Difference(tract.geom, matched_union.geom)::geography)
                ) / 4046.8564224,
                source_id = :source_id, updated_at = now()
            FROM matched_union
            WHERE tract.covid = :covid AND tract.tract_no = :tract_no
        """),
        {"covid": covid, "tract_no": tract_no, "run_seq": run_seq, "source_id": gis_source_id},
    )

    if invalid_geometry_apns:
        session.execute(
            text("""
                UPDATE covenant SET status = 'needs_review', review_reason =
                    CASE WHEN review_reason IS NULL OR review_reason = '' THEN :note
                         ELSE review_reason || '; ' || :note END,
                    updated_at = now()
                WHERE covid = :covid
            """),
            {"covid": covid, "note": (
                f"GEOMETRY DATA QUALITY (automated): {len(invalid_geometry_apns)} parcel(s) in this tract's "
                f"bounding box have invalid geometry in the county's own GIS service ({', '.join(invalid_geometry_apns)}) "
                f"-- excluded from spatial classification rather than guessed at; needs manual review to confirm "
                f"whether any of them actually fall inside this covenant's tract"
            )},
        )

    return {
        "candidates_in_bbox": len(candidates),
        "matched_parcels": len(matched),
        "interior": sum(1 for m in matched if m.is_interior),
        "boundary": sum(1 for m in matched if not m.is_interior),
        "run_seq": run_seq,
        "invalid_geometry_apns": invalid_geometry_apns,
    }


def exclude_non_tract_parcels(session, covid: int, tract_no: int, apns: list[str], reason: str) -> dict:
    """Removes specific parcels from a tract's own classification that the true
    polygon intersection test matched, but that a human reading of the deed's
    own legal description knows are NOT actually part of the encumbered land --
    the spatial-first census is only as good as the tract polygon's own
    precision (CLAUDE.md's own caveat on every anchoring technique in this
    project), and a deed-cited ADJOINING tract (named only to tie a boundary
    corner, e.g. "a called 30 acre tract described in deed to Tessie Belle
    Carroll") can get spuriously caught if the polygon runs even slightly wide
    at that edge.

    Confirmed real (covid 4440, both tracts, 2026-07-30): Tract II's own deed
    text ties a ~7,000+ ft stretch of its northern boundary to "the centerline
    of F. M. 2090" -- any parcel actually north of that real road is adjoining
    land, never this covenant's own. Tract I's deed cites dozens of small
    adjoining tracts by name along its western/southern lines (Carroll 30 ac,
    two separate Duke 5.104 ac tracts, the Bowdoin 12/13 ac tracts already
    used as this tract's own anchor ties) -- all correctly excluded here by
    matching the modern parcel's owner/acreage back to those specific deed
    citations, not by geometry alone.

    This is a genuine human judgment call, not a mechanical rule: a raw,
    unplatted parcel sharing this deed's own survey abstract number is kept
    when its owner or its own legal description ties it to one of this
    tract's confirmed real subdivisions or the municipal utility districts
    serving them (e.g. "TRACT ME13 DIR LOT" ties to MUD #13, already
    confirmed elsewhere in this same tract at 100% interior overlap), and
    excluded when it ties to nothing this project has independently confirmed
    -- an unconnected commercial owner, a deed-cited adjoiner, or a generic
    "director lot" cluster with no matching MUD number. Never deleted
    silently: every excluded APN is named in the covenant's own review_reason,
    same as this module's own GEOMETRY DATA QUALITY note.

    Removes the parcel_covenant rows across every run_seq (these were never a
    legitimate match at any point, unlike a genuine later replat), then
    recomputes classified_acreage/residual_geom from the remaining matches at
    the tract's own latest run_seq -- identical math to classify_metes_and_
    bounds_tract's own residual computation, just re-run over a corrected
    parcel set."""
    row = session.execute(text("SELECT county_fips FROM covenant WHERE covid = :covid"), {"covid": covid}).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} not found")
    county_fips = row.county_fips

    deleted = session.execute(
        text("""
            DELETE FROM parcel_covenant
            WHERE covid = :covid AND tract_no = :tract_no AND county_fips = :county_fips AND apn = ANY(:apns)
        """),
        {"covid": covid, "tract_no": tract_no, "county_fips": county_fips, "apns": apns},
    ).rowcount

    run_seq = session.execute(
        text("SELECT MAX(run_seq) AS n FROM parcel_covenant WHERE covid = :covid AND tract_no = :tract_no"),
        {"covid": covid, "tract_no": tract_no},
    ).fetchone().n
    if run_seq is not None:
        session.execute(
            text("""
                WITH matched_union AS (
                    SELECT ST_Union(p.geom) AS geom
                    FROM parcel p
                    JOIN parcel_covenant pc ON pc.county_fips = p.county_fips AND pc.apn = p.apn
                    WHERE pc.covid = :covid AND pc.tract_no = :tract_no AND pc.run_seq = :run_seq
                )
                UPDATE tract SET
                    residual_geom = ST_Multi(ST_Difference(tract.geom, matched_union.geom)),
                    classified_acreage = (
                        ST_Area(tract.geom::geography) - ST_Area(ST_Difference(tract.geom, matched_union.geom)::geography)
                    ) / 4046.8564224,
                    updated_at = now()
                FROM matched_union
                WHERE tract.covid = :covid AND tract.tract_no = :tract_no
            """),
            {"covid": covid, "tract_no": tract_no, "run_seq": run_seq},
        )

    existing = session.execute(text("SELECT status, review_reason FROM covenant WHERE covid = :covid"), {"covid": covid}).fetchone()
    tag = f"NON-TRACT PARCEL EXCLUSION (automated, tract {tract_no})"
    prior = re.sub(rf";?\s*{re.escape(tag)}:.*?(?=;\s*[A-Z][A-Z0-9 -]*\(automated|$)", "",
                   existing.review_reason or "", flags=re.DOTALL).strip("; ").strip()
    note = f"{tag}: {reason} ({', '.join(apns)})"
    new_reason = f"{prior}; {note}" if prior else note
    status = existing.status if existing.status in ("title_in_progress", "done") else "needs_review"
    session.execute(
        text("UPDATE covenant SET status = :status, review_reason = :reason, updated_at = now() WHERE covid = :covid"),
        {"status": status, "reason": new_reason, "covid": covid},
    )

    tract_row = session.execute(
        text("SELECT classified_acreage FROM tract WHERE covid = :covid AND tract_no = :tract_no"),
        {"covid": covid, "tract_no": tract_no},
    ).fetchone()
    return {"excluded_count": deleted, "classified_acreage": float(tract_row.classified_acreage) if tract_row and tract_row.classified_acreage is not None else None}
