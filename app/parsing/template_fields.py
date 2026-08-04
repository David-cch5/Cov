"""Per-covenant field extraction. The template clustering already tells us which
boilerplate a document uses (declarant/fee/exemption clause structure) -- this step
extracts the doc-specific values (who the declarant is, the recited fee %, the legal
description, etc.) rather than re-parsing the whole document from scratch.

Uses Claude (default: Sonnet, per CLAUDE.md model routing) with a tool-use schema for
reliable structured output, and prompt caching on the (static, reused-every-call)
system instructions + schema.
"""
import json

import anthropic

from app.config import ANTHROPIC_API_KEY, LLM_MODEL_DEFAULT
from app.llm.usage import log_usage
from app.queue.job_queue import run_with_job_queue

EXTRACTION_TOOL = {
    "name": "record_covenant_fields",
    "description": "Record the extracted fields for this covenant document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "declarant_name": {"type": "string", "description": "The Declarant's name exactly as recited."},
            "declarant_address": {"type": ["string", "null"], "description": "The Declarant's mailing address, if stated."},
            "declarant_type": {"type": "string", "enum": ["individual", "entity", "trust", "government", "unknown"],
                                "description": "Individual person vs. a company (LLC/LP/Inc/Corp/Ltd) vs. a trust vs. a government body, based on how the name reads."},
            "fee_percent": {"type": ["number", "null"], "description": "The Reconveyance/Transfer Fee percentage, e.g. 1.0 for one percent."},
            "term_description": {"type": ["string", "null"], "description": "How long the covenant runs / when it terminates, as recited."},
            "recording_instrument": {"type": ["string", "null"], "description": "This covenant's own recording/instrument/file number."},
            "recording_date": {"type": ["string", "null"], "description": "Recording date in YYYY-MM-DD format, if stated."},
            "book": {"type": ["string", "null"]},
            "page": {"type": ["string", "null"]},
            "stated_acreage": {"type": ["number", "null"], "description": "Total acreage of the Property as recited. "
                                "CAUTION for lot/plat descriptions: a phrase like 'Lots 1-5 of OAKWOOD, a subdivision "
                                "of 40 acres...' states the size of the WHOLE referenced subdivision, not the acreage "
                                "actually encumbered by the specific listed lots -- do not report that figure here. "
                                "Leave null in that case; the real encumbered acreage gets computed later from the "
                                "matched parcels' own GIS geometry, which is the trustworthy source for this shape "
                                "of legal description."},
            "legal_description_raw": {"type": "string", "description": "The Exhibit A / legal description text, verbatim or near-verbatim."},
            "legal_description_type": {"type": "string", "enum":
                ["texas_abstract", "plss", "metes_bounds", "subdivision_plat", "unknown"],
                "description": "subdivision_plat = described by lot/block/plat reference to a recorded "
                                "subdivision plat, rather than metes-and-bounds courses or a PLSS "
                                "township/range/section. Prefer this over guessing metes_bounds or plss "
                                "when the description is fundamentally a lot/block/plat citation."},
            "exemptions_raw": {"type": "string", "description": "The exemptions clause (usually Section 6) text, verbatim or near-verbatim."},
            "fee_due_days": {"type": ["integer", "null"], "description": "Days after a qualifying transfer the fee is due, if a specific number is stated."},
            "trustee_name": {"type": ["string", "null"], "description": "The named Trustee's name exactly as recited (the party fees are paid to, usually named in the Definitions section or a 'TRUSTEE'/administrator clause). Null if no trustee is named or the document says the trustee cannot be identified."},
            "trustee_address": {"type": ["string", "null"], "description": "The Trustee's mailing address, if stated."},
            "beneficiaries": {
                "type": "array",
                "description": "Every Beneficiary named in the BENEFICIARIES section, in the order listed. Omit this entirely (empty array) rather than guessing if that section is missing/illegible. A garbled OCR list (e.g. missing letters, percentages that don't sum to ~100%) is expected sometimes -- report exactly what's legible per beneficiary rather than inferring a missing one's share.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The beneficiary's name exactly as recited."},
                        "address": {"type": ["string", "null"]},
                        "percentage_interest": {"type": ["number", "null"], "description": "This beneficiary's percentage share, e.g. 33.0 for (33%). Null if the document names this beneficiary but the percentage itself is illegible/not stated -- never split the remainder evenly or guess."},
                    },
                    "required": ["name"],
                },
            },
            "confidence": {"type": "number", "description": "Extractor's own confidence in this extraction, 0-1."},
            "extraction_notes": {"type": ["string", "null"], "description": "Anything ambiguous, missing, or uncertain -- for the human review queue, never omit a concern to force a clean-looking result."},
        },
        "required": [
            "declarant_name", "declarant_type", "legal_description_raw", "legal_description_type",
            "exemptions_raw", "confidence",
        ],
    },
}

SYSTEM_PROMPT = """You extract structured fields from recorded private-transfer-fee covenant \
documents. Only report what the document actually states. If a field is not stated, use null \
rather than guessing or inferring a plausible-sounding value -- an incorrect guess is worse than \
a missing value here, since this feeds a title-record system. Note anything uncertain in \
extraction_notes rather than silently picking one reading."""


def extract_fields(covenant_text: str, template_version_id: str | None) -> dict:
    template_hint = (
        f"This document has already been matched to template {template_version_id}; "
        "its boilerplate structure is known, focus on the doc-specific values."
        if template_version_id else
        "This document's template is not yet identified -- extract from the raw text directly."
    )

    def _call() -> dict:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=LLM_MODEL_DEFAULT,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[{**EXTRACTION_TOOL, "cache_control": {"type": "ephemeral"}}],
            tool_choice={"type": "tool", "name": "record_covenant_fields"},
            messages=[
                {
                    "role": "user",
                    "content": f"{template_hint}\n\n--- COVENANT TEXT ---\n{covenant_text}",
                }
            ],
        )

        usage = log_usage(f"template_fields template={template_version_id}", response)
        for block in response.content:
            if block.type == "tool_use" and block.name == "record_covenant_fields":
                return {**block.input, "usage": usage}

        raise RuntimeError("model did not return the expected tool call")

    return run_with_job_queue(_call, job_type="llm_extract_fields", payload={"template_version_id": template_version_id})
