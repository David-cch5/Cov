"""Read model for the navigation app. Every function returns plain dicts.

Kept apart from the Flask routes on purpose. CLAUDE.md requires this to stay an
independent system of record with a clean domain boundary so a separate database
can later connect by API -- and an API is a thin wrapper over these functions if
they carry no HTTP in them. It also means the traversal is testable without a
server.

READ-ONLY. Nothing here writes. Navigation must never be the thing that changed a
covenant's state.
"""
from sqlalchemy import text

# A lineage walk is depth-agnostic, so it needs a cycle guard and a hard cap --
# the same shape app/title/payoff.py's find_lineage_ancestors already uses,
# because malformed edge data should degrade to a truncated answer rather than
# hanging the page.
MAX_LINEAGE_DEPTH = 50


def covenant_list(session) -> list[dict]:
    rows = session.execute(text("""
        SELECT c.covid, c.county_fips, co.county_name, co.state_code, c.status,
               c.declarant_raw, c.stated_acreage, c.legal_description_type,
               (SELECT count(*) FROM tract t WHERE t.covid = c.covid) AS tracts,
               (SELECT count(DISTINCT pc.apn) FROM parcel_covenant pc
                 WHERE pc.covid = c.covid) AS parcels,
               (SELECT count(*) FROM tract t
                 WHERE t.covid = c.covid AND t.reconciliation_status = 'reconciled') AS reconciled_tracts,
               (SELECT count(*) FROM covenant_release r
                 WHERE r.covid = c.covid AND r.validity_status = 'valid') AS valid_releases
          FROM covenant c JOIN county co USING (county_fips)
         ORDER BY c.covid
    """)).fetchall()
    return [dict(r._mapping) for r in rows]


def covenant(session, covid: int) -> dict | None:
    row = session.execute(text("""
        SELECT c.*, co.county_name, co.state_code
          FROM covenant c JOIN county co USING (county_fips)
         WHERE c.covid = :covid
    """), {"covid": covid}).fetchone()
    if row is None:
        return None
    out = dict(row._mapping)

    out["tracts"] = [dict(r._mapping) for r in session.execute(text("""
        SELECT t.tract_no, t.boundary_resolution_method, t.reconciliation_status,
               t.stated_acreage, t.classified_acreage, t.unaccounted_acreage,
               t.geom IS NOT NULL AS has_geometry,
               round((ST_Area(t.geom::geography) / 4046.8564224)::numeric, 3) AS geometry_acreage,
               (SELECT count(DISTINCT pc.apn) FROM parcel_covenant pc
                 WHERE pc.covid = t.covid AND pc.tract_no = t.tract_no) AS parcels
          FROM tract t WHERE t.covid = :covid ORDER BY t.tract_no
    """), {"covid": covid})]

    # Every release, valid or not. A pending one asserts nothing (migration 0040)
    # and the page has to show that rather than implying the covenant is over.
    out["releases"] = [dict(r._mapping) for r in session.execute(text("""
        SELECT release_id, release_type, effect, scope, validity_status,
               recording_instrument, recording_date, effective_date, settles_prior_fees,
               rescission_instrument
          FROM covenant_release WHERE covid = :covid ORDER BY recording_date NULLS LAST
    """), {"covid": covid})]

    out["documents"] = [dict(r._mapping) for r in session.execute(text("""
        SELECT relpath, doc_type, pages, ocr_engine, vocab_score
          FROM covenant_document WHERE covid = :covid ORDER BY doc_type, relpath
    """), {"covid": covid})]

    out["beneficiaries"] = [dict(r._mapping) for r in session.execute(text("""
        SELECT b.beneficiary_seq, b.percentage_interest, b.effective_date, ct.name_raw
          FROM covenant_beneficiary b LEFT JOIN contact ct USING (contact_id)
         WHERE b.covid = :covid ORDER BY b.effective_date, b.beneficiary_seq
    """), {"covid": covid})]

    return out


def tract_parcels(session, covid: int, tract_no: int) -> list[dict]:
    """The census for one tract, newest run, PLUS the parcels a human excluded.

    An exclusion does not merely flag a parcel_covenant row -- it removes it, so
    the current census is the post-exclusion set and a LEFT JOIN cannot surface
    what was taken out. Excluded parcels are unioned back in and flagged, because
    a census that silently omits the decisions behind it looks unanimous, and
    seeing why a parcel is NOT counted is half of what navigation is for.
    """
    rows = session.execute(text("""
        WITH current_census AS (
            SELECT pc.county_fips, pc.apn, pc.classification, pc.confidence, pc.rationale,
                   FALSE AS was_excluded, NULL::text AS excluded_reason,
                   NULL::timestamptz AS excluded_at
              FROM parcel_covenant pc
             WHERE pc.covid = :covid AND pc.tract_no = :tract_no
               AND pc.run_seq = (SELECT max(run_seq) FROM parcel_covenant
                                  WHERE covid = :covid AND tract_no = :tract_no)
        ),
        excluded AS (
            SELECT x.county_fips, x.apn, NULL::text AS classification,
                   NULL::numeric AS confidence, NULL::text AS rationale,
                   TRUE AS was_excluded, x.reason AS excluded_reason, x.excluded_at
              FROM parcel_covenant_exclusion x
             WHERE x.covid = :covid AND x.tract_no = :tract_no
               -- Belt and braces: if a future run ever re-admits an excluded
               -- parcel, show it once as a live census row rather than twice.
               AND NOT EXISTS (SELECT 1 FROM current_census c
                                WHERE c.county_fips = x.county_fips AND c.apn = x.apn)
        ),
        combined AS (SELECT * FROM current_census UNION ALL SELECT * FROM excluded)
        SELECT cb.apn, cb.county_fips, cb.classification, cb.confidence, cb.rationale,
               cb.was_excluded, cb.excluded_reason, cb.excluded_at,
               p.owner_name_raw, p.situs_address, p.acreage, p.recited_legal_description,
               p.formed_date, p.formed_by_instrument, p.formation_source, p.geometry_vintage,
               pl.subdivision_name, pl.section,
               (SELECT count(*) FROM parcel_lineage l
                 WHERE l.county_fips = cb.county_fips AND l.apn = cb.apn) AS parent_edges,
               (SELECT count(*) FROM parcel_lineage l
                 WHERE l.parent_county_fips = cb.county_fips AND l.parent_apn = cb.apn) AS child_edges,
               (SELECT count(*) FROM parcel_history h
                 WHERE h.county_fips = cb.county_fips AND h.apn = cb.apn) AS history_rows
          FROM combined cb
          JOIN parcel p ON p.county_fips = cb.county_fips AND p.apn = cb.apn
          LEFT JOIN plat pl ON pl.plat_id = p.plat_id
         ORDER BY cb.was_excluded, p.formed_date NULLS LAST,
                  pl.subdivision_name, pl.section, cb.apn
    """), {"covid": covid, "tract_no": tract_no}).fetchall()
    return [dict(r._mapping) for r in rows]


def parcel(session, county_fips: str, apn: str) -> dict | None:
    row = session.execute(text("""
        SELECT p.*, co.county_name, co.state_code,
               pl.subdivision_name, pl.section, pl.book_volume_page, pl.abstract_name,
               pl.recording_date AS plat_recording_date,
               pl.recording_instrument AS plat_recording_instrument,
               round((ST_Area(p.geom::geography) * 10.763910417)::numeric, 0) AS geometry_sqft
          FROM parcel p JOIN county co USING (county_fips)
          LEFT JOIN plat pl ON pl.plat_id = p.plat_id
         WHERE p.county_fips = :cf AND p.apn = :apn
    """), {"cf": county_fips, "apn": apn}).fetchone()
    if row is None:
        return None
    out = dict(row._mapping)
    out.pop("geom", None)   # never ship geometry blobs through the page

    # BACK TO THE COVENANT. The return leg BUILD_SPEC asks for: from any lot,
    # every covenant that encumbers it, and through which tract.
    #
    # Unions in covenants this parcel was EXCLUDED from, for the same reason
    # tract_parcels does: an exclusion deletes the parcel_covenant row, so
    # without this a parcel shows no relationship at all to a covenant somebody
    # deliberately considered and ruled out. "Never considered" and "considered
    # and excluded, here is why" are different answers.
    out["covenants"] = [dict(r._mapping) for r in session.execute(text("""
        WITH live AS (
            SELECT DISTINCT pc.covid, pc.tract_no, pc.classification, pc.confidence,
                   FALSE AS was_excluded, NULL::text AS excluded_reason
              FROM parcel_covenant pc
             WHERE pc.county_fips = :cf AND pc.apn = :apn
               AND pc.run_seq = (SELECT max(run_seq) FROM parcel_covenant
                                  WHERE covid = pc.covid AND tract_no = pc.tract_no)
        ),
        excluded AS (
            SELECT DISTINCT x.covid, x.tract_no, NULL::text AS classification,
                   NULL::numeric AS confidence, TRUE AS was_excluded, x.reason AS excluded_reason
              FROM parcel_covenant_exclusion x
             WHERE x.county_fips = :cf AND x.apn = :apn
               AND NOT EXISTS (SELECT 1 FROM live l
                                WHERE l.covid = x.covid AND l.tract_no = x.tract_no)
        ),
        combined AS (SELECT * FROM live UNION ALL SELECT * FROM excluded)
        SELECT cb.covid, cb.tract_no, cb.classification, cb.confidence,
               cb.was_excluded, cb.excluded_reason,
               c.status, c.declarant_raw, c.stated_acreage
          FROM combined cb JOIN covenant c USING (covid)
         ORDER BY cb.was_excluded, cb.covid, cb.tract_no
    """), {"cf": county_fips, "apn": apn})]

    out["history"] = [dict(r._mapping) for r in session.execute(text("""
        SELECT h.captured_at, h.effective_date, h.instrument, h.change_reason,
               h.owner_name_raw, h.acreage,
               round((ST_Area(h.geom::geography) * 10.763910417)::numeric, 0) AS geometry_sqft,
               s.reference AS source_reference
          FROM parcel_history h LEFT JOIN source s USING (source_id)
         WHERE h.county_fips = :cf AND h.apn = :apn
         ORDER BY COALESCE(h.effective_date, h.captured_at::date) DESC, h.captured_at DESC
    """), {"cf": county_fips, "apn": apn})]

    # transfer's real column names, read from the schema rather than assumed:
    # instrument_number (not recording_instrument), grantor/grantee are contact_id
    # FKs (not raw text), and superseded_at marks a row a later re-walk replaced.
    out["transfers"] = [dict(r._mapping) for r in session.execute(text("""
        SELECT t.recording_date, t.instrument_number, t.instrument_type,
               t.consideration_amount, t.exemption_category, t.exemption_basis,
               t.review_flag, t.superseded_at, t.covid, t.tract_no,
               gr.name_raw AS grantor_raw, ge.name_raw AS grantee_raw
          FROM transfer t
          LEFT JOIN contact gr ON gr.contact_id = t.grantor_contact_id
          LEFT JOIN contact ge ON ge.contact_id = t.grantee_contact_id
         WHERE t.parcel_county_fips = :cf AND t.parcel_apn = :apn
         ORDER BY t.recording_date DESC NULLS LAST, t.instrument_number LIMIT 200
    """), {"cf": county_fips, "apn": apn})]

    out["ancestors"] = lineage_walk(session, county_fips, apn, direction="up")
    out["descendants"] = lineage_walk(session, county_fips, apn, direction="down")
    return out


def lineage_walk(session, county_fips: str, apn: str, direction: str = "up") -> list[dict]:
    """Walk parcel_lineage to ANY depth, up (ancestors) or down (descendants).

    Depth-agnostic by design: it returns one generation or six with no change
    here, so the traversal BUILD_SPEC asks for -- tract to child to child's child
    down to the current lot, and back -- deepens on its own as edges are recorded
    rather than needing this rewritten.

    Right now it returns NOTHING for every parcel, because parcel_lineage holds
    zero rows: the table and its two consumers (payoff.py's ancestor walk,
    monitor.py's replat detection) were built, and nothing ever wrote an edge.
    That is reported on the page as an absence of recorded evidence rather than
    papered over -- a parent APN cannot be derived from a single current parcel
    snapshot, and inventing one would fabricate title data.
    """
    if direction not in ("up", "down"):
        raise ValueError(f"direction must be 'up' or 'down', got {direction!r}")

    # Each column carries its OWN alias. Aliasing a comma-separated pair as one
    # ("l.parent_county_fips, l.parent_apn AS next_county_fips, next_apn") names
    # only the last one and leaves the other a bare reference -- which Postgres
    # rejects, and which is how this failed first time.
    if direction == "up":
        seed_cols = "l.parent_county_fips AS next_county_fips, l.parent_apn AS next_apn"
        seed_where = "l.county_fips = :cf AND l.apn = :apn"
        recur_cols = "pl.parent_county_fips, pl.parent_apn"
        recur_join = "pl.county_fips = w.next_county_fips AND pl.apn = w.next_apn"
    else:
        seed_cols = "l.county_fips AS next_county_fips, l.apn AS next_apn"
        seed_where = "l.parent_county_fips = :cf AND l.parent_apn = :apn"
        recur_cols = "pl.county_fips, pl.apn"
        recur_join = "pl.parent_county_fips = w.next_county_fips AND pl.parent_apn = w.next_apn"

    rows = session.execute(text(f"""
        WITH RECURSIVE walk AS (
            SELECT {seed_cols}, l.lineage_type, l.split_instrument_number,
                   l.effective_date, 1 AS depth,
                   ARRAY[:cf || ':' || :apn] AS visited
              FROM parcel_lineage l WHERE {seed_where}
            UNION ALL
            SELECT {recur_cols}, pl.lineage_type, pl.split_instrument_number,
                   pl.effective_date, w.depth + 1,
                   w.visited || (w.next_county_fips || ':' || w.next_apn)
              FROM parcel_lineage pl JOIN walk w ON {recur_join}
             WHERE NOT (w.next_county_fips || ':' || w.next_apn) = ANY(w.visited)
               AND w.depth < {MAX_LINEAGE_DEPTH}
        )
        SELECT w.next_county_fips AS county_fips, w.next_apn AS apn, w.lineage_type,
               w.split_instrument_number, w.effective_date, min(w.depth) AS depth,
               p.owner_name_raw, p.acreage, p.recited_legal_description, p.formed_date
          FROM walk w
          LEFT JOIN parcel p ON p.county_fips = w.next_county_fips AND p.apn = w.next_apn
         GROUP BY 1, 2, 3, 4, 5, p.owner_name_raw, p.acreage,
                  p.recited_legal_description, p.formed_date
         ORDER BY depth, apn
    """), {"cf": county_fips, "apn": apn}).fetchall()
    return [dict(r._mapping) for r in rows]


def search(session, q: str, limit: int = 50) -> dict:
    """One box for the three things worth jumping to: a covid, an APN, or an
    owner/declarant name."""
    like = f"%{q.strip().upper()}%"
    covenants = [dict(r._mapping) for r in session.execute(text("""
        SELECT c.covid, c.status, c.declarant_raw, co.county_name
          FROM covenant c JOIN county co USING (county_fips)
         WHERE CAST(c.covid AS text) = :exact
            OR upper(coalesce(c.declarant_raw, '')) LIKE :like
         ORDER BY c.covid LIMIT :limit
    """), {"exact": q.strip(), "like": like, "limit": limit})]
    parcels = [dict(r._mapping) for r in session.execute(text("""
        SELECT p.county_fips, p.apn, p.owner_name_raw, p.situs_address, co.county_name
          FROM parcel p JOIN county co USING (county_fips)
         WHERE p.apn = :exact
            OR upper(coalesce(p.owner_name_raw, '')) LIKE :like
            OR upper(coalesce(p.situs_address, '')) LIKE :like
         ORDER BY p.county_fips, p.apn LIMIT :limit
    """), {"exact": q.strip(), "like": like, "limit": limit})]
    return {"covenants": covenants, "parcels": parcels}


def lineage_coverage(session) -> dict:
    """How much lineage evidence actually exists. Shown on the home page because
    an empty traversal must read as a known gap, not as a parcel with no history."""
    row = session.execute(text("""
        SELECT (SELECT count(*) FROM parcel_lineage) AS edges,
               (SELECT count(*) FROM parcel WHERE formed_date IS NOT NULL) AS parcels_formed,
               (SELECT count(*) FROM parcel) AS parcels_total,
               (SELECT count(*) FROM parcel_history) AS history_rows,
               (SELECT count(*) FROM parcel_history WHERE effective_date IS NOT NULL) AS history_dated
    """)).fetchone()
    return dict(row._mapping)
