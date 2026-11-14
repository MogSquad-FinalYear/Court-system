"""Runtime configuration, read once from the environment (.env supported)."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
BLOB_DIR = DATA_DIR / "blobs"
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "court.db"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
BLOB_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8020"))
CORS_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
]

# --- Document Management -------------------------------------------------
# Landing AI Agentic Document Extraction. The SDK reads this same variable
# itself, but we read it explicitly so the service can degrade cleanly
# (store + version + serve the file, skip extraction) instead of raising at
# import time on a box where no key is configured.
VISION_AGENT_API_KEY = os.environ.get("VISION_AGENT_API_KEY", "")
# "production" | "eu" | "staging" | "dev" -- passed through to the SDK.
LANDING_AI_ENVIRONMENT = os.environ.get("LANDING_AI_ENVIRONMENT", "production")
# Parse calls are a network round trip against a document that may be
# hundreds of pages; the SDK's own default is far shorter than that needs.
LANDING_AI_TIMEOUT_S = float(os.environ.get("LANDING_AI_TIMEOUT_S", "180"))

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
ALLOWED_SUFFIXES = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".docx"
}

# Signing key for document signatures and court seals. A real deployment
# replaces this with an HSM / X.509 key pair -- see services/seals.py.
SEAL_SIGNING_KEY = os.environ.get(
    "SEAL_SIGNING_KEY", "dev-only-insecure-signing-key-change-me"
).encode()

COURT_ID = os.environ.get("COURT_ID", "DISTRICT-COURT-01")
