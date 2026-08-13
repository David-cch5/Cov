"""Audit what the course patterns SWALLOW: one match covering another call.

Overlapping matches became possible when extract_courses started deduping spans
instead of keeping its patterns mutually exclusive. The risk that introduced is
specific: a long match can span a whole following call, the dedup keeps the
longer one, and a course disappears -- silently, because a dropped course only
shows up as a bad closure.

TWO ARTIFACTS MAKE A NAIVE VERSION OF THIS AUDIT LIE, and both did:

  a match's own THENCE reads as "inside" it. _THENCE begins with an optional
    quote and `\\s*`, so a match may START at the whitespace before the word.
    Counting THENCE tokens from the match's start therefore counts its own, and
    reported 167 swallowed calls where the real number is 1.

  the same artifact makes a call look UNREAD. Testing "does any match start at
    or after this THENCE" fails when the match legitimately starts a character
    or two earlier. That is what made covid 4981's row 1667 appear to drop two
    calls it in fact reads.

So this counts THENCE tokens strictly AFTER the match's own, and reports the
covid and row for anything it finds rather than a bare total.

Usage: python3 scripts/audit_course_spans.py
"""
import collections
import re
import sys

sys.path.insert(0, ".")

from app.ingestion.exha_sheet import read_sheet
from app.parsing.legal_description import metes_bounds as mb

PATTERNS = [
    ("COURSE", mb._COURSE_RE),
    ("COMPACT", mb._COMPACT_COURSE_RE),
    ("COMPOUND", mb._COMPOUND_BEARING_RE),
    ("CHORD_CURVE", mb._CHORD_CURVE_RE),
    ("NON_TANGENT", mb._NON_TANGENTIAL_CURVE_RE),
    ("TANGENT", mb._TANGENT_CURVE_RE),
]
_THENCE_WORD = re.compile(r"\bTHENCE\b", re.IGNORECASE)


def _kept_spans(text: str) -> list[tuple[int, int, str]]:
    """Replay extract_courses' dedup: earliest start wins, longest span breaks ties."""
    spans = [(m.start(), m.end(), name)
             for name, pattern in PATTERNS for m in pattern.finditer(text)]
    kept, end = [], -1
    for start, stop, name in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]))):
        if start < end:
            continue
        kept.append((start, stop, name))
        end = stop
    return kept


def main() -> None:
    swallowed = collections.Counter()
    rows = collections.defaultdict(set)
    findings = []
    scanned = 0
    for row in read_sheet():
        if not row.is_tract:
            continue
        scanned += 1
        text = mb.repair_ocr_survey_words(mb.repair_ocr_decimals(row.text))
        for start, stop, name in _kept_spans(text):
            own = _THENCE_WORD.search(text, start, stop)
            after = own.end() if own else start
            extra = _THENCE_WORD.findall(text[after:stop])
            if extra:
                swallowed[name] += len(extra)
                rows[name].add((row.covid, row.row_number))
                findings.append((len(extra), stop - start, name, row.covid, row.row_number))

    print(f"{scanned:,} tract descriptions scanned\n")
    print(f"{'pattern':<13} {'calls swallowed':>16} {'rows':>6}")
    for name, _ in PATTERNS:
        print(f"{name:<13} {swallowed[name]:>16} {len(rows[name]):>6}")
    print(f"\ntotal: {sum(swallowed.values())} call(s) swallowed")
    for extra, length, name, covid, row_number in sorted(findings, reverse=True):
        print(f"   {name:<12} {length:>5}-char span swallows {extra} call(s): "
              f"covid {covid}, sheet row {row_number}")


if __name__ == "__main__":
    main()
