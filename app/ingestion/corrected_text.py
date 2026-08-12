"""Text somebody has CORRECTED, kept so the correction is made once.

Every other text source in this project is a machine reading of a scan:
_textcache* (the original OCR), _intake_text/ (pdftotext then Tesseract), and
_ocr_escalated/ (vision OCR). All three re-derive from the same damaged image on
every run, so a fault fixed today is re-read tomorrow -- covid 4981's Exhibit A cost
six separate repairs (a lost decimal point, "an atc distance", "Chord Beating",
"82.94 fect", "10" for "to", a comma before the terminator) and nothing kept them.

This directory holds the corrected reading itself, with what was changed and on what
evidence, so the work is not repeated per run.

IT IS VERSION-CONTROLLED, unlike every other text directory in this project. Those
hold machine readings, and losing one costs a re-run; this holds a human verdict on
which reading is right, which nothing regenerates. Keep it that way when adding
files here.

A WHOLE-DOCUMENT CORRECTION AND A SEGMENT CORRECTION ARE NOT THE SAME THING, and
conflating them would be a regression rather than a fix. A corrected TRACT
description covers one tract of a document that may hold several, so returning it
where the whole text was asked for would silently discard every other tract. Only a
correction recorded as scope='document' is preferred by get_deed_text; a
scope='segment' correction is offered separately, to callers reading one tract.

VERIFIED IS A SEPARATE CLAIM FROM CORRECTED. A repair that makes a description parse
is not thereby right: covid 4981's tract now reads all 14 of its calls and its area
lands within 0.6% of the stated 11.878 acres, while the traverse still closes at
1:25. So `verified` stays false until a traverse closes or a person signs off, and
the evidence for the claim travels with the text.
"""
import json
import os
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CORRECTED_DIR = os.path.join(PROJECT_ROOT, "_corrected_text")

SCOPES = ("document", "segment")


def _path(covid: int, key: str = "document") -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return os.path.join(CORRECTED_DIR, f"{covid}_{safe}.json")


def save_correction(covid: int, text: str, *, corrected_by: str, basis: str,
                    scope: str = "document", key: str = "document",
                    repairs: list[str] | None = None, verified: bool = False,
                    evidence: dict | None = None) -> str:
    """Record a corrected reading. Returns the file path.

    `corrected_by` and `basis` are required and not defaulted: a correction with no
    author and no stated evidence is just another opinion about the text, and this
    file outranks three machine readings.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")
    if not text or not text.strip():
        raise ValueError("a correction needs text")
    if not corrected_by or not basis:
        raise ValueError("a correction must say who made it and on what evidence")

    os.makedirs(CORRECTED_DIR, exist_ok=True)
    record = {
        "covid": covid, "scope": scope, "key": key, "text": text,
        "corrected_by": corrected_by, "basis": basis,
        "repairs": repairs or [],
        # Not the same as corrected -- see the module docstring.
        "verified": verified, "evidence": evidence or {},
        "chars": len(text),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _path(covid, key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=1)
    return path


def load_correction(covid: int, key: str = "document") -> dict | None:
    path = _path(covid, key)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def corrected_document_text(covid: int) -> str | None:
    """The corrected WHOLE-document text, or None. Segment corrections are
    deliberately not returned here -- see the module docstring."""
    record = load_correction(covid)
    if record and record.get("scope") == "document" and record.get("text"):
        return record["text"]
    return None


def corrections_for(covid: int) -> list[dict]:
    """Every correction on record for a covenant, document and segment alike."""
    if not os.path.isdir(CORRECTED_DIR):
        return []
    out = []
    for name in sorted(os.listdir(CORRECTED_DIR)):
        if not name.startswith(f"{covid}_") or not name.endswith(".json"):
            continue
        with open(os.path.join(CORRECTED_DIR, name), encoding="utf-8") as f:
            out.append(json.load(f))
    return out
