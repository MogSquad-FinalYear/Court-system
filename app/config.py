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
    o.strip() for o in os.environ.get(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",") if o.strip()
]
if "*" in CORS_ORIGINS:
    raise RuntimeError("CORS_ORIGINS must list explicit frontend origins when credentials are enabled")

JWT_PRIVATE_KEY = os.environ.get("JWT_PRIVATE_KEY", "")
JWT_PUBLIC_KEY = os.environ.get("JWT_PUBLIC_KEY", "")
JWT_ISSUER = os.environ.get("JWT_ISSUER", "veritas-court")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "veritas-court-api")
JWT_ACCESS_TTL_MINUTES = int(os.environ.get("JWT_ACCESS_TTL_MINUTES", "10"))
JWT_REFRESH_TTL_DAYS = int(os.environ.get("JWT_REFRESH_TTL_DAYS", "30"))
AUTH_ALLOW_REGISTRATION = os.environ.get("AUTH_ALLOW_REGISTRATION", "false").lower() == "true"
AUTH_REGISTRATION_JURISDICTION = os.environ.get("AUTH_REGISTRATION_JURISDICTION", "")
AUTH_REFRESH_COOKIE = os.environ.get("AUTH_REFRESH_COOKIE", "veritas_refresh")
AUTH_COOKIE_SECURE = os.environ.get("AUTH_COOKIE_SECURE", "true").lower() == "true"

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
