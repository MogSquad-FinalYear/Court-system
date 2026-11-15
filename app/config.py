"""Runtime configuration, read once from the environment (.env supported)."""
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
def _env_multiline(name: str) -> str:
    return os.environ.get(name, "").replace("\\n", "\n")


DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
BLOB_DIR = DATA_DIR / "blobs"
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "court.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL", "")
_cloudinary_parts = urlsplit(CLOUDINARY_URL) if CLOUDINARY_URL else None
CLOUDINARY_CLOUD_NAME = (
    _cloudinary_parts.hostname if _cloudinary_parts else
    os.environ.get("CLOUDINARY_CLOUD_NAME", "")
)
CLOUDINARY_API_KEY = (
    _cloudinary_parts.username if _cloudinary_parts else
    os.environ.get("CLOUDINARY_API_KEY", "")
)
CLOUDINARY_API_SECRET = (
    _cloudinary_parts.password if _cloudinary_parts else
    os.environ.get("CLOUDINARY_API_SECRET", "")
)
if CLOUDINARY_URL and not all(
    (CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)
):
    raise RuntimeError("CLOUDINARY_URL must contain a cloud name, API key, and API secret")

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

JWT_PRIVATE_KEY = _env_multiline("JWT_PRIVATE_KEY")
JWT_PUBLIC_KEY = _env_multiline("JWT_PUBLIC_KEY")
JWT_ISSUER = os.environ.get("JWT_ISSUER", "veritas-court")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "veritas-court-api")
JWT_ACCESS_TTL_MINUTES = int(os.environ.get("JWT_ACCESS_TTL_MINUTES", "10"))
JWT_REFRESH_TTL_DAYS = int(os.environ.get("JWT_REFRESH_TTL_DAYS", "30"))
AUTH_ALLOW_REGISTRATION = os.environ.get("AUTH_ALLOW_REGISTRATION", "false").lower() == "true"
AUTH_REGISTRATION_JURISDICTION = os.environ.get("AUTH_REGISTRATION_JURISDICTION", "")
AUTH_REFRESH_COOKIE = os.environ.get("AUTH_REFRESH_COOKIE", "veritas_refresh")
AUTH_COOKIE_SECURE = os.environ.get("AUTH_COOKIE_SECURE", "true").lower() == "true"
AUTH_COOKIE_SAMESITE = os.environ.get("AUTH_COOKIE_SAMESITE", "strict").lower()
if AUTH_COOKIE_SAMESITE not in {"strict", "lax", "none"}:
    raise RuntimeError("AUTH_COOKIE_SAMESITE must be strict, lax, or none")
if AUTH_COOKIE_SAMESITE == "none" and not AUTH_COOKIE_SECURE:
    raise RuntimeError("AUTH_COOKIE_SECURE must be true when AUTH_COOKIE_SAMESITE is none")

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
