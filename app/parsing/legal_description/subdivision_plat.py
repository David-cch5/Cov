"""Structure a subdivision-plat legal description (lot/block/plat reference) into
fields usable for GIS matching. Deliberately a small, separate, cheap call against
the already-extracted `legal_description_raw` text -- not a reprocess of the whole
document -- since phrasing varies too much across the corpus for a single regex
("Lot 1R Blk A, KELLER TOWN CENTER", "Lots 14 and 15, Block A/5, UNIVERSITY HILL",
"Lots 263, 281 through 286... of GLENEAGLES, SECTION 4A") to generalize reliably.

Returns a LIST, one entry per distinct tract/subdivision reference in the text, in
document order -- never a single merged reference. Confirmed real and necessary,
not a hypothetical: covid 4123's own legal description covers two genuinely
different tracts under different subdivisions (Lots 5-8 of "Country Meadows
Square" and, separately, Lot 2C of "Meadows Square 2nd Amend"). A single-reference
shape can only ever hold one of them, so resolve_subdivision_plat_tract (the only
caller) silently reused whichever reference happened to be cached for every
tract_no it was asked to resolve -- corrupting tract 1's classification with
tract 2's own lots on a re-run. The overwhelming majority of covenants describe
only a single tract; for those this is just a one-element list, no different in
practice from before this shape existed."""
import anthropic

from app.config import ANTHROPIC_API_KEY, LLM_MODEL_DEFAULT
from app.llm.usage import log_usage
from app.queue.job_queue import run_with_job_queue

SUBDIVISION_TOOL = {
    "name": "record_subdivision_references",
    "description": "Record every distinct subdivision/lot/block tract reference found in a legal description.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tracts": {
                "type": "array",
                "description": "One entry per distinct tract/subdivision reference in the text, in the "
                                "ORDER each is introduced. A legal description describing lots in TWO "
                                "different platted subdivisions (even if related, e.g. 'Country Meadows "
                                "Square' vs. 'Country Meadows Square 3rd Amd') is two entries here, never "
                                "merged into one. Most legal descriptions cover only a single tract -- "
                                "that's still a one-element list, not a bare object.",
                "items": {
                    "type": "object",
                    "properties": {
                        "subdivision_name": {"type": ["string", "null"], "description": "The subdivision name exactly as recited, e.g. 'GLENEAGLES, SECTION 4A'."},
                        "block": {"type": ["string", "null"], "description": "Block identifier, if any (e.g. 'A', 'A/5')."},
                        "lots": {"type": "array", "items": {"type": "string"},
                                  "description": "Every individual lot identifier belonging to THIS tract, with any 'X through Y' ranges fully expanded (e.g. '281 through 286' -> '281','282','283','284','285','286'). Preserve non-numeric suffixes like '1R' or '121-A' as-is. Never include a lot that belongs to a different tract's own subdivision reference."},
                        "plat_reference": {"type": ["string", "null"], "description": "Where this tract's own plat is recorded, e.g. 'Cabinet C, Sheet 28A' or 'Plat No. 16, Volume 16 Page 3'."},
                        "confidence": {"type": "number"},
                    },
                    "required": ["subdivision_name", "lots", "confidence"],
                },
            },
        },
        "required": ["tracts"],
    },
}

SYSTEM_PROMPT = """You structure subdivision-plat legal descriptions (lot/block/plat references) \
into discrete fields. A legal description may cover more than one distinct tract, each with its own \
subdivision/lot/block reference -- sometimes for genuinely different platted subdivisions, even if \
similarly named. Record EACH distinct tract as its own separate entry in `tracts`, in the order they're \
introduced in the text, rather than merging them into one combined reference. Expand every lot range \
fully within each tract's own lots -- never leave a range unexpanded, and never let one tract's lots \
bleed into another's entry. If a subdivision name or lot number is unclear, use null / omit it rather \
than guessing."""


def parse_subdivision_reference(legal_description_raw: str) -> list[dict]:
    def _call() -> list[dict]:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=LLM_MODEL_DEFAULT,
            max_tokens=2048,
            # This system prompt + tool schema is identical on every call --
            # called once per subdivision_plat covenant, often many in a batch
            # ingestion/resolution run -- so it's cached rather than resent in
            # full each time.
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            tools=[{**SUBDIVISION_TOOL, "cache_control": {"type": "ephemeral"}}],
            tool_choice={"type": "tool", "name": "record_subdivision_references"},
            messages=[{"role": "user", "content": legal_description_raw}],
        )
        # Deliberately NOT attached to the returned value, unlike this project's
        # other LLM call sites -- classifier.py json.dumps()'s this function's
        # return value verbatim into covenant.legal_description_parsed (a JSONB
        # column); a "usage" key would leak logging metadata into stored title
        # data. Logged here (print only) instead.
        log_usage("subdivision_plat", response)
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_subdivision_references":
                return block.input["tracts"]
        raise RuntimeError("model did not return the expected tool call")

    return run_with_job_queue(_call, job_type="llm_subdivision_reference")
