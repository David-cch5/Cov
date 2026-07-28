"""Reconciliation (BUILD_SPEC.md / CLAUDE.md's own non-negotiable): "Every covenant
passes a reconciliation check before it is considered done: classified acreage must
reconcile with the covenant's stated acreage, and any unaccounted area inside the
footprint is flagged." This is the step that actually populates tract.reconciliation_
status / tract.unaccounted_acreage -- both columns have existed since migration
0001_initial_schema, but nothing wrote to them until now (confirmed real: covid 2497
and covid 4955 both already carry a manually-set reconciliation_status from earlier
work in this project, before this module existed -- this module's own results
reproduce both by the same rule, not a coincidence).

Only 'current_parcel_match' tracts (subdivision-plat or Texas-abstract legal
descriptions, resolved by app/gis/classifier.py's resolve_subdivision_plat_tract) are
reconciled here. That resolution method's own tract.geom is BY CONSTRUCTION the union
of whatever parcels were matched -- there's no INDEPENDENT footprint to compute a
geometric residual against, so residual_geom stays NULL and the only real signal is
classified_acreage vs. the covenant's own stated_acreage. A genuinely independent,
geometry-first residual check (ST_Difference against a metes-and-bounds-derived
polygon, populating residual_geom for real) needs a metes-and-bounds tract's interior
parcels to be spatially classified against tract.geom first -- confirmed real: every
metes_and_bounds_traverse tract in this project currently has ZERO parcel_covenant
rows, meaning that spatial-first parcel census (CLAUDE.md's own non-negotiable) hasn't
actually been built yet for this resolution method. Deliberately not attempted here --
a real, separate, larger piece of work, not something to fold into reconciliation.
"""
import re
from datetime import date

from sqlalchemy import text

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
            SELECT t.boundary_resolution_method, t.classified_acreage, c.stated_acreage
            FROM tract t JOIN covenant c ON c.covid = t.covid
            WHERE t.covid = :covid AND t.tract_no = :tract_no
        """), {"covid": covid, "tract_no": tract_no},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} tract {tract_no} not found")

    if row.boundary_resolution_method is None:
        return {"checked": False, "reason": "tract boundary not yet confirmed (a rough geocode-approximate "
                                             "placement, not a real boundary) -- nothing to reconcile against"}
    if row.boundary_resolution_method != "current_parcel_match":
        return {"checked": False, "reason": f"{row.boundary_resolution_method!r} tracts need their interior "
                                             f"parcels spatially classified against tract.geom first (a real, "
                                             f"separate, not-yet-built capability) before there's anything to "
                                             f"reconcile"}

    classified = row.classified_acreage
    stated = row.stated_acreage
    if stated is None:
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
            note = (f"{unaccounted:.3f} ac of the covenant's stated {stated} ac isn't accounted for by any "
                    f"matched parcel ({classified} ac classified) -- a lot may be missing from this walk's "
                    f"matches, needs human review")
        else:
            status = "over_classified"
            note = (f"matched parcels total {classified} ac, {unaccounted:.3f} ac more than the "
                    f"covenant's own stated {stated} ac -- may include a parcel that doesn't actually belong "
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
    reason = existing.review_reason or ""
    # same tagged-note pattern as app/title/chain.py's _update_covenant_gap_notes and
    # scripts/ingest_probe.py's _merge_ingestion_note: only this stage's own tagged
    # section is ever replaced, everything else in review_reason is left alone.
    reason = re.sub(r";?\s*RECONCILIATION-STAGE \(automated[^)]*\):.*$", "", reason).strip("; ").strip()

    if problems:
        detail = "; ".join(f"tract {tn}: {r['note']}" for tn, r in problems.items())
        note = f"RECONCILIATION-STAGE (automated, {date.today().isoformat()}): {detail}"
        reason = f"{reason}; {note}" if reason else note

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
