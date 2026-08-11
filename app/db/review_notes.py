"""One implementation of covenant.review_reason's tagged-note convention.

review_reason is a single text column shared by every pipeline stage, holding a
"; "-joined list of notes each shaped:

    TAG (qualifier): body text, which may itself contain ; and ( ) freely

A stage must be able to re-run and replace ITS OWN note without disturbing any
other stage's -- so each write is a strip-then-append. Before this module, every
stage hand-rolled that regex, and they drifted apart in ways that silently
destroyed real data:

  - `.*$` (greedy to end of string) in app/title/chain.py and
    scripts/ingest_probe.py. covid 4780 really carries, in order,
    CHAIN-OF-TITLE GAP, GEOMETRY DATA QUALITY, ANCHOR RESOLVED (manual) and
    RECONCILIATION-STAGE -- re-running the chain walk there would have deleted
    the last three, including the hand-verified anchor provenance.
  - A lookahead of `\\(automated` only (app/gis/classifier.py's
    exclude_non_tract_parcels), which swallows any following `(manual` note --
    reachable, because scripts/manual_commit_*.py append their ANCHOR RESOLVED
    (manual, ...) note last.
  - No strip at all (classifier.py's GEOMETRY DATA QUALITY and POSSIBLE
    NON-TRACT SUBDIVISION notes), so every re-run appended another copy;
    classification re-runs on each monitor cycle, so these grew without bound.

The boundary between one note and the next is therefore detected here once, and
deliberately LIBERALLY: any "; " followed by an upper-case tag and an opening
paren ends the current note. Erring that way costs at worst a leftover fragment;
erring the other way deletes a sibling stage's note, which is the failure this
module exists to prevent. Verified against all 25 review_reason values currently
in the database: every boundary it finds is a real note tag, and none of the
free-text bodies (which do contain things like "Thulasi Shri Investments LLC
(deed 4/16/2015)") false-fire, because a real boundary requires the semicolon.

ONE CONSTRAINT ON NOTE AUTHORS: a note body must never contain "; " immediately
followed by an upper-case word and an open paren, because that is byte-identical
to a note boundary and no parser can tell them apart. Join multi-entry bodies
with " | " instead. This is not hypothetical -- classifier.py's own POSSIBLE
NON-TRACT SUBDIVISION note was written that way first and split itself in half.
"""
import re

# Real tag vocabulary in the live data spans automated, manual and bare-date
# qualifiers (ANCHOR RESOLVED (manual, ...), RE-VERIFIED (2026-07-24), ...), so
# the qualifier is deliberately unconstrained -- matching on tag shape alone.
# The underscore is load-bearing. Without it, a tag containing one is not
# recognised as a note boundary, so removing an EARLIER note deletes it too --
# the exact data loss this module exists to prevent, arriving through the tag
# vocabulary rather than through a greedy `.*$`. Demonstrated on real note text:
#
#   merge_tagged_note("ANCHOR RESOLVED (...): anchored; "
#                     "CLASSIFY_PARCELS-STAGE (...): needs checking",
#                     "ANCHOR RESOLVED", None)   ->   ""
#
# Both notes gone. A hyphen-only tag ("NON-TRACT PARCEL EXCLUSION") survived the
# same call, which is why this went unnoticed: every tag in the live data
# predates the pipeline's own stage tags, and none of them contained one.
_NEXT_TAG_OR_END = r"(?=;\s*[A-Z][A-Z0-9_ /&'-]{2,}\(|$)"


def merge_tagged_note(review_reason: str | None, tag: str, note: str | None = None) -> str:
    """Return review_reason with every existing `tag`-prefixed note removed and
    `note` appended (when given). Other stages' notes are always preserved.

    `tag` may be given either way:

      - Bare, e.g. "RECONCILIATION-STAGE" -- the "(automated, 2026-08-06)"
        qualifier is then matched generically, so a note written on an earlier
        date (or by an earlier code path using a different qualifier) is still
        recognised as the same note and replaced rather than duplicated.
      - Fully qualified, e.g. "NON-TRACT PARCEL EXCLUSION (automated, tract 1)"
        -- matched literally. Required when the qualifier is what distinguishes
        two notes that must COEXIST: covid 4440 really carries a tract 1 and a
        tract 2 exclusion note, and a bare tag would delete both when writing
        either.

    `note` is the complete replacement text, including its own tag.

    Removal is global, so a review_reason that already accumulated duplicate
    copies (from the pre-helper append-only bug) is collapsed on the next write
    rather than left to grow.
    """
    reason = review_reason or ""
    # A tag supplied with its own "(...)" is matched exactly; a bare one takes
    # any qualifier.
    tag_pattern = re.escape(tag) if tag.endswith(")") else rf"{re.escape(tag)}\s*\([^)]*\)"
    reason = re.sub(
        rf";?\s*{tag_pattern}:.*?{_NEXT_TAG_OR_END}",
        "", reason, flags=re.DOTALL,
    ).strip("; ").strip()
    if note:
        reason = f"{reason}; {note}" if reason else note
    return reason

# Tags whose notes RECORD work that succeeded rather than raising a concern.
# review_reason is the only place per-covenant provenance lives, so success
# records share the field with real concerns -- and anything reading the field to
# decide "is this covenant clean?" has to tell them apart.
#
# Confirmed real on covid 4956: its tract reconciled to 0.0000 ac unaccounted and
# the covenant still came back needs_review, because "ANCHOR RESOLVED (automated,
# tier=llm_parcel_tie, confidence=0.90)" was sitting in review_reason. A note
# saying the anchor WORKED was holding the covenant open, and no LLM-anchored
# covenant could ever have reached 'reconciled'.
#
# Deliberately an allow-list, not a deny-list: an unrecognised tag counts as a
# concern, so the safe direction stays the default and adding a record-style note
# is a deliberate act.
RECORD_ONLY_TAGS = (
    "ANCHOR RESOLVED",
    "RE-VERIFIED",
)

# ENCUMBERED LAND SUMMARY is deliberately NOT here, though it started as a
# summary and reads like one. On covid 4956 it was the natural place to record an
# open question -- 0.1381 ac of the tract lying inside a parcel that may be
# partially encumbered, unresolved until a plat is read -- and a tag that can
# carry either a record or a concern has to count as a concern. Being wrong in
# that direction leaves a covenant flagged for a human; being wrong the other way
# marks encumbered land finished.


def strip_record_only_notes(review_reason: str | None) -> str:
    """review_reason with the record-style notes removed, leaving only notes that
    represent something still open. Returns "" when nothing is outstanding.

    Does not modify anything -- callers use it to JUDGE a review_reason, while
    the field itself keeps every note, because the provenance is worth having.
    """
    remaining = review_reason or ""
    for tag in RECORD_ONLY_TAGS:
        remaining = merge_tagged_note(remaining, tag, None)
    return remaining.strip()
