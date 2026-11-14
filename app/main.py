"""Court case management system -- API entrypoint.

All four features are built. Document Management is backed by Landing AI
Agentic Document Extraction. The Integration Hub's five connectors are
mocks: the calling framework around them (idempotency, retry, circuit
breaker, redaction, call log) is real, the upstreams are not.

A browser UI for exercising all four features is served at /ui.

Run:
    uvicorn app.main:app --reload --port 8020      (from the project root)
    python -m app.main                             (binds HOST/PORT from config)
"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import CORS_ORIGINS, HOST, PORT
from app.db import init_db
from app.routers import documents, filing, integrations, scheduling
from app.services import extraction

app = FastAPI(
    title="Court Case Management System",
    version="0.1.0",
    description=(
        "All four features are implemented. Document Management is backed by "
        "Landing AI Agentic Document Extraction. The Integration Hub's five "
        "connectors are MOCKS -- the calling framework (idempotency, retry, "
        "circuit breaker, redaction, call log) is real, the upstreams are not."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC), name="static")

app.include_router(documents.router)
app.include_router(filing.router)
app.include_router(scheduling.router)
app.include_router(integrations.router)


@app.get("/ui", include_in_schema=False)
def ui() -> FileResponse:
    """The test console. Auth is headers, so the page carries a role switcher."""
    return FileResponse(_STATIC / "index.html")


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/ui")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {
        "status": "healthy",
        "features": {
            "document_management": "implemented",
            "case_filing": "implemented",
            "scheduling": "implemented",
            "integration_hub": "implemented (mock adapters)",
        },
        # Surfaced because upload silently degrades to store-without-text
        # when this is false; an operator should be able to see that.
        "document_extraction_configured": extraction.is_configured(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
