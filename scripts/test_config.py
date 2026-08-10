"""Assert the RESOLVED configuration matches the documented policy.

This exists because of a drift that hid for weeks in plain sight. CLAUDE.md's
model-routing rule says hard reasoning goes to Opus 5, and app/config.py's
source default was corrected to say so. But config.py reads

    LLM_MODEL_HARD = os.environ.get("LLM_MODEL_HARD", "claude-opus-5")

after calling load_dotenv(), and .env still carried

    LLM_MODEL_HARD=claude-opus-4-8

Once load_dotenv() puts a name in the environment, os.environ.get never sees its
own default. So the source read "opus-5", the docs read "opus-5", and every
hard-reasoning escalation -- anchor_agent's Tier 2 included -- actually ran a
model behind the policy. Nothing failed; it just quietly did the wrong thing.

Reading source files cannot catch that. Only resolving the config can, which is
what this does. `.env` is gitignored (it holds the API key), so it is invisible
to review and to every other test in this suite -- this is the one place a
machine's own configuration is checked against what the project says it does.

If the routing policy genuinely changes, change CLAUDE.md and this file together.
The point is that they cannot drift apart silently.

Usage: python3 scripts/test_config.py
"""
import os
import sys

sys.path.insert(0, ".")

# Straight from CLAUDE.md's "Model use (routing)" section. Sonnet at high effort
# is the default; Haiku for trivial subagent work; Opus 5 for hard reasoning and
# edge-case classification; Fable 5 for the very hardest and for bad-scan vision.
EXPECTED = {
    "LLM_MODEL_DEFAULT": "claude-sonnet-5",
    "LLM_MODEL_HARD": "claude-opus-5",
    "LLM_MODEL_HARDEST": "claude-fable-5",
}


def test_resolved_model_routing_matches_claude_md() -> None:
    from app import config

    wrong = []
    for name, expected in EXPECTED.items():
        actual = getattr(config, name)
        if actual != expected:
            source = "the environment or .env" if os.environ.get(name) else "app/config.py"
            wrong.append(f"{name} resolves to {actual!r}, expected {expected!r} "
                         f"(set by {source})")
    assert not wrong, (
        "resolved model routing disagrees with CLAUDE.md:\n  " + "\n  ".join(wrong)
        + "\n\nRemember .env beats the source default, and .env is gitignored -- "
          "check it before assuming app/config.py is what runs.")
    print("PASS: resolved model routing matches CLAUDE.md "
          f"(default={config.LLM_MODEL_DEFAULT}, hard={config.LLM_MODEL_HARD}, "
          f"hardest={config.LLM_MODEL_HARDEST})")


def test_trivial_tier_is_haiku() -> None:
    from app import config

    assert config.LLM_MODEL_TRIVIAL.startswith("claude-haiku"), (
        f"CLAUDE.md routes trivial subagent work to Haiku; resolved "
        f"{config.LLM_MODEL_TRIVIAL!r}")
    print(f"PASS: trivial tier is Haiku ({config.LLM_MODEL_TRIVIAL})")


def test_no_credentials_are_hardcoded() -> None:
    """CLAUDE.md: never commit API keys or credentials. config.py must read them
    from the environment with an EMPTY default, so a missing key fails loudly
    rather than a committed one working quietly."""
    with open("app/config.py", encoding="utf-8") as f:
        source = f.read()
    assert 'ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")' in source, (
        "the API key must come from the environment with an empty default")
    assert "sk-ant" not in source, "an API key appears to be hardcoded in app/config.py"
    print("PASS: no credentials hardcoded; the API key defaults to empty, never a literal")


def test_database_url_points_at_a_real_reachable_database() -> None:
    """A resolved-config test is the right place for this too: DATABASE_URL has
    the same .env-over-default shape, and pointing at the wrong database is a
    failure mode no source review catches either."""
    from sqlalchemy import text

    from app.config import DB_SCHEMA
    from app.db.session import get_session

    with get_session() as session:
        assert session.execute(text("SELECT 1")).scalar() == 1
        found = session.execute(
            text("SELECT count(*) FROM information_schema.schemata WHERE schema_name = :s"),
            {"s": DB_SCHEMA},
        ).scalar()
        assert found == 1, f"schema {DB_SCHEMA!r} not present in the configured database"
        version = session.execute(text("SELECT version_num FROM alembic_version")).scalar()
    print(f"PASS: configured database reachable, schema {DB_SCHEMA!r} present, "
          f"migrations at {version}")


if __name__ == "__main__":
    test_resolved_model_routing_matches_claude_md()
    test_trivial_tier_is_haiku()
    test_no_credentials_are_hardcoded()
    test_database_url_points_at_a_real_reachable_database()
    print("\nall config tests passed")
