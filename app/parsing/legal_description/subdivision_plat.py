"""Structure a subdivision-plat legal description (lot/block/plat reference) into
fields usable for GIS matching. Deliberately a small, separate, cheap call against
the already-extracted `legal_description_raw` text -- not a reprocess of the whole
document -- since phrasing varies too much across the corpus for a single regex
("Lot 1R Blk A, KELLER TOWN CENTER", "Lots 14 and 15, Block A/5, UNIVERSITY HILL",
"Lots 263, 281 through 286... of GLENEAGLES, SECTION 4A") to generalize reliably.
"""
import anthropic

from app.config import ANTHROPIC_API_KEY, LLM_MODEL_DEFAULT

SUBDIVISION_TOOL = {
    "name": "record_subdivision_reference",
    "description": "Record the structured subdivision/lot/block reference from a legal description.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subdivision_name": {"type": ["string", "null"], "description": "The subdivision name exactly as recited, e.g. 'GLENEAGLES, SECTION 4A'."},
            "block": {"type": ["string", "null"], "description": "Block identifier, if any (e.g. 'A', 'A/5')."},
            "lots": {"type": "array", "items": {"type": "string"},
                      "description": "Every individual lot identifier, with any 'X through Y' ranges fully expanded (e.g. '281 through 286' -> '281','282','283','284','285','286'). Preserve non-numeric suffixes like '1R' or '121-A' as-is."},
            "plat_reference": {"type": ["string", "null"], "description": "Where the plat itself is recorded, e.g. 'Cabinet C, Sheet 28A' or 'Plat No. 16, Volume 16 Page 3'."},
            "confidence": {"type": "number"},
        },
        "required": ["subdivision_name", "lots", "confidence"],
    },
}

SYSTEM_PROMPT = """You structure subdivision-plat legal descriptions (lot/block/plat references) \
into discrete fields. Expand every lot range fully -- never leave a range unexpanded. If the \
subdivision name or a lot number is unclear, use null / omit it rather than guessing."""


def parse_subdivision_reference(legal_description_raw: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=LLM_MODEL_DEFAULT,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        tools=[SUBDIVISION_TOOL],
        tool_choice={"type": "tool", "name": "record_subdivision_reference"},
        messages=[{"role": "user", "content": legal_description_raw}],
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_subdivision_reference":
            return block.input
    raise RuntimeError("model did not return the expected tool call")
