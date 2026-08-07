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
from app.db.review_notes import merge_tagged_note
from app.gis.adapters import (
    bexar_tx, collin_tx, dallas_tx, denton_tx, douglas_co, harris_tx, hunt_tx, kerr_tx, llano_tx,
    montgomery_tx, nueces_tx, tarrant_tx, travis_tx,
)
from app.ingestion.walk import get_deed_text
from app.parsing.legal_description.adjoiners import extract_adjoining_subdivisions
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
    tract_refs = row.legal_description_parsed
    if tract_refs is None:
        # Confirmed real, previously-undetected gap: parse_subdivision_reference()
        # existed and was correct in isolation but had no caller anywhere in the
        # pipeline, so legal_description_parsed was never populated by anything --
        # this raised "has no legal_description_parsed to resolve from" on
        # essentially every subdivision_plat covenant rather than actually
        # resolving one. Parse and persist it here, on first need, instead of
        # requiring a separate ingestion-time step.
        if not row.legal_description_raw:
            raise RuntimeError(f"covid {covid} has no legal_description_raw to parse a subdivision reference from")
        tract_refs = parse_subdivision_reference(row.legal_description_raw)
        session.execute(
            text("UPDATE covenant SET legal_description_parsed = (:parsed)::jsonb, updated_at = now() "
                 "WHERE covid = :covid"),
            {"covid": covid, "parsed": json.dumps(tract_refs)},
        )
    # tract_refs is a LIST -- one entry per distinct tract this covenant's legal
    # description describes, in document order -- never a single reference shared
    # across every tract_no. Confirmed real and necessary, not defensive-programming
    # theater: covid 4123 describes two tracts under two different subdivisions;
    # a single shared reference silently reused tract 2's own lots when tract 1 was
    # (re-)resolved, corrupting its classification with the wrong parcels. Out-of-
    # range is a hard error, never a silent fall-back to a different tract's entry.
    if tract_no > len(tract_refs) or tract_no < 1:
        raise RuntimeError(
            f"covid {covid}: tract_no={tract_no} requested but only {len(tract_refs)} distinct tract "
            f"reference(s) were parsed from this covenant's legal description -- never reusing a "
            f"different tract's own subdivision/lot reference"
        )
    parsed = tract_refs[tract_no - 1]

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

    # acreage is the county's own attribute where populated; some counties
    # (e.g. Harris, for smaller/commercial parcels) leave it NULL even though
    # the parcel has real geometry, so fall back to computing that PARCEL's
    # own acreage from its geometry rather than leaving it uncounted.
    #
    # The COALESCE must be applied per-row (COALESCE(acreage, ST_Area(geom...)))
    # then summed -- NOT COALESCE(SUM(acreage), ...) as this used to read. SQL's
    # SUM() silently skips NULL rows rather than returning NULL for the whole
    # aggregate; it only returns NULL if EVERY row is NULL. A tract with, say,
    # 90 parcels carrying real acreage and 10 with NULL acreage never triggered
    # the outer COALESCE's fallback at all -- SUM(acreage) still returned a real
    # number (just the 90's total), silently missing the other 10 parcels'
    # acreage entirely rather than falling back to their own geometry.
    session.execute(
        text("""
            INSERT INTO tract (covid, tract_no, geom, classified_acreage, boundary_resolution_method, source_id, updated_at)
            SELECT :covid, :tract_no, ST_Multi(ST_Union(geom)),
                   SUM(COALESCE(acreage, ST_Area(geom::geography) / 4046.8564224)),
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
    #
    # Confirmed real, not hypothetical (covid 4781, APN 389449): "invalid" here usually
    # means a minor self-touching ring, not a fundamentally broken shape -- ST_MakeValid
    # repaired this one to within 0.03 ac of its own recorded acreage (2.409 vs 2.408),
    # and the repaired shape turned out to be 99.9% inside the tract -- a real interior
    # parcel that sat in "needs manual review" limbo for no reason. Blanket-excluding
    # every invalid geometry threw away a real, cheaply-recoverable answer. Repair is
    # only trusted when the repaired area is close to the parcel's OWN recorded acreage
    # (same 5% tolerance this project already uses for LLM-derived anchors) -- a repair
    # that changes the area wildly means ST_MakeValid guessed at fixing something more
    # broken than a simple self-touch, and that parcel still gets excluded/flagged.
    _MAKEVALID_MAX_ACREAGE_DEVIATION = 0.05
    invalid_geometry_rows = session.execute(
        text("""
            SELECT apn, acreage,
                   ST_IsValid(ST_MakeValid(geom)) AS repair_valid,
                   ST_Area(ST_MakeValid(geom)::geography) / 4046.8564224 AS repaired_acres
            FROM parcel
            WHERE county_fips = :county_fips AND apn = ANY(:apns) AND NOT ST_IsValid(geom)
        """),
        {"county_fips": county_fips, "apns": [p["apn"] for p in candidates]},
    ).fetchall()
    repairable_apns = [
        r.apn for r in invalid_geometry_rows
        if r.repair_valid and r.acreage and r.repaired_acres
        and abs(r.repaired_acres - float(r.acreage)) / float(r.acreage) <= _MAKEVALID_MAX_ACREAGE_DEVIATION
    ]

    # FALLBACK when the county records no acreage to verify against. Confirmed
    # real and costly (covid 5838, Nueces): the acreage gate above can never
    # pass for a parcel whose acreage is NULL or 0.000, so five genuine parcels
    # -- Palmilla Beach PUD private streets and common areas, exactly the kind
    # of parcel a county leaves unvalued, which is WHY acreage is null -- were
    # excluded on every single run. They accounted for 11.41 of that tract's
    # 17.22-acre unexplained residual, i.e. two thirds of a gap that looked
    # like missing land and was actually this gate failing closed.
    #
    # The obvious alternative -- compare the repaired area to the raw
    # geometry's own area -- was tried and REJECTED: "Nested shells" (every
    # case seen in this project) double-counts area by construction, so the
    # raw figure is meaningless and reads a 38-68% "loss" on a correct repair.
    #
    # What actually verifies the repair is whether it fits the surrounding
    # parcel fabric: a repair that invented or misplaced area would overlap
    # neighbouring parcels, because real parcels do not overlap each other. On
    # covid 5838 the five genuine repairs collided with 0.0% of any neighbour
    # while the two that genuinely belong elsewhere were untouched by this
    # (they simply don't intersect the tract, and are dropped later by the
    # normal spatial test). A parcel WITH usable acreage that fails the
    # acreage check is never rescued here -- a stated 10 acres repairing to 30
    # is a real discrepancy, and the county's own figure stays authoritative.
    _MAKEVALID_MAX_FABRIC_OVERLAP = 0.02
    unverifiable = [
        r.apn for r in invalid_geometry_rows
        if r.repair_valid and r.apn not in repairable_apns and not (r.acreage and float(r.acreage) > 0)
    ]
    fabric_ok = []
    if unverifiable:
        fabric_ok = [
            r.apn for r in session.execute(
                text("""
                    WITH repaired AS (
                        SELECT apn, ST_MakeValid(geom) AS geom FROM parcel
                        WHERE county_fips = :county_fips AND apn = ANY(:unverifiable)
                    ), neighbours AS (
                        SELECT apn, CASE WHEN ST_IsValid(geom) THEN geom ELSE ST_MakeValid(geom) END AS geom
                        FROM parcel WHERE county_fips = :county_fips AND apn = ANY(:apns)
                    )
                    SELECT r.apn
                    FROM repaired r LEFT JOIN neighbours n
                      ON n.apn <> r.apn AND ST_Intersects(r.geom, n.geom)
                    GROUP BY r.apn, r.geom
                    HAVING ST_Area(r.geom::geography) > 0
                       AND COALESCE(SUM(ST_Area(ST_Intersection(r.geom, n.geom)::geography)), 0)
                           / ST_Area(r.geom::geography) <= :max_overlap
                """),
                {"county_fips": county_fips, "unverifiable": unverifiable,
                 "apns": [p["apn"] for p in candidates], "max_overlap": _MAKEVALID_MAX_FABRIC_OVERLAP},
            )
        ]
        repairable_apns = repairable_apns + fabric_ok

    invalid_geometry_apns = [r.apn for r in invalid_geometry_rows if r.apn not in repairable_apns]

    matched = session.execute(
        text("""
            WITH usable_parcels AS (
                SELECT apn, recited_legal_description, owner_name_raw,
                       CASE WHEN ST_IsValid(geom) THEN geom ELSE ST_MakeValid(geom) END AS geom
                FROM parcel
                WHERE county_fips = :county_fips AND apn = ANY(:apns)
                  AND (ST_IsValid(geom) OR apn = ANY(:repairable_apns))
            )
            SELECT apn, recited_legal_description, owner_name_raw, is_interior, was_repaired,
                   overlap_m2, overlap_m2 / NULLIF(parcel_m2, 0) AS overlap_fraction
            FROM (
                SELECT p.apn, p.recited_legal_description, p.owner_name_raw,
                       ST_Contains(t.geom, p.geom) AS is_interior,
                       p.apn = ANY(:repairable_apns) AS was_repaired,
                       -- the real overlap AREA, kept alongside the fraction: a
                       -- fraction alone can't tell a genuine narrow clip off a
                       -- huge parcel (small fraction, acres of real encumbered
                       -- land) from a digitization touch worth square
                       -- centimetres. Computed once here and reused for the
                       -- fraction, so this costs nothing extra.
                       ST_Area(ST_Intersection(t.geom, p.geom)::geography) AS overlap_m2,
                       ST_Area(p.geom::geography) AS parcel_m2
                FROM usable_parcels p, tract t
                WHERE t.covid = :covid AND t.tract_no = :tract_no
                  AND ST_Intersects(t.geom, p.geom)
            ) x
        """),
        {"county_fips": county_fips, "apns": [p["apn"] for p in candidates], "covid": covid, "tract_no": tract_no,
         "repairable_apns": repairable_apns},
    ).fetchall()
    if not matched:
        raise RuntimeError(
            f"covid {covid} tract {tract_no}: {len(candidates)} candidates found in the bounding box, but none "
            f"actually intersect the tract's own polygon -- check the tract's geometry (may be mis-anchored) "
            f"before assuming the area is truly empty"
        )

    # Parcels a human already ruled out stay out, however the geometry reads --
    # otherwise this rebuild silently resurrects them (migration 0034).
    already_excluded = excluded_apns(session, covid, tract_no)
    if already_excluded:
        matched = [m for m in matched if m.apn not in already_excluded]

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
                    + (
                        (
                            f"; the county's own GIS geometry for this parcel was invalid (self-touching "
                            f"ring) and repaired via ST_MakeValid -- the county records no usable acreage "
                            f"for it, so the repair was verified instead by confirming it overlaps no other "
                            f"parcel by more than {_MAKEVALID_MAX_FABRIC_OVERLAP:.0%} of its own area (real "
                            f"parcels do not overlap, so a repair that invented area would collide)"
                            if m.apn in fabric_ok else
                            f"; the county's own GIS geometry for this parcel was invalid (self-touching "
                            f"ring) and repaired via ST_MakeValid -- repaired area verified within "
                            f"{_MAKEVALID_MAX_ACREAGE_DEVIATION:.0%} of the parcel's own recorded acreage "
                            f"before being trusted"
                        )
                        if m.was_repaired else ""
                    )
                ),
            },
        )

    # Real geometric residual -- the part of the tract's own independently-derived polygon
    # that no matched parcel actually covers. A single CTE so the union is computed once and
    # reused for both residual_geom and classified_acreage, rather than a naive sum of each
    # matched parcel's own acreage (which would over-count a 'boundary' parcel's area lying
    # outside the tract).
    #
    # Same on-the-fly ST_MakeValid as the matched query above, not upsert_parcel's own
    # ON CONFLICT DO UPDATE -- that always overwrites parcel.geom with EXCLUDED.geom on
    # every sync, so persisting a repair there would just get silently wiped the next
    # time this parcel's county GIS data gets re-synced, reintroducing the exact bug this
    # was meant to fix.
    session.execute(
        text("""
            WITH matched_union AS (
                SELECT ST_Union(CASE WHEN ST_IsValid(p.geom) THEN p.geom ELSE ST_MakeValid(p.geom) END) AS geom
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
        _write_covenant_note(
            session, covid, "GEOMETRY DATA QUALITY",
            f"GEOMETRY DATA QUALITY (automated): {len(invalid_geometry_apns)} parcel(s) in this tract's "
            f"bounding box have invalid geometry in the county's own GIS service ({', '.join(invalid_geometry_apns)}) "
            f"-- excluded from spatial classification rather than guessed at, needs manual review to confirm "
            f"whether any of them actually fall inside this covenant's tract",
        )

    deed_text = get_deed_text(
        session, covid,
        session.execute(
            text("SELECT legal_description_raw FROM covenant WHERE covid = :covid"), {"covid": covid},
        ).scalar(),
    )
    sliver_groups = _detect_sliver_subdivision_clusters(
        matched, extract_adjoining_subdivisions(deed_text),
    )
    if sliver_groups:
        # " | ", never "; " -- a semicolon followed by these upper-case
        # subdivision names and their parenthesised detail is byte-identical to
        # a note boundary, and split this very note in half. See
        # app/db/review_notes.py's own note-author constraint.
        apns_note = " | ".join(
            f"{g['subdivision']} ({len(g['apns'])} parcels, overlap {g['min_overlap']:.0%}-{g['max_overlap']:.0%}, "
            f"evidence={g['evidence']}: {', '.join(g['apns'])})" for g in sliver_groups
        )
        _write_covenant_note(
            session, covid, "POSSIBLE NON-TRACT SUBDIVISION", (
                f"POSSIBLE NON-TRACT SUBDIVISION (automated): {len(sliver_groups)} boundary-classified "
                f"subdivision(s) show a uniformly low, tightly-clustered overlap fraction with no member "
                f"above {_SLIVER_OVERLAP_MAX_FRACTION:.0%} -- the signature of a nearby/adjoining platted "
                f"subdivision whose own recorded lot lines just barely clip this tract's polygon at normal "
                f"survey/digitization tolerance, not genuine partial encumbrance (confirmed real on covid "
                f"8534 tract 1: Forman Williamsburg Square, a subdivision the deed's own Exhibit A names as "
                f"adjoining -- not part of -- this tract). evidence=deed_names_as_adjoining means the deed's "
                f"own text independently corroborates it (geometry AND text agree); evidence=geometry_only "
                f"means the deed never names it, so the sliver pattern is the only signal. Any subdivision "
                f"the deed DERIVES this tract from is never listed here. Still needs a human check against "
                f"the deed before calling exclude_non_tract_parcels(): {apns_note}"
            ),
        )

    negligible = _detect_negligible_overlap_parcels(matched)
    if negligible:
        detail = ", ".join(f"{n['apn']} ({n['overlap_m2']:.2f} m2)" for n in negligible)
        _write_covenant_note(
            session, covid, "NEGLIGIBLE OVERLAP", (
                f"NEGLIGIBLE OVERLAP (automated): {len(negligible)} boundary parcel(s) intersect this "
                f"tract by less than {_MIN_OVERLAP_AREA_M2:.0f} m2 -- a digitization artifact where two "
                f"independently-surveyed boundaries cross by a hair, not encumbered land. Unlike a "
                f"low-overlap FRACTION (which can still be acres on a large parcel), an absolute area "
                f"this small cannot represent a real property interest. Each is currently recorded as "
                f"encumbered and would carry fee liability, so confirm and remove via "
                f"exclude_non_tract_parcels(): {detail}"
            ),
        )

    public = _detect_public_property_parcels(matched)
    if public:
        owners = sorted({p["owner"] for p in public})
        detail = " | ".join(
            f"{o} ({', '.join(p['apn'] for p in public if p['owner'] == o)})" for o in owners
        )
        _write_covenant_note(
            session, covid, "POSSIBLE PUBLIC PROPERTY", (
                f"POSSIBLE PUBLIC PROPERTY (automated): {len(public)} matched parcel(s) across "
                f"{len(owners)} owner(s) appear to be government-owned. This covenant's own template "
                f"carves that out -- 'SAVE AND EXCEPT any portion of the Property owned by a "
                f"governmental entity ... This Declaration shall not apply to Public Property' -- so "
                f"they may not be encumbered at all, and each currently carries fee liability. "
                f"Whether a municipal utility district or a housing authority counts as a "
                f"'governmental entity' for this clause is a legal determination, so this is only a "
                f"flag: confirm, then remove via exclude_non_tract_parcels(). {detail}"
            ),
        )

    return {
        "candidates_in_bbox": len(candidates),
        "matched_parcels": len(matched),
        "interior": sum(1 for m in matched if m.is_interior),
        "boundary": sum(1 for m in matched if not m.is_interior),
        "run_seq": run_seq,
        "invalid_geometry_apns": invalid_geometry_apns,
        "repaired_geometry_apns": repairable_apns,
        "possible_non_tract_subdivisions": sliver_groups,
        "negligible_overlap_parcels": negligible,
        "possible_public_property": public,
    }


# Deliberately NOT app.gis.plat_parser.parse_plat_reference -- that function's own
# subdivision_name is tuned for exact plat-lookup identity (Montgomery/Collin's own
# recorder-index naming conventions) and doesn't cleanly separate a phase/block suffix
# from every county's format (e.g. Denton's "FORMAN WILLIAMSBURG SQUARE PH II BLK A LOT
# 22" -- confirmed via direct testing). This only needs a rough, stable grouping key for
# the anomaly check below, not a canonical subdivision identity.
_SUBDIVISION_SUFFIX_RE = re.compile(r"\s+(?:PHASE|PH|BLK|BLOCK|LOT|LT)\b", re.IGNORECASE)

# A county writes the same real subdivision both with and without a generic
# descriptor -- Denton CAD has "SHERMAN CROSSING ADDITION BLK A LOT 5R" and
# "SHERMAN CROSSING PHASE 2B BLK B LOT 1" for one subdivision. Left in, those
# become two group keys, which both risks a spurious flag on one half and, worse,
# can drop each half below the >=2-member threshold so a genuine adjoiner is
# never flagged at all -- a silent miss, the failure mode that costs a human the
# manual catch.
#
# Exactly ONE trailing descriptor is removed, and only when enough name survives
# it: stripping repeatedly turns "OAK ADDITION ESTATES" into a useless "OAK",
# which both over-merges genuinely distinct subdivisions and falls below the
# deed-matching floor (_MIN_NAME_MATCH_CHARS), silently disabling the veto.
_GENERIC_DESCRIPTOR_RE = re.compile(
    r"\s+(?:ADDITION|ADDN|SUBDIVISION|SUBD|ESTATES?|SEC|SECTION|UNIT)\s*$", re.IGNORECASE,
)
_MIN_GROUP_KEY_CHARS = 6

# Confirmed real (covid 8534 tract 1, Denton County): a genuine boundary-straddling
# parcel can land anywhere from near-0% to near-100% overlap depending on exactly where
# the historical dividing line fell -- but a sliver-overlap ARTIFACT from a nearby,
# unrelated subdivision's platted edge just barely clipping this tract's own polygon
# never produces a large overlap, because the true encumbered portion is negligible.
# Forman Williamsburg Square (15 parcels, 10.9%-26.4% overlap) and Hercules West
# Addition (25 parcels, 6.2%-48.9% overlap) both cleanly separated below this threshold
# from Sherman Crossing's own genuinely-platted-from-this-tract parcels (87.0%-97.3%).
_SLIVER_OVERLAP_MAX_FRACTION = 0.5

# A parcel whose real intersection with the tract is smaller than this is a
# digitization artifact -- two boundaries drawn from different surveys crossing
# by a hair -- not encumbered land. 10 m2 is ~108 sq ft; no real property
# interest is conveyed in a strip that size, so the threshold is deliberately
# absolute rather than a fraction. The distinction matters: a 2% clip off a
# 100-acre parcel is a small FRACTION but ~3,200 m2 of genuinely encumbered
# land and must be kept, while a suburban lot touching the boundary by 0.05 m2
# cannot be encumbered anything.
#
# Confirmed real and consequential (survey across all metes-and-bounds tracts,
# 2026-08-06): 52 parcels across 9 tracts fall below this. covid 8245 was the
# starkest -- 3 of its 6 matched parcels overlapped by 0.01-0.06 m2 (square
# centimetres), all private homeowners recorded as subject to a 1% transfer-fee
# covenant, two of them already carrying recorded non-exempt transfers from the
# chain walk. The subdivision-cluster check above structurally cannot catch
# these: a parcel with no recited_legal_description gets no group key at all,
# and one genuinely-straddling member exempts a whole subdivision.
_MIN_OVERLAP_AREA_M2 = 10.0

# 22 of the 23 covenants in this corpus carve out government-owned land in their
# own template text, verbatim:
#
#   "SAVE AND EXCEPT any portion of the Property owned by a governmental entity
#    (whether state, local, city, municipality, federal, or otherwise,
#    hereinafter "Public Property"). This Declaration shall not apply to Public
#    Property."
#
# Nothing in this pipeline read that clause, so 43 government-owned parcels
# (~374 acres) sit in the encumbered census today: Splendora and Montgomery
# ISD school sites, Blaketree and East Montgomery County MUDs, the City of Port
# Aransas, and the Denton and Corpus Christi housing authorities.
#
# Deliberately a FLAG, not an exclusion. Whether a municipal utility district or
# a housing authority is a "governmental entity" for this clause is a legal
# determination with real fee consequences, not something to encode from deed
# text alone -- so this surfaces the parcels and leaves the call to a human,
# exactly as the non-tract checks do.
#
# Patterns are deliberately precise rather than broad: an earlier "% COUNTY%"
# draft matched "BROWNS MAVERICK COUNTY RANCH LP", a private partnership.
# Validated against all 4,750 distinct owner names in the parcel table -- 19
# match, every one genuinely governmental, no false positives.
_PUBLIC_OWNER_RE = re.compile(
    r"\bCITY OF\b|\bTOWN OF\b|\bVILLAGE OF\b|\bCOUNTY OF\b|,\s*CITY OF\b"
    r"|\bI\.?S\.?D\b|\bINDEPENDENT SCHOOL DIST|\bSCHOOL DISTRICT\b"
    r"|\bMUNICIPAL UTILITY DIST|\bM\.?U\.?D\.?\s*#?\s*\d|\bUTILITY DISTRICT\b"
    r"|\bHOUSING AUTHORITY\b|\bSTATE OF TEXAS\b|\bUNITED STATES\b"
    r"|\bDRAINAGE DIST|\bNAVIGATION DIST|\bWATER CONTROL|\bIMPROVEMENT DISTRICT\b"
    r"|\bCOLLEGE DIST|\bCOMMUNITY COLLEGE\b|\bTXDOT\b|\bTEXAS DEPARTMENT OF\b"
    r"|\bPUBLIC LIBRARY\b",
    re.IGNORECASE,
)


def _sliver_group_key(recited_legal_description: str | None) -> str | None:
    if not recited_legal_description:
        return None
    key = _SUBDIVISION_SUFFIX_RE.split(recited_legal_description.strip(), maxsplit=1)[0].strip().upper()
    stripped = _GENERIC_DESCRIPTOR_RE.sub("", key, count=1).strip()
    if len(stripped) >= _MIN_GROUP_KEY_CHARS:
        key = stripped
    return key or None


def excluded_apns(session, covid: int, tract_no: int) -> set[str]:
    """APNs a human has reviewed and ruled OUT of this tract's encumbered land
    (migration 0034). Both classify_metes_and_bounds_tract and
    monitor_tract_for_new_plats must consult this before writing
    parcel_covenant, or they rebuild the census from geometry and silently
    resurrect every exclusion -- reproduced on covid 8245."""
    return {
        r.apn for r in session.execute(
            text("SELECT apn FROM parcel_covenant_exclusion WHERE covid = :covid AND tract_no = :tract_no"),
            {"covid": covid, "tract_no": tract_no},
        )
    }


def restore_excluded_parcels(session, covid: int, tract_no: int, apns: list[str], reason: str) -> dict:
    """The explicit un-exclude path: drop the exclusion record so the next
    classification run can legitimately re-add these parcels.

    Before migration 0034 there was no such path, because none was needed --
    re-running classification restored a parcel automatically, which is exactly
    the bug that made exclusions worthless. Restoring is now a deliberate,
    recorded act (covid 4440 really needed it: 6 of 28 parcels were excluded in
    error and restored after deed-history verification confirmed they trace back
    to the tract's own original grantor).

    Does not re-insert parcel_covenant rows itself -- run
    classify_metes_and_bounds_tract afterwards, so the parcels come back through
    the same real spatial test as everything else rather than being asserted."""
    row = session.execute(text("SELECT county_fips FROM covenant WHERE covid = :covid"), {"covid": covid}).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} not found")

    removed = session.execute(
        text("""
            DELETE FROM parcel_covenant_exclusion
            WHERE covid = :covid AND tract_no = :tract_no AND county_fips = :county_fips AND apn = ANY(:apns)
        """),
        {"covid": covid, "tract_no": tract_no, "county_fips": row.county_fips, "apns": apns},
    ).rowcount
    _write_covenant_note(
        session, covid, f"EXCLUSION REVERSED (automated, tract {tract_no})",
        f"EXCLUSION REVERSED (automated, tract {tract_no}): {reason} ({', '.join(apns)}). "
        f"Re-run classify_metes_and_bounds_tract to bring them back through the real spatial test.",
    )
    return {"restored_count": removed}


def _write_covenant_note(session, covid: int, tag: str, note: str, status: str | None = "needs_review") -> None:
    """Strip-then-replace this stage's own tagged note (never another stage's)
    and flag the covenant. Previously both of this module's notes were appended
    unconditionally, so every re-classification -- which happens on each monitor
    cycle -- stacked another identical copy in review_reason. Pass status=None
    to leave covenant.status alone."""
    existing = session.execute(
        text("SELECT status, review_reason FROM covenant WHERE covid = :covid"), {"covid": covid},
    ).fetchone()
    merged = merge_tagged_note(existing.review_reason if existing else None, tag, note)
    if status is None:
        session.execute(
            text("UPDATE covenant SET review_reason = :reason, updated_at = now() WHERE covid = :covid"),
            {"reason": merged, "covid": covid},
        )
        return
    # Never regress a covenant that has already moved past this stage.
    new_status = existing.status if existing and existing.status in ("title_in_progress", "done") else status
    session.execute(
        text("UPDATE covenant SET status = :status, review_reason = :reason, updated_at = now() "
             "WHERE covid = :covid"),
        {"status": new_status, "reason": merged, "covid": covid},
    )


def _squash(name: str) -> str:
    """Alphanumerics only, so 'GULF SIDE ESTATES SUBDIVISION' (deed) and
    'GULFSIDE ESTATES' (the CAD's own spelling of the same real subdivision --
    both confirmed real on covid 5838) compare equal by containment. A leading
    'THE' is dropped for the same reason ('THE HEIGHTS AT WESTRIDGE' vs the
    deed's own 'Heights At Westridge Phase II', covid 4981)."""
    squashed = re.sub(r"[^A-Z0-9]", "", name.upper())
    return squashed[3:] if squashed.startswith("THE") else squashed


# Below this, a containment match between two squashed names is coincidence
# rather than evidence (e.g. a 4-character name inside a long one).
_MIN_NAME_MATCH_CHARS = 8


def _deed_role_for(subdivision: str, deed_adjoiners: list[dict]) -> str | None:
    """'adjoining', 'derivation', or None if the deed never names this
    subdivision. A derivation match anywhere wins -- see adjoiners.py."""
    key = _squash(subdivision)
    role = None
    for entry in deed_adjoiners:
        other = _squash(entry["subdivision"])
        if len(key) < _MIN_NAME_MATCH_CHARS or len(other) < _MIN_NAME_MATCH_CHARS:
            continue
        if key in other or other in key:
            if entry["role"] == "derivation":
                return "derivation"
            role = "adjoining"
    return role


def _detect_public_property_parcels(matched) -> list[dict]:
    """Matched parcels whose owner of record looks like a governmental entity,
    which 22 of this corpus' 23 covenants carve out of the encumbered Property
    by their own terms -- see _PUBLIC_OWNER_RE.

    Interior parcels ARE included, unlike the other two checks: a school site or
    MUD tract sitting wholly inside the tract is precisely the case the clause is
    about, and it is the owner that matters here, not the geometry."""
    flagged = [
        {"apn": m.apn, "owner": m.owner_name_raw,
         "classification": "interior" if m.is_interior else "boundary"}
        for m in matched
        if m.owner_name_raw and _PUBLIC_OWNER_RE.search(m.owner_name_raw)
    ]
    return sorted(flagged, key=lambda d: (d["owner"] or "", d["apn"]))


def _detect_negligible_overlap_parcels(matched) -> list[dict]:
    """Boundary parcels whose real intersection with the tract is below
    _MIN_OVERLAP_AREA_M2 -- see that constant. Per-parcel and independent of
    any subdivision grouping, which is the point: these are exactly the cases
    the cluster check cannot reach.

    Interior parcels are never considered -- fully contained means encumbered,
    however small the parcel. A review flag, not an exclusion: removing a
    parcel from the encumbered census stays a human decision through
    exclude_non_tract_parcels, same as every other correction in this module."""
    flagged = [
        {"apn": m.apn, "overlap_m2": float(m.overlap_m2 or 0),
         "overlap_fraction": float(m.overlap_fraction or 0),
         "legal": m.recited_legal_description}
        for m in matched
        if not m.is_interior and m.overlap_m2 is not None and m.overlap_m2 < _MIN_OVERLAP_AREA_M2
    ]
    return sorted(flagged, key=lambda d: d["overlap_m2"])


def _detect_sliver_subdivision_clusters(matched, deed_adjoiners: list[dict] | None = None) -> list[dict]:
    """Groups boundary (non-interior) parcels by a rough subdivision-name key and
    flags any group of >=2 parcels whose overlap_fraction is uniformly below
    _SLIVER_OVERLAP_MAX_FRACTION -- see that constant's own comment. A single
    low-overlap parcel is left alone (a lone genuine partial-lot split is plausible;
    a whole block of them sharing one nearby subdivision's name is not) -- this is a
    review flag, never an automatic exclusion (that judgment call stays with
    exclude_non_tract_parcels, per its own docstring).

    `deed_adjoiners` (from app/parsing/legal_description/adjoiners.py) turns the
    purely-geometric flag into a two-signal one, and more importantly supplies a
    VETO. Each flagged group carries an `evidence` field:
      - 'deed_names_as_adjoining' -- the deed itself only ever ties a boundary to
        this subdivision, never conveys it (covid 8534's Forman Williamsburg
        Square). Geometry AND text agree: strong.
      - 'geometry_only' -- the deed never names it at all, so the sliver pattern
        is the only evidence (covid 8534's Hercules West Addition, platted long
        after the covenant). Real, but weaker -- still needs the human check.
    A group the deed DERIVES the tract from is dropped from the flag list
    entirely rather than reported weakly: on covid 4781 the tract was itself
    platted into Watermark Section One, and on covid 5838 the tract IS a lot in
    Gulfside Estates -- suggesting either for exclusion would drop genuinely
    encumbered land, the one error CLAUDE.md's accuracy-over-completeness rule
    is most concerned with."""
    deed_adjoiners = deed_adjoiners or []
    groups: dict[str, list] = {}
    for m in matched:
        if m.is_interior:
            continue
        key = _sliver_group_key(m.recited_legal_description)
        if key is None:
            continue
        groups.setdefault(key, []).append(m)

    flagged = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        fractions = [float(m.overlap_fraction or 0) for m in members]
        if max(fractions) >= _SLIVER_OVERLAP_MAX_FRACTION:
            continue
        role = _deed_role_for(key, deed_adjoiners)
        if role == "derivation":
            continue
        flagged.append({
            "subdivision": key,
            "apns": [m.apn for m in members],
            "min_overlap": min(fractions),
            "max_overlap": max(fractions),
            "evidence": "deed_names_as_adjoining" if role == "adjoining" else "geometry_only",
        })
    return flagged


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

    # Record the judgment FIRST, in a table that survives re-classification.
    # Deleting the parcel_covenant rows alone is not enough: that census is
    # rebuilt from geometry by both classify_metes_and_bounds_tract and
    # monitor_tract_for_new_plats, so before migration 0034 every exclusion was
    # one scheduled monitor run away from being silently undone (reproduced on
    # covid 8245 -- see that migration's own docstring).
    for apn in apns:
        session.execute(
            text("""
                INSERT INTO parcel_covenant_exclusion (county_fips, apn, covid, tract_no, reason)
                VALUES (:county_fips, :apn, :covid, :tract_no, :reason)
                ON CONFLICT (county_fips, apn, covid, tract_no)
                DO UPDATE SET reason = EXCLUDED.reason, excluded_at = now()
            """),
            {"county_fips": county_fips, "apn": apn, "covid": covid, "tract_no": tract_no, "reason": reason},
        )

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

    # Fully-qualified tag, so tract 1's and tract 2's exclusion notes coexist
    # (covid 4440 really carries both) -- a bare tag would delete the sibling
    # tract's note when writing this one.
    tag = f"NON-TRACT PARCEL EXCLUSION (automated, tract {tract_no})"
    _write_covenant_note(session, covid, tag, f"{tag}: {reason} ({', '.join(apns)})")

    tract_row = session.execute(
        text("SELECT classified_acreage FROM tract WHERE covid = :covid AND tract_no = :tract_no"),
        {"covid": covid, "tract_no": tract_no},
    ).fetchone()
    return {"excluded_count": deleted, "classified_acreage": float(tract_row.classified_acreage) if tract_row and tract_row.classified_acreage is not None else None}
