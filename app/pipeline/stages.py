"""The stages a covenant passes through, and what each one decides.

A stage is a function of (session, covid, payload) returning a StageVerdict. It
does the work and reports; it never enqueues anything and never touches job
rows. The runner owns the queue, so a stage can be called directly -- which is
how they are tested, and how a human re-runs one by hand -- without a queue in
the picture at all.

THREE VERDICTS, and the middle one is the point:

  advanced     the work is done and the next stage can run.
  needs_review the work ran correctly and reached a question only a human can
               answer. NOT a failure. The chain stops here, with the reason
               recorded, and resumes when somebody answers -- which is why every
               stage is re-runnable.
  retry        something outside this covenant is temporarily unavailable. Not a
               conclusion about the covenant at all. Comes back later with the
               tier still available, rather than falling through to a more
               expensive one -- see app/gis/ngs.py's NgsUnanswered for the case
               that made this a separate verdict rather than a kind of failure.

Anything unexpected raises, and the runner turns it into a job_queue error with
backoff. Stages do not swallow exceptions to look tidy.

STAGE ORDER, and what is deliberately not in it yet. The encumbered land has to
be right before anything is built on top of it, so this pass runs
intake -> resolve_tract -> classify_parcels -> reconcile and stops.
chain_of_title and fee_compute are registered here, and are NOT auto-enqueued;
running them on a covenant whose encumbered land is unconfirmed would spend
recorder-portal and deed-reading effort against the wrong parcels.

EVERY STAGE MUST BE IDEMPOTENT. app/queue/queue.py's docstring explains why: a
crash between "work committed" and "job marked done" leaves the job to be
reclaimed and re-run, so a stage that cannot be run twice safely cannot be
queued. Each one below either upserts or is a pure read-and-record.
"""
import os
from dataclasses import dataclass, field

from sqlalchemy import text

from app.db.review_notes import merge_tagged_note
from app.gis.ngs import NgsUnanswered

# Ordered. The runner advances along this list; a stage that is not the last one
# names its successor implicitly by position, so adding a stage means adding it
# here and nowhere else.
STAGE_ORDER = ("intake", "resolve_tract", "classify_parcels", "reconcile")

# Registered so they can be enqueued and run deliberately, but never reached by
# advancing: the encumbered land comes first. See the module docstring.
MANUAL_STAGES = ("chain_of_title", "fee_compute")

JOB_TYPE_PREFIX = "pipeline_"


def job_type(stage: str) -> str:
    return f"{JOB_TYPE_PREFIX}{stage}"


def stage_from_job_type(jt: str) -> str:
    return jt[len(JOB_TYPE_PREFIX):] if jt.startswith(JOB_TYPE_PREFIX) else jt


@dataclass
class StageVerdict:
    status: str                        # 'advanced' | 'needs_review' | 'retry'
    note: str
    covid: int | None = None           # intake discovers it; later stages are given it
    detail: dict = field(default_factory=dict)
    # Set when a stage knows the next one is pointless -- e.g. a subdivision-plat
    # tract already has its parcels, so classify_parcels has nothing to do.
    skip_stages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in ("advanced", "needs_review", "retry"):
            raise ValueError(f"unknown stage status {self.status!r}")


def _record_stage_note(session, covid: int, stage: str, note: str | None) -> None:
    """Keep each stage's own note in covenant.review_reason under its own tag.

    Same mechanism ingestion and chain-of-title already use, and for the same
    hard-won reason: a bare overwrite here once wiped covid 2497's manual
    acreage-reconciliation note. Only this stage's tag is replaced; everything
    else in review_reason survives.
    """
    tag = f"{stage.upper()}-STAGE"
    existing = session.execute(
        text("SELECT review_reason FROM covenant WHERE covid = :c"), {"c": covid},
    ).scalar()
    merged = merge_tagged_note(existing, tag, f"{tag} (automated): {note}" if note else None)
    session.execute(
        text("UPDATE covenant SET review_reason = :r, updated_at = now() WHERE covid = :c"),
        {"r": merged or None, "c": covid},
    )


# --------------------------------------------------------------------------
# intake
# --------------------------------------------------------------------------

def run_intake(session, covid: int | None, payload: dict) -> StageVerdict:
    """A dropped file becomes a covenant row.

    The only stage that takes a path instead of a covid, and the only one that
    can create a covid -- job_queue.covid is a FK to covenant, so an intake job
    necessarily carries covid NULL and its subject in payload.
    """
    from app.ingestion.ingest import escalate_ocr_confidence, ingest_one
    from app.ingestion.intake import candidate_for_dropped_file

    path = payload.get("path")
    if not path:
        raise ValueError("intake job has no 'path' in its payload")
    if not os.path.exists(path):
        return StageVerdict("needs_review", f"dropped file no longer exists: {path}")

    built = candidate_for_dropped_file(session, path)
    candidate = built["candidate"]
    notes = list(built["notes"])

    # Free tier could not read it -> the paid tier is the documented next step,
    # and the yield gate has already decided that rather than a vocabulary score.
    if candidate.text_usable is False:
        escalate_ocr_confidence([candidate])
        notes.append("escalated to vision OCR after the free tier came up short")

    if candidate.county_fips is None:
        # Nothing can be recorded without it -- covenant.county_fips is NOT NULL
        # and every downstream table is keyed on it. The candidate's own reason
        # already names the work required.
        return StageVerdict("needs_review", candidate.review_reason or "county unresolved",
                            covid=candidate.covid, detail={"notes": notes})

    ingest_one(session, candidate)
    _record_stage_note(session, candidate.covid, "intake",
                       None if not candidate.needs_review else candidate.review_reason)
    if candidate.needs_review:
        return StageVerdict("needs_review", candidate.review_reason or "flagged during ingestion",
                            covid=candidate.covid, detail={"notes": notes})
    return StageVerdict("advanced", "; ".join(notes), covid=candidate.covid,
                        detail={"notes": notes})


# --------------------------------------------------------------------------
# resolve_tract
# --------------------------------------------------------------------------

def run_resolve_tract(session, covid: int, payload: dict) -> StageVerdict:
    """Establish the encumbered land's boundary, by whichever route the deed's
    own legal description calls for.

    The branch is the deed's legal_description_type, not a guess: a
    subdivision-plat covenant names platted lots and resolves by matching them
    in the parcel fabric, while a metes-and-bounds covenant has to be anchored
    and walked. They also differ downstream -- a plat resolution produces the
    parcel census as a side effect, so classify_parcels has nothing left to do,
    while a traverse produces only a polygon and the census is a separate step.
    """
    from app.gis.classifier import classify_metes_and_bounds_tract, resolve_subdivision_plat_tract  # noqa: F401
    from app.gis.anchor_resolver import resolve_metes_and_bounds_anchor

    row = session.execute(
        text("SELECT legal_description_type, county_fips FROM covenant WHERE covid = :c"),
        {"c": covid},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} not found")

    kind = row.legal_description_type
    if kind == "subdivision_plat":
        # Raises rather than returning a verdict when it cannot resolve at all
        # (no parsed legal description, no GIS registry entry); the runner turns
        # that into a retryable job_queue error, which is right for a GIS
        # endpoint being down and visible for anything else.
        result = resolve_subdivision_plat_tract(session, covid)
        matched = result.get("matched_parcels") or 0
        missing = result.get("missing_lots") or []
        if not matched:
            note = (f"no parcels matched this covenant's platted lots "
                    f"({result.get('requested_lots', '?')} requested)")
            _record_stage_note(session, covid, "resolve_tract", note)
            return StageVerdict("needs_review", note, covid=covid, detail=result)
        if missing:
            # A partial match is exactly the case CLAUDE.md's name-first warning
            # is about -- a name pass once missed 2,500+ lots. Report the gap
            # rather than advancing on the lots that happened to match.
            note = (f"{matched} parcels matched but {len(missing)} requested lot(s) did not: "
                    f"{', '.join(str(m) for m in missing[:12])}"
                    f"{' ...' if len(missing) > 12 else ''}")
            _record_stage_note(session, covid, "resolve_tract", note)
            return StageVerdict("needs_review", note, covid=covid, detail=result)
        _record_stage_note(session, covid, "resolve_tract", None)
        # Matching the platted lots IS the census -- resolve_subdivision_plat_tract
        # writes parcel_covenant rows itself -- so classify_parcels, which
        # intersects a traverse polygon, has nothing to add here.
        return StageVerdict("advanced", f"{matched} platted lots matched", covid=covid,
                            detail=result, skip_stages=("classify_parcels",))

    if kind in ("metes_bounds", "texas_abstract"):
        try:
            result = resolve_metes_and_bounds_anchor(session, covid)
        except NgsUnanswered as e:
            # A free, published, survey-grade answer exists and NGS did not hand
            # it over. Coming back later keeps that tier; carrying on would buy
            # the same answer from the paid tiers. See app/gis/ngs.py.
            return StageVerdict("retry", f"NGS unavailable, tier preserved: {e}", covid=covid)

        tier = result.get("tier")
        if tier == "skipped_released":
            # Historic: worth recording, not worth researching. Not review either
            # -- there is no question outstanding.
            _record_stage_note(session, covid, "resolve_tract", result.get("reason"))
            return StageVerdict("needs_review", result.get("reason") or "covenant fully released",
                                covid=covid, detail=result)
        if not result.get("committed"):
            note = result.get("reason") or f"no tier could anchor this tract (last: {tier})"
            _record_stage_note(session, covid, "resolve_tract", note)
            return StageVerdict("needs_review", note, covid=covid, detail=result)
        _record_stage_note(session, covid, "resolve_tract", None)
        return StageVerdict("advanced", f"anchored via {tier}", covid=covid, detail=result)

    note = (f"legal_description_type {kind!r} has no boundary-resolution route -- "
            f"expected subdivision_plat, metes_bounds or texas_abstract")
    _record_stage_note(session, covid, "resolve_tract", note)
    return StageVerdict("needs_review", note, covid=covid)


# --------------------------------------------------------------------------
# classify_parcels
# --------------------------------------------------------------------------

def run_classify_parcels(session, covid: int, payload: dict) -> StageVerdict:
    """Spatial-first parcel census inside the tract polygon.

    Never reports a raw match count as an answer. CLAUDE.md's non-negotiable
    and a confirmed real error: on covid 8534 tract 1, 254 matched parcels were
    actually 214, the other 40 belonging to two subdivisions the deed never
    conveys. So when the classifier flags that signature, this halts for the
    human call rather than advancing on an inflated census.
    """
    from app.gis.classifier import classify_metes_and_bounds_tract

    result = classify_metes_and_bounds_tract(session, covid)
    flagged = result.get("possible_non_tract_subdivisions") or []
    if flagged:
        note = (f"{result.get('matched_parcels', '?')} parcels matched, but "
                f"{len(flagged)} whole subdivision(s) are entirely low-overlap "
                f"({', '.join(str(f) for f in flagged)}) -- check them against the deed's own "
                f"text before this census is trusted; exclusion is a human call via "
                f"exclude_non_tract_parcels")
        _record_stage_note(session, covid, "classify_parcels", note)
        return StageVerdict("needs_review", note, covid=covid, detail=result)

    _record_stage_note(session, covid, "classify_parcels", None)
    return StageVerdict("advanced",
                        f"{result.get('matched_parcels', '?')} parcels classified",
                        covid=covid, detail=result)


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------

def run_reconcile(session, covid: int, payload: dict) -> StageVerdict:
    """The gate that decides a covenant is done.

    Accuracy over completeness, per CLAUDE.md: classified acreage must reconcile
    with the covenant's stated acreage and any unaccounted area inside the
    footprint is flagged. A covenant that does not reconcile is not finished, and
    saying otherwise is the one outcome this project treats as unacceptable.
    """
    from app.gis.reconcile import reconcile_covenant

    # reconcile_covenant already writes covenant.status and merges its own
    # RECONCILIATION-STAGE note, including deliberately NOT claiming a
    # reconciliation when some other stage's concern is still open in
    # review_reason. So this stage reads its verdict rather than writing a second
    # note under a second tag -- two tags for one judgement would be two places
    # to disagree.
    result = reconcile_covenant(session, covid)
    final_status = result.get("final_status")
    per_tract = result.get("tract_results") or {}

    unchecked = {tn: r.get("reason") for tn, r in per_tract.items() if not r.get("checked")}
    problems = {tn: r.get("note") for tn, r in per_tract.items()
                if r.get("checked") and r.get("status") != "reconciled"}

    if final_status == "reconciled" and not unchecked and not problems:
        return StageVerdict("advanced",
                            f"reconciled across {len(per_tract)} tract(s)",
                            covid=covid, detail=result)

    detail = "; ".join(
        [f"tract {tn}: {note}" for tn, note in problems.items()]
        + [f"tract {tn} not checkable: {reason}" for tn, reason in unchecked.items()]
    ) or f"reconciliation left this covenant at status {final_status!r}"
    return StageVerdict("needs_review", detail, covid=covid, detail=result)


# --------------------------------------------------------------------------
# manual-only stages
# --------------------------------------------------------------------------

def run_chain_of_title(session, covid: int, payload: dict) -> StageVerdict:
    """Registered, not auto-enqueued. Walking title against a parcel census that
    has not reconciled spends recorder-portal and deed-reading effort on
    possibly-wrong parcels, so this runs on instruction."""
    from app.title.chain import walk_chain_of_title

    result = walk_chain_of_title(session, covid, max_parcels=payload.get("max_parcels"))
    return StageVerdict("advanced", f"chain walked: {result}", covid=covid, detail=result or {})


def run_fee_compute(session, covid: int, payload: dict) -> StageVerdict:
    """Registered, not auto-enqueued -- fees follow from transfers, which follow
    from a chain walk."""
    from app.title.fee_compute import compute_fees_for_covid

    result = compute_fees_for_covid(session, covid)
    return StageVerdict("advanced", f"fees computed: {result}", covid=covid, detail=result or {})


STAGES = {
    "intake": run_intake,
    "resolve_tract": run_resolve_tract,
    "classify_parcels": run_classify_parcels,
    "reconcile": run_reconcile,
    "chain_of_title": run_chain_of_title,
    "fee_compute": run_fee_compute,
}


def next_stage(stage: str, skip: tuple[str, ...] = ()) -> str | None:
    """The stage after this one, skipping any the completed stage said were
    pointless. Returns None at the end of the line -- and for a manual stage,
    which is not on the line at all."""
    if stage not in STAGE_ORDER:
        return None
    position = STAGE_ORDER.index(stage) + 1
    for candidate in STAGE_ORDER[position:]:
        if candidate not in skip:
            return candidate
    return None
