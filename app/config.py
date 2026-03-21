import os
from pathlib import Path

ENV = os.environ.get("VIRGIL_ENV", "local")  # "local" or "prod"
IS_PROD = ENV == "prod"

DB_PATH = os.environ.get("VIRGIL_DB_PATH", str(Path(__file__).parent.parent / "data" / "virgil.db"))
SECOND_BRAIN_PATH = os.environ.get("VIRGIL_SECOND_BRAIN_PATH", "")
ENCRYPTION_KEY = os.environ.get("VIRGIL_ENCRYPTION_KEY", "")
BASE_URL = os.environ.get("VIRGIL_BASE_URL", "http://localhost:8123")
HOST = os.environ.get("VIRGIL_HOST", "0.0.0.0")
PORT = 8123

# Internal LLM — used for onboarding and system features.
# Fallback when no user-configured provider is active.
INTERNAL_LLM_MODEL = os.environ.get("VIRGIL_INTERNAL_LLM_MODEL", "gemini/gemini-3-flash-preview")
INTERNAL_LLM_KEY = os.environ.get("VIRGIL_INTERNAL_LLM_KEY", "")
