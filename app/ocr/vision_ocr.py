"""Vision-OCR escalation tier (CLAUDE.md's tiered OCR policy): Tesseract free
OCR first -> confidence gate -> escalate to a Claude vision model reading
directly from the page image -> still illegible -> human review queue. This
tier reads pixels directly rather than relying on a classical OCR engine's
text-region heuristics, so it is far more robust to page layouts that trip
those up (crowded paragraphs, thin margins, a stray fold/shadow) -- exactly
the failure mode this was built for: covid 8245's Exhibit A page, where
Tesseract's overall document vocab_score was high (0.99) but this one page's
extracted text was a truncated fragment of the real content.
"""
import base64

import anthropic

from app.config import ANTHROPIC_API_KEY, LLM_MODEL_HARD, LLM_MODEL_HARDEST
from app.llm.usage import log_usage

TRANSCRIBE_TOOL = {
    "name": "record_transcription",
    "description": "Record the verbatim text transcription of this scanned document page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The complete verbatim text of the page, preserving "
                      "line breaks and paragraph structure as closely as practical."},
            "confidence": {"type": "number", "description": "0-1 confidence that the transcription is a "
                           "complete, accurate reading of every character on the page."},
            "notes": {"type": ["string", "null"], "description": "Any words, numbers, or passages that were "
                      "illegible or genuinely ambiguous, if any -- flag them here rather than guessing."},
        },
        "required": ["text", "confidence"],
    },
}

SYSTEM_PROMPT = (
    "You transcribe a single scanned document page verbatim, exactly as printed, including all numbers, "
    "punctuation, and line structure. Do not summarize or paraphrase. If a character or word is genuinely "
    "illegible, mark it clearly (e.g. [illegible]) rather than guessing."
)


def ocr_page_image(image_path: str, model: str = LLM_MODEL_HARDEST) -> dict:
    """model defaults to Fable 5 (smartest, per CLAUDE.md); pass LLM_MODEL_HARD
    (Opus 4.8) for the cheaper fallback."""
    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    media_type = "image/png" if image_path.lower().endswith(".png") else "image/jpeg"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        tools=[TRANSCRIBE_TOOL],
        tool_choice={"type": "tool", "name": "record_transcription"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": "Transcribe this page verbatim."},
            ],
        }],
    )
    usage = log_usage(f"vision_ocr page={image_path}", response)
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_transcription":
            return {**block.input, "usage": usage}
    raise RuntimeError("model did not return the expected tool call")
