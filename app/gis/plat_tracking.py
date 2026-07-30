"""Track when a tract's own raw acreage actually got subdivided into real,
dated lots -- as opposed to when this project's software happened to notice
(parcel_lineage.effective_date, which is stamped with the detection date,
not the plat's own recording date).

Confirmed real (covid 4440, Montgomery): "THE CANOPIES" section 3 (covering
this tract's own matched lot APN 815074) was recorded as a real plat on
2024-08-20, File# 2024082483 -- 15 years after this covenant's own 2009
recording. Montgomery's recorder portal exposes this directly: switching
its Department selector from "Public Records" to "Plats" and searching a
subdivision's base name returns every section's own recording date at once
(app/recorder/adapters/publicsearch.py's search_plats_by_subdivision).

Cost discipline: one live search per DISTINCT subdivision name found among
a tract's own matched parcels (never one per lot, never one per section --
a single search already returns every section), and never re-searched once
this project has an answer (plat.lookup_status handles both "found" and a
real "not_found" -- both are terminal, not retried every run).
"""
import re
from datetime import date, datetime

from sqlalchemy import text

from app.db.repository import insert_source, upsert_plat
from app.gis.plat_parser import normalize_section, parse_plat_reference
from app.queue.job_queue import run_with_job_queue
from app.recorder.adapters import publicsearch
from app.recorder.session import recorder_context


def _parse_slash_date(s: str | None):
    try:
        return datetime.strptime((s or "").strip(), "%m/%d/%Y").date()
    except ValueError:
        return None


def _flag_plat_lookup_note(session, covid: int, tract_no: int, note: str) -> None:
    """Same tagged-note convention as every other stage in this project
    (RECONCILIATION-STAGE, MONITOR-STAGE, CHAIN-OF-TITLE GAP, ...), but
    scoped per-tract in the tag itself (confirmed necessary: covid 4440
    has 2 tracts, each with its own genuinely different unresolved
    parcels -- an unscoped "PLAT LOOKUP (automated): ..." tag would have
    the second tract's own call blindly strip-and-replace the first
    tract's already-written finding, losing it). Only this exact tract's
    own prior note is ever replaced; a sibling tract's own note (or any
    other stage's) is matched up to the next recognized "; TAG (automated"
    boundary, not just the next semicolon, since a single note's own body
    can itself contain "; " between multiple findings."""
    existing = session.execute(
        text("SELECT status, review_reason FROM covenant WHERE covid = :covid"), {"covid": covid},
    ).fetchone()
    reason = existing.review_reason or ""
    reason = re.sub(
        rf";?\s*PLAT LOOKUP \(automated, tract {tract_no}\b[^)]*\):.*?(?=;\s*[A-Z][A-Z ]*\(automated|$)",
        "", reason, flags=re.DOTALL,
    ).strip("; ").strip()
    tagged = f"PLAT LOOKUP (automated, tract {tract_no}, {date.today().isoformat()}): {note}"
    reason = f"{reason}; {tagged}" if reason else tagged
    status = existing.status if existing.status in ("title_in_progress", "done") else "needs_review"
    session.execute(
        text("UPDATE covenant SET status = :status, review_reason = :reason, updated_at = now() WHERE covid = :covid"),
        {"status": status, "reason": reason, "covid": covid},
    )


def resolve_plats_for_tract(session, covid: int, tract_no: int) -> dict:
    """For every matched parcel in this tract not yet tied to a plat: parse
    its recited legal description (platted vs. still-raw abstract tract vs.
    ambiguous), look up any not-yet-searched subdivision's real plat dates
    live (once per subdivision name), and assign parcel.plat_id wherever a
    (subdivision, section) match is found. Never guesses a match -- an
    ambiguous description or an unresolved section is flagged on the
    covenant, not silently skipped or silently assumed."""
    row = session.execute(
        text("""
            SELECT c.county_fips, r.base_url
            FROM tract t JOIN covenant c ON c.covid = t.covid
            JOIN county_recorder_registry r ON r.county_fips = c.county_fips
            WHERE t.covid = :covid AND t.tract_no = :tract_no
        """),
        {"covid": covid, "tract_no": tract_no},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} tract {tract_no}: no county_recorder_registry entry for this county")
    county_fips, base_url = row.county_fips, row.base_url

    # DISTINCT on (county_fips, apn), not a plain join: parcel_covenant carries one row
    # per (apn, run_seq) and a tract can accumulate several run_seq batches over its
    # life (each classify_metes_and_bounds_tract re-run inserts a fresh one, by design
    # -- monitor_run is an audit trail, not a cache) -- a plain join would process, and
    # report stats on, the same real parcel once per historical batch.
    parcels = session.execute(
        text("""
            SELECT DISTINCT p.apn, p.recited_legal_description
            FROM parcel_covenant pc JOIN parcel p ON p.county_fips = pc.county_fips AND p.apn = pc.apn
            WHERE pc.covid = :covid AND pc.tract_no = :tract_no AND p.plat_id IS NULL
              AND p.recited_legal_description IS NOT NULL
        """),
        {"covid": covid, "tract_no": tract_no},
    ).fetchall()

    parsed, ambiguous_apns = {}, []
    for p in parcels:
        ref = parse_plat_reference(p.recited_legal_description)
        if ref is None:
            ambiguous_apns.append(p.apn)
        else:
            parsed[p.apn] = ref

    platted_by_subdivision: dict[str, list[str]] = {}
    for apn, ref in parsed.items():
        if ref.platted:
            platted_by_subdivision.setdefault(ref.subdivision_name, []).append(apn)

    already_known = {
        r.subdivision_name for r in session.execute(
            text("""
                SELECT DISTINCT subdivision_name FROM plat
                WHERE county_fips = :cf AND subdivision_name = ANY(:names)
            """),
            {"cf": county_fips, "names": list(platted_by_subdivision)},
        ).fetchall()
    } if platted_by_subdivision else set()
    to_search = sorted(set(platted_by_subdivision) - already_known)

    plats_found, plats_not_found = 0, 0
    for subdivision_name in to_search:
        def _call(name=subdivision_name):
            with recorder_context() as context:
                return publicsearch.search_plats_by_subdivision(context, base_url, name)
        rows = run_with_job_queue(
            _call, job_type="title_plat_lookup", county_fips=county_fips, covid=covid,
            payload={"base_url": base_url, "subdivision_name": subdivision_name},
        )
        plat_source_id = insert_source(
            session, source_type="recorder_portal", reference=f"{base_url} (plats department)", confidence=None,
        )
        if not rows:
            upsert_plat(
                session, county_fips=county_fips, subdivision_name=subdivision_name, section="",
                lookup_status="not_found", recording_instrument=None, recording_date=None,
                book_volume_page=None, abstract_name=None, source_id=plat_source_id,
            )
            plats_not_found += 1
            continue
        for r in rows:
            upsert_plat(
                session, county_fips=county_fips, subdivision_name=subdivision_name,
                section=normalize_section(r.get("SECTION")), lookup_status="found",
                recording_instrument=r.get("FILE NUMBER") or None,
                recording_date=_parse_slash_date(r.get("RECORDED DATE")),
                book_volume_page=r.get("VOL/BK/PG") or None, abstract_name=r.get("ABSTRACT NAME") or None,
                source_id=plat_source_id,
            )
            plats_found += 1

    assigned, unresolved = 0, []
    for apn, ref in parsed.items():
        if not ref.platted:
            continue
        plat_row = session.execute(
            text("""
                SELECT plat_id FROM plat
                WHERE county_fips = :cf AND subdivision_name = :name AND section = :section
                  AND lookup_status = 'found'
            """),
            {"cf": county_fips, "name": ref.subdivision_name, "section": normalize_section(ref.section)},
        ).fetchone()
        if plat_row is None:
            unresolved.append((apn, ref.subdivision_name, ref.section))
            continue
        session.execute(
            text("UPDATE parcel SET plat_id = :plat_id WHERE county_fips = :cf AND apn = :apn"),
            {"plat_id": plat_row.plat_id, "cf": county_fips, "apn": apn},
        )
        assigned += 1

    notes = []
    if ambiguous_apns:
        shown = ", ".join(ambiguous_apns[:10]) + ("..." if len(ambiguous_apns) > 10 else "")
        notes.append(f"{len(ambiguous_apns)} parcel(s) have a recited legal description this project's plat "
                      f"parser doesn't recognize as either platted or raw ({shown}) -- needs manual review")
    if unresolved:
        detail = "; ".join(f"{apn}: {name!r} sec {section!r}" for apn, name, section in unresolved[:10])
        notes.append(f"{len(unresolved)} parcel(s) recite a platted subdivision/section not found among this "
                      f"project's own plat search results ({detail}) -- needs manual review")
    if notes:
        _flag_plat_lookup_note(session, covid, tract_no, "; ".join(notes))

    return {
        "parcels_considered": len(parcels),
        "platted_parcels_parsed": sum(1 for r in parsed.values() if r.platted),
        "raw_tract_parcels_parsed": sum(1 for r in parsed.values() if not r.platted),
        "ambiguous_parcels": len(ambiguous_apns),
        "subdivisions_searched": len(to_search),
        "plats_found": plats_found,
        "plats_not_found": plats_not_found,
        "parcels_assigned_plat": assigned,
        "unresolved_section_parcels": len(unresolved),
    }


def platting_timeline(session, covid: int, tract_no: int) -> dict:
    """Reconstructs, from already-resolved plat.recording_date facts (no new
    lookups here -- pure deterministic aggregation), how much of the tract
    was platted as of each real plat-recording event, oldest first. Any
    acreage not tied to a 'found' plat (whether it's a still-raw abstract-
    tract parcel, an unmatched residual, or a parcel plat_tracking hasn't
    resolved yet) simply hasn't contributed to "platted" at any date --
    conservative by construction, never assumed platted without a real date."""
    tract_row = session.execute(
        text("SELECT ST_Area(geom::geography) / 4046.8564224 AS acres FROM tract WHERE covid = :covid AND tract_no = :tract_no"),
        {"covid": covid, "tract_no": tract_no},
    ).fetchone()
    if tract_row is None:
        raise RuntimeError(f"covid {covid} tract {tract_no} not found")
    tract_acreage = float(tract_row.acres)

    # distinct_matched_parcels de-duplicates BEFORE joining to plat: parcel_covenant
    # carries one row per (apn, run_seq), and this tract's own history includes
    # several run_seq batches from repeated classify_metes_and_bounds_tract runs --
    # summing p.acreage straight off a plain join would multiply-count the same
    # real parcel's acreage once per historical batch (confirmed real: this
    # produced an obviously-wrong 716-parcel, inflated-acreage first event before
    # this fix, on a subdivision phase that's really ~179 parcels).
    rows = session.execute(
        text("""
            WITH distinct_matched_parcels AS (
                SELECT DISTINCT p.apn, p.acreage, p.plat_id
                FROM parcel p
                JOIN parcel_covenant pc ON pc.county_fips = p.county_fips AND pc.apn = p.apn
                WHERE pc.covid = :covid AND pc.tract_no = :tract_no AND p.plat_id IS NOT NULL
            )
            SELECT pl.recording_date, pl.subdivision_name, pl.section, pl.recording_instrument,
                   SUM(dmp.acreage) AS platted_acreage, count(*) AS n_parcels
            FROM distinct_matched_parcels dmp
            JOIN plat pl ON pl.plat_id = dmp.plat_id
            WHERE pl.recording_date IS NOT NULL
            GROUP BY pl.recording_date, pl.subdivision_name, pl.section, pl.recording_instrument
            ORDER BY pl.recording_date, pl.subdivision_name, pl.section
        """),
        {"covid": covid, "tract_no": tract_no},
    ).fetchall()

    events, cumulative = [], 0.0
    for r in rows:
        cumulative += float(r.platted_acreage or 0)
        events.append({
            "recording_date": str(r.recording_date),
            "subdivision_name": r.subdivision_name,
            "section": r.section,
            "recording_instrument": r.recording_instrument,
            "parcels_platted": r.n_parcels,
            "acreage_this_event": float(r.platted_acreage or 0),
            "cumulative_platted_acreage": cumulative,
            "remaining_raw_acreage": tract_acreage - cumulative,
        })

    return {"tract_acreage": tract_acreage, "events": events}
