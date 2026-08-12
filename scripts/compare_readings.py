"""Compare the readings that exist for every covenant's legal descriptions.

Reads only local files -- the reviewed sheet and the OCR cache -- so it costs
nothing and touches no recorder, GIS or LLM. That is deliberate: this is the
step that runs BEFORE anchoring, because a tract whose reading drops half its
calls cannot be placed, and trying wastes the expensive tiers finding that out.

Usage:
  python3 scripts/compare_readings.py                 # portfolio summary
  python3 scripts/compare_readings.py 4981            # one covenant, in detail
  python3 scripts/compare_readings.py --dropped 20    # worst parser gaps
  python3 scripts/compare_readings.py --anchorable    # ready to anchor now
"""
import re
import sys

sys.path.insert(0, ".")

from app.ingestion.exha_sheet import read_sheet
from app.ingestion.text_compare import tract_facts, compare_covenant, sweep

_THENCE_RE = re.compile(r"\bTHENCE\b", re.IGNORECASE)


def _detail(covid: int) -> None:
    got = compare_covenant(covid)
    print(f"covid {covid}: {len(got['sheet_tracts'])} sheet tract row(s), "
          f"document copy {got['document_chars']:,} chars\n")
    for entry in got["sheet_tracts"]:
        f = entry["facts"]
        closure = f"1:{f.closure_denominator:,.0f}" if f.closure_denominator else "no traverse"
        print(f"  sheet row {entry['sheet_row']}")
        print(f"    {f.stated_acres} ac stated | {f.survey or '?'} Survey, "
              f"abstract {f.abstract or '?'}")
        print(f"    {f.course_count} courses read | closure {closure} | "
              f"area {f.area_acres and round(f.area_acres, 3)} "
              f"({'agrees' if f.area_agrees else 'DISAGREES'})")
        print(f"    evidenced in our document copy: {entry['evidenced_in_document']}"
              f"{'  [declaration/preamble]' if entry['declaration'] else ''}")
    if got["unevidenced_in_document"]:
        print(f"\n  the sheet describes land this COPY does not evidence: "
              f"{[e['sheet_row'] for e in got['unevidenced_in_document']]}")
        print("  -> a document-acquisition lead, not a finding about the land")
    if got["acreages_only_in_document"]:
        print(f"\n  acreages recited in the document with no sheet row: "
              f"{got['acreages_only_in_document'][:12]}")
    print(f"\n  anchorable now: {[e['sheet_row'] for e in got['anchorable']] or 'none'}")


def _dropped(limit: int) -> None:
    """Where the PARSER, not the text, is the problem."""
    findings = []
    for row in read_sheet():
        if not row.is_tract:
            continue
        facts = tract_facts(row.text)
        thence = len(_THENCE_RE.findall(row.text))
        if facts.course_count and thence > facts.course_count:
            findings.append((thence - facts.course_count, row, facts, thence))
    findings.sort(reverse=True, key=lambda f: f[0])
    total = sum(f[0] for f in findings)
    print(f"{len(findings)} tract descriptions read fewer courses than they have THENCE "
          f"calls, {total:,} calls dropped in total\n")
    for missing, row, facts, thence in findings[:limit]:
        closure = f"1:{facts.closure_denominator:,.0f}" if facts.closure_denominator else "-"
        print(f"  covid {row.covid:<5} row {row.row_number:<5} read {facts.course_count:>3} "
              f"of {thence:>3} ({missing:>3} dropped)  closure {closure:>12}  "
              f"stated {facts.stated_acres} ac")


def _anchorable() -> None:
    out = sweep()
    ready = [(c, e) for c, r in out["results"].items() for e in r["anchorable"]]
    print(f"{len(ready)} tract(s) whose reading closes tightly and reproduces the deed's "
          f"own acreage:\n")
    for covid, entry in sorted(ready):
        f = entry["facts"]
        print(f"  covid {covid:<5} row {entry['sheet_row']:<5} {f.stated_acres:>10} ac  "
              f"1:{f.closure_denominator:>10,.0f}  {f.survey or ''}")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--dropped":
        return _dropped(int(args[1]) if len(args) > 1 else 20)
    if args and args[0] == "--anchorable":
        return _anchorable()
    if args and args[0].isdigit():
        return _detail(int(args[0]))

    out = sweep()
    results = out["results"]
    tracts = [t for r in results.values() for t in r["sheet_tracts"]]
    walkable = [t for t in tracts if t["facts"].course_count >= 3]
    anchorable = [t for r in results.values() for t in r["anchorable"]]
    unevidenced = [t for r in results.values() for t in r["unevidenced_in_document"]]
    print(f"covenants compared              : {len(results):,}")
    print(f"  no document copy on disk      : {len(out['covids_without_a_document_copy']):,}")
    print(f"sheet tract rows                : {len(tracts):,}")
    print(f"  with a walkable traverse      : {len(walkable):,}")
    print(f"  closing tight enough to anchor: {len(anchorable):,}")
    print(f"sheet tracts our copy does not evidence: {len(unevidenced):,}")
    print("\nRun --dropped to see where the parser, not the text, is the problem.")


if __name__ == "__main__":
    main()
