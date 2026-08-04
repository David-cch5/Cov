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
