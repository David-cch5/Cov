import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://postgres@localhost:5544/covenant_db"
)
DB_SCHEMA = os.environ.get("DB_SCHEMA", "covenant")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

LLM_MODEL_DEFAULT = os.environ.get("LLM_MODEL_DEFAULT", "claude-sonnet-5")
LLM_MODEL_HARD = os.environ.get("LLM_MODEL_HARD", "claude-opus-5")
LLM_MODEL_HARDEST = os.environ.get("LLM_MODEL_HARDEST", "claude-fable-5")
LLM_MODEL_TRIVIAL = os.environ.get("LLM_MODEL_TRIVIAL", "claude-haiku-4-5-20251001")

OBJECT_STORAGE_ROOT = os.environ.get("OBJECT_STORAGE_ROOT", "./_object_store")

# Recorder-portal credentials, keyed by VENDOR rather than by county: one GovOS
# PublicSearch login covers every *.publicsearch.us county, so Collin, Denton,
# Montgomery and Nueces all authenticate with the same pair. Which credential a
# county needs is recorded in county_recorder_registry.auth_notes; the secret
# itself lives only in .env, which is gitignored.
#
# Empty is a valid state and must stay one: searching these portals works as a
# guest, and only DOCUMENT IMAGES need a login. Code that needs credentials asks
# for them explicitly and reports their absence rather than failing obscurely --
# see app/recorder/document_image.py.
PUBLICSEARCH_USERNAME = os.environ.get("PUBLICSEARCH_USERNAME", "")
PUBLICSEARCH_PASSWORD = os.environ.get("PUBLICSEARCH_PASSWORD", "")


def publicsearch_credentials() -> tuple[str, str] | None:
    """The GovOS login, or None if it is not configured. Never logs either value."""
    if PUBLICSEARCH_USERNAME and PUBLICSEARCH_PASSWORD:
        return PUBLICSEARCH_USERNAME, PUBLICSEARCH_PASSWORD
    return None
