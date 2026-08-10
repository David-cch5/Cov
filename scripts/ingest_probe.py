"""Run ingestion for the sanctioned probe scope: Montgomery County TX + the 4-covenant
multi-county pilot. Not the full portfolio -- see CLAUDE.md's scope guardrail.

Each covenant commits independently (idempotent + resumable, per BUILD_SPEC): one
document failing (e.g. an API error) never blocks or rolls back the others.

The ingestion logic itself now lives in app/ingestion/ingest.py, so the queued
pipeline can call it too. What stays here is the probe's own scope -- which
covids, and the batch reporting.

Usage: python3 scripts/ingest_probe.py
"""
import sys

sys.path.insert(0, ".")

from app.db.session import get_session
from app.ingestion.ingest import escalate_ocr_confidence, ingest_one
from app.ingestion.ocr_escalation import MAX_PAGES_WITHOUT_APPROVAL
from app.ingestion.walk import iter_candidates


MONTGOMERY_TX_COVIDS = ["3346", "4781", "4440", "8245", "4780", "3194", "3297"]
PILOT_COVIDS = ["7029", "5340", "5835", "3428"]
# One covenant each from 10 further TX counties (Bexar, Denton, Collin, Harris, Kerr,
# Travis, Nueces, Webb, Ellis, Hunt) -- picked as the cleanest-OCR (highest vocab_score)
# covenant in each county from _pilot/covid_index.csv, to broaden the small multi-county
# sample per CLAUDE.md's scope guardrail without approaching the full portfolio.
EXPANDED_TX_SAMPLE_COVIDS = ["2497", "7938", "4955", "7991", "7768", "7994", "5963", "2340", "8386", "5346"]

def run(covids: list[str], max_ocr_escalation_pages: int = MAX_PAGES_WITHOUT_APPROVAL) -> None:
    with get_session() as lookup_session:
        candidates = list(iter_candidates(lookup_session, covids))

    escalate_ocr_confidence(candidates, max_ocr_escalation_pages)

    succeeded, failed = [], []
    for c in candidates:
        print(f"--- covid {c.covid} ---")
        try:
            with get_session() as session:
                ingest_one(session, c)
            succeeded.append(c.covid)
        except Exception as exc:
            print(f"  FAILED: {exc}")
            failed.append((c.covid, str(exc)))

    print(f"\n{len(succeeded)} succeeded, {len(failed)} failed")
    for covid, err in failed:
        print(f"  {covid}: {err}")


if __name__ == "__main__":
    run(MONTGOMERY_TX_COVIDS + PILOT_COVIDS + EXPANDED_TX_SAMPLE_COVIDS)
