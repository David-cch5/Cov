"""LLM-assisted extraction of metes-and-bounds courses from raw, noisy OCR text.

This is the escalation path when the deterministic regex parser (metes_bounds.py)
doesn't reliably separate the actual course data (bearing + distance) from the
dozens of interleaved "a called N acre tract described in a deed to..." adjoiner
clauses real surveys are full of -- confirmed necessary by testing the regex
parser against real text (21/38 courses captured, traverse didn't close).

The traverse-walk math itself (walk_traverse in metes_bounds.py) stays pure
deterministic code either way -- this module only replaces the text-to-structured-
data step, per the tiered escalation CLAUDE.md already calls for on noisy scans.
"""
import re

import anthropic

from app.config import ANTHROPIC_API_KEY, LLM_MODEL_DEFAULT, LLM_MODEL_HARD
from app.llm.usage import log_usage
from app.parsing.legal_description.metes_bounds import Course, extract_courses, walk_traverse
from app.queue.job_queue import run_with_job_queue

COURSE_EXTRACTION_TOOL = {
    "name": "record_courses",
    "description": "Record every metes-and-bounds course (bearing + distance) in this survey text, in order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "point_of_beginning": {"type": "string", "description": "The tie point description following 'BEGINNING at...', verbatim or near-verbatim."},
            "courses": {
                "type": "array",
                "description": "Every THENCE course, in order. Do not skip any, even when a long adjoiner-tract "
                                "description separates the bearing from its distance -- the adjoiner clauses "
                                "describe OTHER people's land along the line, not this course's own measurement.",
                "items": {
                    "type": "object",
                    "properties": {
                        "ns": {"type": "string", "enum": ["North", "South"]},
                        "degrees": {"type": "number"},
                        "minutes": {"type": "number"},
                        "seconds": {"type": "number"},
                        "ew": {"type": "string", "enum": ["East", "West"]},
                        "distance_feet": {"type": "number"},
                        "uncertain": {"type": "boolean", "description": "true if any digit in the bearing or "
                                       "distance looked OCR-garbled and this is a best-effort reading, not a "
                                       "confident transcription."},
                    },
                    "required": ["ns", "degrees", "minutes", "seconds", "ew", "distance_feet", "uncertain"],
                },
            },
            "confidence": {"type": "number", "description": "Overall confidence in the complete, in-order course list, 0-1."},
            "notes": {"type": ["string", "null"], "description": "Anything ambiguous: missing point-of-beginning, "
                       "a course that didn't fit the bearing/distance pattern, multiple tracts in view, etc."},
        },
        "required": ["point_of_beginning", "courses", "confidence"],
    },
}

SYSTEM_PROMPT = """You extract metes-and-bounds survey courses (bearing + distance pairs) from raw, \
often OCR-noisy legal descriptions. Each course is a THENCE clause: a compass bearing (North/South, \
degrees-minutes-seconds, East/West) and a distance in feet to the next corner. Between the bearing and \
its distance, surveys routinely list OTHER tracts and owners along that line ("a called 5 acre tract \
described in a deed to X") -- that is NOT the course's own data, keep reading past it to find this \
course's actual distance. Extract every course in order; do not silently drop one because the \
intervening text is long. Correct obvious non-numeric OCR noise (a misread word) using context, but \
never guess a numeric digit with false confidence -- if a bearing or distance digit is genuinely \
ambiguous, give your best reading and set uncertain=true rather than silently picking one."""


def extract_courses_llm(text_segment: str, model: str = LLM_MODEL_DEFAULT) -> dict:
    # Confirmed real, not hypothetical: a transient DNS/connection blip killed an
    # entire resolve_metes_and_bounds_anchor attempt outright on covid 4981 -- this
    # was the one Claude API call site in the metes-and-bounds path with no retry
    # at all (every other live network call in this project gets one via
    # run_with_job_queue; this one was missed when it was first written).
    def _call() -> dict:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=model,
            max_tokens=8192,
            # This system prompt + tool schema is identical on every call at a
            # given model tier -- called at least once per metes-and-bounds
            # covenant (often twice, Sonnet then Opus, per
            # extract_courses_with_escalation), so it's cached rather than
            # resent in full each time.
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[{**COURSE_EXTRACTION_TOOL, "cache_control": {"type": "ephemeral"}}],
            tool_choice={"type": "tool", "name": "record_courses"},
            messages=[{"role": "user", "content": text_segment}],
        )
        usage = log_usage("metes_bounds_llm", response)
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_courses":
                return {**block.input, "usage": usage}
        raise RuntimeError("model did not return the expected tool call")

    return run_with_job_queue(_call, job_type="llm_extract_courses", payload={"model": model})


def to_course_objects(result: dict) -> list[Course]:
    """Raises ValueError (never a bare TypeError/KeyError) if the model's own
    tool-call input doesn't actually match COURSE_EXTRACTION_TOOL's schema --
    confirmed real, not hypothetical: without tool-input strict-mode, Claude
    can occasionally return a malformed `courses` entry on a genuinely messy,
    multi-tract input (covid 4981, a 3-tract document in one Exhibit A) even
    though the SAME call succeeds on a retry. extract_courses_with_escalation
    catches this and treats it the same as "this tier's result looks
    incomplete" -- escalate to the next tier -- rather than letting a raw
    IndexError/TypeError crash the whole anchor-resolution attempt."""
    try:
        return [
            Course(
                ns=c["ns"], degrees=c["degrees"], minutes=c["minutes"], seconds=c["seconds"],
                ew=c["ew"], distance_ft=c["distance_feet"],
            )
            for c in result["courses"]
        ]
    except (TypeError, KeyError) as exc:
        raise ValueError(f"model's own tool-call input didn't match the expected course schema: {exc}") from exc


# Below this, a course count roughly this much lower than the raw text's own
# "THENCE" count is treated as a likely regex-parser gap (missing a class of
# phrasing the deterministic parser doesn't yet handle) rather than the deed
# genuinely having fewer courses -- confirmed real this project's own way,
# repeatedly: 3 separate real regex bugs (a curly-quote minutes marker, a
# "(Deed = X feet)" aside, a compound multi-bearing clause) were each found
# by noticing the extracted course count looked implausibly low relative to
# the number of THENCE occurrences in the source text.
_COURSE_COUNT_SHORTFALL_RATIO = 0.7
# A closure this loose (perimeter-to-error ratio) also signals a likely
# extraction gap rather than genuine survey imprecision -- real deeds in
# this project's own corpus have closed tighter than 1:500 even before any
# LLM-assisted fix.
_MIN_ACCEPTABLE_CLOSURE_RATIO = 1 / 500


def _looks_incomplete(text_segment: str, courses: list[Course]) -> bool:
    thence_count = len(re.findall(r"\bTHENCE\b", text_segment, re.IGNORECASE))
    if thence_count == 0:
        return False  # nothing to compare against -- don't second-guess a clean zero-course case
    if len(courses) < thence_count * _COURSE_COUNT_SHORTFALL_RATIO:
        return True
    if not courses:
        return True
    closure_ratio = walk_traverse(courses)["closure_ratio"]
    return closure_ratio is None or closure_ratio > _MIN_ACCEPTABLE_CLOSURE_RATIO


def extract_courses_with_escalation(text_segment: str) -> tuple[list[Course], dict]:
    """Runs the deterministic regex parser (metes_bounds.extract_courses)
    first -- free, and correct for the large majority of this project's own
    corpus once the 3 real bugs found this session were fixed. Only escalates
    to an LLM reading of the same text when the result looks genuinely
    incomplete (see _looks_incomplete): first at this project's default
    extraction tier (Sonnet, matching every other field-extraction call in
    this codebase), then at the Opus tier if that still doesn't produce a
    plausible course list. Returns (courses, diagnostics) -- diagnostics
    records which tier actually produced the returned courses, for the
    caller's own provenance/review-reason notes."""
    regex_courses = extract_courses(text_segment)
    zero_usage = {"input_tokens": 0, "output_tokens": 0,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    if not _looks_incomplete(text_segment, regex_courses):
        return regex_courses, {"tier": "regex", "courses_found": len(regex_courses), "usage": zero_usage}

    sonnet_result = extract_courses_llm(text_segment, model=LLM_MODEL_DEFAULT)
    try:
        sonnet_courses = to_course_objects(sonnet_result)
        sonnet_malformed = None
    except ValueError as exc:
        sonnet_courses, sonnet_malformed = [], str(exc)
    if sonnet_malformed is None and not _looks_incomplete(text_segment, sonnet_courses):
        return sonnet_courses, {
            "tier": "llm_sonnet", "courses_found": len(sonnet_courses),
            "extraction_confidence": sonnet_result.get("confidence"), "extraction_notes": sonnet_result.get("notes"),
            "usage": sonnet_result["usage"],
        }

    opus_result = extract_courses_llm(text_segment, model=LLM_MODEL_HARD)
    try:
        opus_courses = to_course_objects(opus_result)
        opus_malformed = None
    except ValueError as exc:
        opus_courses, opus_malformed = [], str(exc)
    if opus_malformed is not None:
        tier = "llm_opus_malformed"
    elif not _looks_incomplete(text_segment, opus_courses):
        tier = "llm_opus"
    else:
        tier = "llm_opus_still_incomplete"
    combined_usage = {
        key: sonnet_result["usage"][key] + opus_result["usage"][key] for key in zero_usage
    }
    notes = opus_result.get("notes")
    if sonnet_malformed:
        notes = f"{notes}; sonnet tier malformed: {sonnet_malformed}" if notes else f"sonnet tier malformed: {sonnet_malformed}"
    if opus_malformed:
        notes = f"{notes}; opus tier malformed: {opus_malformed}" if notes else f"opus tier malformed: {opus_malformed}"
    return opus_courses, {
        "tier": tier, "courses_found": len(opus_courses),
        "extraction_confidence": opus_result.get("confidence"), "extraction_notes": notes,
        "usage": combined_usage,
    }
