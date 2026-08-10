"""Reconciliation (BUILD_SPEC.md / CLAUDE.md's own non-negotiable): "Every covenant
passes a reconciliation check before it is considered done: classified acreage must
reconcile with the covenant's stated acreage, and any unaccounted area inside the
footprint is flagged." This is the step that actually populates tract.reconciliation_
status / tract.unaccounted_acreage -- both columns have existed since migration
0001_initial_schema, but nothing wrote to them until now (confirmed real: covid 2497
and covid 4955 both already carry a manually-set reconciliation_status from earlier
work in this project, before this module existed -- this module's own results
reproduce both by the same rule, not a coincidence).

'current_parcel_match' tracts (subdivision-plat or Texas-abstract legal descriptions,
resolved by app/gis/classifier.py's resolve_subdivision_plat_tract) are reconciled by
comparing classified_acreage against the TRACT's own stated_acreage (migration 0036),
falling back to covenant.stated_acreage only where the covenant has a single tract and
the two therefore describe the same land -- that
resolution method's tract.geom is BY CONSTRUCTION the union of whatever parcels were
matched, so there's no INDEPENDENT footprint to diff against and residual_geom stays
NULL for them.

'metes_and_bounds_traverse' tracts (resolved by classifier.py's
classify_metes_and_bounds_tract) get a genuinely stronger check: tract.geom there is an
independently-derived polygon (from the deed's own courses/distances, anchored to a
real surveyed tie point -- see app/gis/state_plane_anchor.py), so residual_geom
(ST_Difference against every spatially-matched parcel's union) is a real geometric
fact, not a derived non-signal. unaccounted_acreage comes directly from that residual's
own area, not from a stated_acreage comparison -- there's no "over_classified" concept
here (a residual can't represent more area than the tract itself, unlike a set of
matched parcels that might). A metes_and_bounds_traverse tract with no parcel_covenant
rows yet (classification not yet run) is reported not-checkable, same as before.
"""
from datetime import date

from sqlalchemy import text

from app.db.review_notes import merge_tagged_note

# Matched acreage rarely sums to the covenant's stated acreage EXACTLY even when every
# real lot was found (each parcel's own recorded acreage is itself already rounded,
# e.g. "0.82 AM/L") -- but any discrepancy beyond simple float/rounding noise is a real
# signal, not something to tolerate away. Confirmed by this project's own two prior,
# manually-reconciled cases: covid 2497's 0.082-acre (18%) overage was flagged
# 'over_classified', and covid 4955's 1.401-acre (12%) shortfall was flagged
# 'unaccounted_area' -- neither was waved through as "close enough."
RECONCILIATION_TOLERANCE_ACRES = 0.01


def reconcile_tract(session, covid: int, tract_no: int = 1) -> dict:
    """One tract's reconciliation check. Writes tract.reconciliation_status /
    unaccounted_acreage when the tract's boundary method supports a real
    check; otherwise returns why not, without touching the row (a tract
    that can't be reconciled yet stays 'pending', its own honest default,
    rather than being marked anything else)."""
    row = session.execute(
        text("""
            SELECT t.boundary_resolution_method, t.classified_acreage,
                   -- A tract's OWN stated acreage. covenant.stated_acreage covers
                   -- every tract together, so it is only this tract's figure when
                   -- the covenant has exactly one tract; see tract_count below.
                   t.stated_acreage AS tract_stated_acreage,
                   c.stated_acreage AS covenant_stated_acreage,
                   (SELECT count(*) FROM tract t2 WHERE t2.covid = t.covid) AS tract_count,
                   ST_Area(t.residual_geom::geography) / 4046.8564224 AS residual_acreage,
                   EXISTS(SELECT 1 FROM parcel_covenant pc WHERE pc.covid = t.covid AND pc.tract_no = t.tract_no)
                       AS has_parcel_census
            FROM tract t JOIN covenant c ON c.covid = t.covid
            WHERE t.covid = :covid AND t.tract_no = :tract_no
        """), {"covid": covid, "tract_no": tract_no},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} tract {tract_no} not found")

    if row.boundary_resolution_method is None:
        return {"checked": False, "reason": "tract boundary not yet confirmed (a rough geocode-approximate "
                                             "placement, not a real boundary) -- nothing to reconcile against"}
    if row.boundary_resolution_method not in ("current_parcel_match", "metes_and_bounds_traverse"):
        return {"checked": False, "reason": f"{row.boundary_resolution_method!r} is not a recognized, "
                                             f"reconcilable boundary_resolution_method"}
    if row.boundary_resolution_method == "metes_and_bounds_traverse" and not row.has_parcel_census:
        return {"checked": False, "reason": f"{row.boundary_resolution_method!r} tracts need their interior "
                                             f"parcels spatially classified against tract.geom first (via "
                                             f"classifier.classify_metes_and_bounds_tract) before there's "
                                             f"anything to reconcile"}

    classified = row.classified_acreage

    # The tract's OWN stated acreage. A covenant-level figure is only this tract's
    # when the covenant has a single tract; using it on a multi-tract covenant
    # measures one tract against all of them. Confirmed real on covid 5838, whose
    # 33.5 ac tract 2 was compared against tract 1's 318.779 ac and reported
    # 285.261 ac "unaccounted" -- 850% of the tract, on land fully accounted for.
    stated = row.tract_stated_acreage
    if stated is None and row.tract_count == 1:
        stated = row.covenant_stated_acreage

    if row.boundary_resolution_method == "metes_and_bounds_traverse":
        # A real, independent geometric fact (see module docstring) -- not a comparison
        # against stated_acreage, so a missing/wrong stated_acreage extraction can't mask
        # or manufacture a discrepancy here. No 'over_classified': a residual can never
        # represent more area than the tract's own polygon.
        unaccounted = float(row.residual_acreage or 0)
        if unaccounted <= RECONCILIATION_TOLERANCE_ACRES:
            status, note = "reconciled", None
        else:
            status = "unaccounted_area"
            note = (f"{unaccounted:.3f} ac of this metes-and-bounds tract's own surveyed polygon is not "
                    f"covered by any spatially-matched parcel ({classified} ac classified) -- may be "
                    f"unplatted remainder, road ROW, or a missing parcel match, needs human review")
    elif stated is None and row.tract_count > 1:
        # Nothing legitimate to check against: this tract's own acreage has not
        # been read, and its covenant's figure covers every tract together.
        # Reported not-checkable rather than reconciled against a number that
        # isn't this tract's -- and any value a previous run wrote under the old
        # covenant-wide comparison is CLEARED, because leaving a known-wrong
        # figure in place is worse than leaving the column empty.
        session.execute(
            text("""
                UPDATE tract SET reconciliation_status = 'pending', unaccounted_acreage = NULL,
                                 updated_at = now()
                WHERE covid = :covid AND tract_no = :tract_no
                  AND (reconciliation_status IS DISTINCT FROM 'pending'
                       OR unaccounted_acreage IS NOT NULL)
            """), {"covid": covid, "tract_no": tract_no},
        )
        return {"checked": False,
                "reason": (f"covid {covid} has {row.tract_count} tracts, so the covenant's own "
                           f"stated acreage is not this tract's -- set tract.stated_acreage from "
                           f"tract {tract_no}'s own deed text to reconcile it")}
    elif classified is None:
        # A current_parcel_match tract whose parcel census has not been written
        # yet has nothing to compare. Reported not-checkable rather than crashing
        # on float(None) -- confirmed real on covid 5839 tract 2, freshly created
        # from a parcel match before its census row existed.
        return {"checked": False,
                "reason": (f"covid {covid} tract {tract_no} has no classified_acreage yet -- "
                           f"its parcel census must be written before it can be reconciled")}
    elif stated is None:
        status = "reconciled"
        unaccounted = None
        note = None
    else:
        diff = float(stated) - float(classified)
        # stored as a magnitude, not signed -- matches this project's own pre-existing,
        # manually-set precedent (covid 2497's over_classified row already carries a
        # positive unaccounted_acreage); reconciliation_status is what carries direction.
        unaccounted = abs(diff)
        if unaccounted <= RECONCILIATION_TOLERANCE_ACRES:
            status, note = "reconciled", None
        elif diff > 0:
            status = "unaccounted_area"
            note = (f"{unaccounted:.3f} ac of this tract's stated {stated} ac isn't accounted for by any "
                    f"matched parcel ({classified} ac classified) -- a lot may be missing from this walk's "
                    f"matches, needs human review")
        else:
            status = "over_classified"
            note = (f"matched parcels total {classified} ac, {unaccounted:.3f} ac more than this tract's "
                    f"own stated {stated} ac -- may include a parcel that doesn't actually belong "
                    f"to this covenant, needs human review")

    session.execute(
        text("""
            UPDATE tract SET reconciliation_status = :status, unaccounted_acreage = :unaccounted, updated_at = now()
            WHERE covid = :covid AND tract_no = :tract_no
        """),
        {"status": status, "unaccounted": unaccounted, "covid": covid, "tract_no": tract_no},
    )
    return {"checked": True, "status": status, "unaccounted_acreage": unaccounted, "note": note}


def reconcile_covenant(session, covid: int) -> dict:
    """Every tract for this covid, then advances covenant.status the same
    way every other stage in this project does: never regress a status
    that's already past this stage (title_in_progress/done), never
    silently clear an unrelated stage's own still-open concern (its tagged
    note in review_reason, e.g. INGESTION-STAGE, survives untouched), and
    only actually advance to 'reconciled' when reconciliation itself is
    clean AND review_reason is otherwise empty."""
    tract_rows = session.execute(
        text("SELECT tract_no FROM tract WHERE covid = :covid ORDER BY tract_no"), {"covid": covid},
    ).fetchall()
    if not tract_rows:
        raise RuntimeError(f"covid {covid} has no tract rows to reconcile")

    results = {t.tract_no: reconcile_tract(session, covid, t.tract_no) for t in tract_rows}
    problems = {tn: r for tn, r in results.items() if r["checked"] and r["status"] != "reconciled"}
    not_yet_checkable = {tn: r for tn, r in results.items() if not r["checked"]}

    existing = session.execute(
        text("SELECT status, review_reason FROM covenant WHERE covid = :covid"), {"covid": covid},
    ).fetchone()
    # Only this stage's own tagged section is ever replaced; every other stage's
    # note in review_reason is left alone. See app/db/review_notes.py for why
    # this is one shared helper rather than a regex per stage -- a bare `.*$`
    # here really did swallow a later NON-TRACT PARCEL EXCLUSION note on covid
    # 8534, and an (?:automated|manual)-only boundary would have deleted covid
    # 2497's hand-written RE-VERIFIED (2026-07-24) note.
    note = None
    if problems:
        # " | " between tracts, not "; " -- see review_notes.py's note-author
        # constraint (a body must not look like a note boundary).
        detail = " | ".join(f"tract {tn}: {r['note']}" for tn, r in problems.items())
        note = f"RECONCILIATION-STAGE (automated, {date.today().isoformat()}): {detail}"
    reason = merge_tagged_note(existing.review_reason, "RECONCILIATION-STAGE", note)

    _DO_NOT_REGRESS = {"title_in_progress", "done"}
    if problems or reason:
        # either reconciliation itself found a problem, or some OTHER stage's own
        # still-open concern remains in review_reason -- either way, this covenant is
        # not clean, and that must not be silently overridden by a good reconciliation
        # result alone.
        final_status = existing.status if existing.status in _DO_NOT_REGRESS else "needs_review"
    elif not_yet_checkable:
        # nothing wrong, but not every tract could actually be checked yet -- leave
        # status where it is rather than claiming a reconciliation that didn't fully happen.
        final_status = existing.status
    else:
        final_status = existing.status if existing.status in _DO_NOT_REGRESS else "reconciled"

    session.execute(
        text("UPDATE covenant SET status = :status, review_reason = :reason, updated_at = now() WHERE covid = :covid"),
        {"status": final_status, "reason": reason or None, "covid": covid},
    )
    return {"covid": covid, "tract_results": results, "final_status": final_status}
