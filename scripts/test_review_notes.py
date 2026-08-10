"""Smoke test for app/db/review_notes.py's merge_tagged_note.

Every case here is a real defect this helper was extracted to kill, using the
real note shapes from this project's own database -- not invented prose.

Usage: python3 scripts/test_review_notes.py
"""
import sys

sys.path.insert(0, ".")

from app.db.review_notes import merge_tagged_note


def test_replaces_own_note_only() -> None:
    existing = ("INGESTION-STAGE (automated, 2026-07-01): old ingestion detail; "
                "RECONCILIATION-STAGE (automated, 2026-08-01): old residual detail")
    got = merge_tagged_note(existing, "RECONCILIATION-STAGE",
                            "RECONCILIATION-STAGE (automated, 2026-08-06): new residual detail")
    assert got == ("INGESTION-STAGE (automated, 2026-07-01): old ingestion detail; "
                   "RECONCILIATION-STAGE (automated, 2026-08-06): new residual detail"), got
    print("PASS: merge_tagged_note -> replaces only its own tagged note")


def test_greedy_tail_bug_chain_py_covid_4780() -> None:
    """The real covid 4780 ordering. chain.py's own `.*$` pattern deleted the
    three notes following its own -- including hand-verified anchor
    provenance."""
    existing = ("CHAIN-OF-TITLE GAP (automated, 2026-07-26): 299216 owner mismatch; "
                "GEOMETRY DATA QUALITY (automated): 6 invalid parcels; "
                "ANCHOR RESOLVED (manual, tier=sibling_tract_tie, confidence=0.85): tract 1 anchored; "
                "RECONCILIATION-STAGE (automated, 2026-08-06): 12.5 ac unaccounted")
    got = merge_tagged_note(existing, "CHAIN-OF-TITLE GAP",
                            "CHAIN-OF-TITLE GAP (automated, 2026-08-06): rewalked")
    for survivor in ("GEOMETRY DATA QUALITY", "ANCHOR RESOLVED", "RECONCILIATION-STAGE"):
        assert survivor in got, f"{survivor} was destroyed: {got}"
    assert "299216 owner mismatch" not in got, got
    assert got.count("CHAIN-OF-TITLE GAP") == 1, got
    print("PASS: merge_tagged_note -> covid 4780's three following notes survive a chain re-walk "
          "(the greedy `.*$` bug)")


def test_following_manual_note_survives() -> None:
    """exclude_non_tract_parcels' own lookahead stopped at `(automated` only,
    so a following `(manual` note was swallowed whole."""
    existing = ("NON-TRACT PARCEL EXCLUSION (automated, tract 1): old detail (A1, A2); "
                "ANCHOR RESOLVED (manual, tier=named_feature_tie, confidence=0.85): must survive")
    got = merge_tagged_note(existing, "NON-TRACT PARCEL EXCLUSION",
                            "NON-TRACT PARCEL EXCLUSION (automated, tract 1): new detail (B1)")
    assert "ANCHOR RESOLVED (manual" in got, got
    assert "must survive" in got, got
    assert "old detail" not in got, got
    print("PASS: merge_tagged_note -> a following (manual ...) note survives")


def test_following_bare_date_qualifier_note_survives() -> None:
    """RE-VERIFIED (2026-07-24) is real on covid 2497/4955 and uses a bare date,
    matching neither 'automated' nor 'manual' -- an (?:automated|manual)
    lookahead would delete it."""
    existing = ("RECONCILIATION-STAGE (automated, 2026-08-01): stale; "
                "RE-VERIFIED (2026-07-24): hand-verified acreage reconciliation, must survive")
    got = merge_tagged_note(existing, "RECONCILIATION-STAGE",
                            "RECONCILIATION-STAGE (automated, 2026-08-06): fresh")
    assert "RE-VERIFIED (2026-07-24)" in got and "must survive" in got, got
    assert "stale" not in got, got
    print("PASS: merge_tagged_note -> a following bare-date-qualifier note (RE-VERIFIED) survives")


def test_collapses_accumulated_duplicates() -> None:
    """classifier.py appended unconditionally, so a covenant re-classified N
    times carries N copies. The next write must collapse them, not add N+1."""
    dup = "; ".join(
        [f"POSSIBLE NON-TRACT SUBDIVISION (automated): copy {i}" for i in range(4)]
    ) + "; RECONCILIATION-STAGE (automated, 2026-08-06): keep"
    got = merge_tagged_note(dup, "POSSIBLE NON-TRACT SUBDIVISION",
                            "POSSIBLE NON-TRACT SUBDIVISION (automated): fresh")
    assert got.count("POSSIBLE NON-TRACT SUBDIVISION") == 1, got
    assert "copy 0" not in got and "copy 3" not in got, got
    assert "RECONCILIATION-STAGE" in got, got
    print("PASS: merge_tagged_note -> collapses notes already duplicated by the old append-only bug")


def test_body_parens_do_not_split_a_note() -> None:
    """Note bodies really do contain parenthesised text (covid 4955: 'Thulasi
    Shri Investments LLC (deed 4/16/2015)'). Only a SEMICOLON followed by an
    upper-case tag ends a note, so parens alone are safe."""
    existing = ("PARCEL EXTRACTION (automated): current owner Thulasi Shri Investments LLC "
                "(deed 4/16/2015) differs from the declarant; RE-VERIFIED (2026-07-24): keep me")
    got = merge_tagged_note(existing, "PARCEL EXTRACTION", None)
    assert got == "RE-VERIFIED (2026-07-24): keep me", got
    print("PASS: merge_tagged_note -> a body containing '(...)' is removed whole and the sibling "
          "note survives")


def test_note_bodies_must_not_emit_a_semicolon_before_an_uppercase_tag() -> None:
    """The one convention a note AUTHOR has to honour, documented here because
    a real note format violated it: classifier.py's POSSIBLE NON-TRACT
    SUBDIVISION note originally joined its per-subdivision entries with "; ",
    each starting with an upper-case name and an open paren -- byte-identical
    to a note boundary, so the parser (correctly, unavoidably) treated the
    second entry as a separate note and left it behind. The fix is in the
    emitter: entries are joined with " | ". This test pins BOTH halves so the
    format can't silently regress."""
    bad = ("POSSIBLE NON-TRACT SUBDIVISION (automated): FORMAN WILLIAMSBURG SQUARE "
           "(15 parcels); HERCULES WEST ADDITION (25 parcels)")
    assert merge_tagged_note(bad, "POSSIBLE NON-TRACT SUBDIVISION", None) != "", (
        "a '; UPPERCASE (' inside a body is indistinguishable from a note boundary -- "
        "note emitters must not produce it"
    )
    good = ("POSSIBLE NON-TRACT SUBDIVISION (automated): FORMAN WILLIAMSBURG SQUARE "
            "(15 parcels) | HERCULES WEST ADDITION (25 parcels); RE-VERIFIED (2026-07-24): keep me")
    assert merge_tagged_note(good, "POSSIBLE NON-TRACT SUBDIVISION", None) == \
        "RE-VERIFIED (2026-07-24): keep me", merge_tagged_note(good, "POSSIBLE NON-TRACT SUBDIVISION", None)
    print("PASS: merge_tagged_note -> ' | '-joined multi-entry bodies are removed whole "
          "(and the '; UPPERCASE (' anti-pattern is pinned as unsupported)")


def test_fully_qualified_tag_keeps_sibling_tract_note() -> None:
    """covid 4440 really carries a tract 1 AND a tract 2 exclusion note. The
    qualifier is the only thing distinguishing them, so writing one must not
    delete the other -- which a bare "NON-TRACT PARCEL EXCLUSION" tag would."""
    existing = ("NON-TRACT PARCEL EXCLUSION (automated, tract 1): old t1 (A1); "
                "NON-TRACT PARCEL EXCLUSION (automated, tract 2): t2 must survive (B1)")
    got = merge_tagged_note(existing, "NON-TRACT PARCEL EXCLUSION (automated, tract 1)",
                            "NON-TRACT PARCEL EXCLUSION (automated, tract 1): new t1 (A2)")
    assert "t2 must survive" in got, got
    assert "old t1" not in got and "new t1 (A2)" in got, got
    # ...and the bare form deliberately matches every qualifier, for the
    # date-stamped tags where that is what's wanted.
    both_gone = merge_tagged_note(existing, "NON-TRACT PARCEL EXCLUSION", None)
    assert both_gone == "", both_gone
    print("PASS: merge_tagged_note -> a fully-qualified tag replaces only its own tract's note; "
          "the bare form still matches every qualifier")


def test_no_prefix_collision_between_similar_tags() -> None:
    """GEOMETRY DATA QUALITY and GEOMETRY DATA QUALITY RESOLVED both exist."""
    existing = ("GEOMETRY DATA QUALITY (automated): 6 invalid; "
                "GEOMETRY DATA QUALITY RESOLVED (automated): repaired 1")
    got = merge_tagged_note(existing, "GEOMETRY DATA QUALITY", None)
    assert got == "GEOMETRY DATA QUALITY RESOLVED (automated): repaired 1", got
    print("PASS: merge_tagged_note -> a tag that is a prefix of another tag doesn't remove it")


def test_empty_and_none_inputs() -> None:
    assert merge_tagged_note(None, "X-TAG", "X-TAG (automated): a") == "X-TAG (automated): a"
    assert merge_tagged_note("", "X-TAG", None) == ""
    assert merge_tagged_note("X-TAG (automated): only note", "X-TAG", None) == ""
    print("PASS: merge_tagged_note -> None/empty input, and removing the only note, all handled")


def test_an_underscore_in_a_tag_is_still_a_note_boundary() -> None:
    """Real data loss, found when app/pipeline's stage tags became the first ones
    in this project to contain an underscore. The note-boundary pattern's tag
    character class omitted it, so removing an EARLIER note swallowed the
    underscore-tagged one along with it:

        merge_tagged_note("ANCHOR RESOLVED (...): anchored; "
                          "CLASSIFY_PARCELS-STAGE (...): needs checking",
                          "ANCHOR RESOLVED", None)   ->   ""

    Both notes gone -- exactly the failure this module exists to prevent, arriving
    through the tag vocabulary rather than through a greedy `.*$`. It went
    unnoticed because every tag in the live data is hyphen-only and survives the
    same call.
    """
    record = "ANCHOR RESOLVED (automated, tier=llm_parcel_tie): anchored"
    for other in ("CLASSIFY_PARCELS-STAGE (automated): needs checking",
                  "RESOLVE_TRACT-STAGE (automated): could not anchor",
                  "NON-TRACT PARCEL EXCLUSION (automated, tract 1): excluded"):
        kept = merge_tagged_note(f"{record}; {other}", "ANCHOR RESOLVED", None)
        assert kept == other, f"removing the first note lost {other!r}; got {kept!r}"
        # And the reverse direction: removing the second must keep the first.
        tag = other.split(" (")[0]
        kept = merge_tagged_note(f"{record}; {other}", tag, None)
        assert kept == record, f"removing {tag!r} lost the first note; got {kept!r}"
    print("PASS: an underscore in a tag is a note boundary in both directions")


if __name__ == "__main__":
    test_replaces_own_note_only()
    test_greedy_tail_bug_chain_py_covid_4780()
    test_following_manual_note_survives()
    test_following_bare_date_qualifier_note_survives()
    test_collapses_accumulated_duplicates()
    test_body_parens_do_not_split_a_note()
    test_note_bodies_must_not_emit_a_semicolon_before_an_uppercase_tag()
    test_fully_qualified_tag_keeps_sibling_tract_note()
    test_no_prefix_collision_between_similar_tags()
    test_empty_and_none_inputs()
    print("\nall review_notes smoke tests passed")
    test_an_underscore_in_a_tag_is_still_a_note_boundary()
