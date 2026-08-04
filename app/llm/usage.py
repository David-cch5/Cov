"""Shared token-usage logging for every direct Claude API call site in this
project (app/ocr/vision_ocr.py, app/parsing/template_fields.py,
app/parsing/legal_description/{subdivision_plat,metes_bounds_llm}.py, plus
app/llm/anchor_agent.py's own per-turn summing for its agentic loop).

Print-only by design -- persisting usage durably beyond stdout is each
caller's own decision, since where it's safe to attach usage to a function's
*return value* depends on what that caller does with it. Confirmed a real
risk, not hypothetical: subdivision_plat.py's parse_subdivision_reference()
result gets json.dumps()'d verbatim into covenant.legal_description_parsed
(a JSONB column) -- silently adding a "usage" key there would leak logging
metadata into structured title data. That call site logs via this helper but
deliberately does not attach the returned totals to its own result.
"""


def log_usage(label: str, response) -> dict:
    """Print one line reporting a single Messages API response's real usage
    and return it as a plain dict ({input_tokens, output_tokens,
    cache_creation_input_tokens, cache_read_input_tokens}) for callers that
    want to aggregate or persist it themselves. `label` should identify the
    call site and any relevant context (e.g. a covid) since the response
    itself carries no such context."""
    usage = response.usage
    totals = {
        "input_tokens": usage.input_tokens or 0,
        "output_tokens": usage.output_tokens or 0,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
        "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
    }
    print(
        f"  [llm_usage] {label} model={response.model} tokens_in={totals['input_tokens']} "
        f"tokens_out={totals['output_tokens']} cache_write={totals['cache_creation_input_tokens']} "
        f"cache_read={totals['cache_read_input_tokens']}"
    )
    return totals
