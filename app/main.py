from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import CORS_ORIGINS, HOST, PORT
from app.db import init_db
from app.routers import auth, documents, filing, integrations, scheduling
from app.services import extraction

app = FastAPI(
    title="Court Case Management System",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://yournyaya.vercel.app/"],
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
app.include_router(auth.router)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


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
        "document_extraction_configured": extraction.is_configured(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=True)
