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
import anthropic

from app.config import ANTHROPIC_API_KEY, LLM_MODEL_DEFAULT
from app.parsing.legal_description.metes_bounds import Course

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


def extract_courses_llm(text_segment: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=LLM_MODEL_DEFAULT,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        tools=[COURSE_EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": "record_courses"},
        messages=[{"role": "user", "content": text_segment}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_courses":
            return block.input
    raise RuntimeError("model did not return the expected tool call")


def to_course_objects(result: dict) -> list[Course]:
    return [
        Course(
            ns=c["ns"], degrees=c["degrees"], minutes=c["minutes"], seconds=c["seconds"],
            ew=c["ew"], distance_ft=c["distance_feet"],
        )
        for c in result["courses"]
    ]
