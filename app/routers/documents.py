"""Document Management API.

Secure, searchable document store with version control, digital signatures,
court seal stamping, and access permissions by role and jurisdiction.

Text extraction is Landing AI's Agentic Document Extraction -- see
services/extraction.py for why, and for how upload behaves when it is
unavailable.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.config import ALLOWED_SUFFIXES, MAX_UPLOAD_MB
from app.db import cursor, query_all, query_one
from app.models import (
    AttestationOut,
    DocumentDetail,
    DocumentSummary,
    SearchHit,
    VersionOut,
)
from app.security import Classification, Principal, current_principal
from app.services import extraction, search, seals, storage

router = APIRouter(prefix="/api/documents", tags=["Document Management"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(principal: Principal, document_id: str | None, action: str,
           allowed: bool, detail: str | None = None) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO access_log
               (at, actor_id, actor_role, jurisdiction, document_id, action, allowed, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now(), principal.user_id, principal.role.value, principal.jurisdiction,
             document_id, action, 1 if allowed else 0, detail),
        )


def _deny(principal: Principal, document_id: str | None, action: str,
          message: str) -> None:
    """Log a role-gate refusal, then raise 403.

    `_require_read` already logged that this principal *may see* the
    document; without this, a privileged action they were refused would sit
    in the audit trail as allowed=True, which is the opposite of what
    happened.
    """
    _audit(principal, document_id, action, False, message)
    raise HTTPException(403, message)


def _load_document(document_id: str) -> dict:
    doc = query_one("SELECT * FROM documents WHERE id = ?", (document_id,))
    if not doc:
        raise HTTPException(404, "Unknown document id.")
    return doc


def _require_read(principal: Principal, doc: dict, action: str) -> None:
    allowed = principal.may_read(doc["classification"], doc["jurisdiction"])
    _audit(principal, doc["id"], action, allowed,
           None if allowed else f"role={principal.role.value} "
                                f"jurisdiction={principal.jurisdiction}")
    if not allowed:
        # 404, not 403: confirming a sealed document exists in another
        # jurisdiction is itself a disclosure.
        raise HTTPException(404, "Unknown document id.")


def _latest_version(document_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM document_versions WHERE document_id = ? "
        "ORDER BY version_no DESC LIMIT 1",
        (document_id,),
    )


def _version_out(v: dict) -> VersionOut:
    return VersionOut(
        version_id=v["id"],
        version_no=v["version_no"],
        filename=v["filename"],
        media_type=v["media_type"],
        size_bytes=v["size_bytes"],
        content_hash=v["content_hash"],
        uploaded_at=v["uploaded_at"],
        uploaded_by=v["uploaded_by"],
        note=v["note"],
        extraction_status=v["extraction_status"],
        page_count=v["page_count"],
        element_count=v["element_count"],
        fields=json.loads(v["fields_json"]) if v["fields_json"] else None,
        extraction_error=v["extraction_error"],
        attestations=[
            AttestationOut(**a) for a in seals.for_version(v["id"], v["content_hash"])
        ],
    )


def _ingest(upload: UploadFile) -> tuple[str, int]:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            400, f"Unsupported file type '{suffix}'. Allowed: {sorted(ALLOWED_SUFFIXES)}"
        )
    content_hash, size = storage.store_stream(upload.file)
    if size == 0:
        raise HTTPException(400, "Uploaded file is empty.")
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds the {MAX_UPLOAD_MB}MB limit.")
    return content_hash, size


def _run_extraction(version_id: str, document_id: str, title: str,
                    content_hash: str) -> extraction.ExtractionOutcome:
    outcome = extraction.extract_document(storage.ensure_local(content_hash))
    with cursor() as cur:
        cur.execute(
            """UPDATE document_versions
               SET extraction_status = ?, markdown = ?, page_count = ?,
                   element_count = ?, fields_json = ?, extraction_error = ?
               WHERE id = ?""",
            (outcome.status, outcome.markdown, outcome.page_count,
             outcome.element_count, outcome.fields_json, outcome.error, version_id),
        )
    if outcome.markdown:
        search.index_version(version_id, document_id, title, outcome.markdown)
    return outcome


@router.post("", response_model=DocumentDetail, status_code=201)
def create_document(
    file: UploadFile = File(...),
    title: str = Form(...),
    jurisdiction: str = Form(...),
    case_number: str | None = Form(default=None),
    doc_type: str = Form(default="unclassified"),
    classification: str = Form(default=Classification.RESTRICTED.value),
    note: str | None = Form(default=None),
    principal: Principal = Depends(current_principal),
) -> DocumentDetail:
    """Upload a new document (version 1) and extract its text."""
    if not principal.may_upload:
        _audit(principal, None, "create", False, "role may not upload")
        raise HTTPException(403, f"Role '{principal.role.value}' may not upload documents.")
    try:
        Classification(classification)
    except ValueError:
        raise HTTPException(400, f"classification must be one of "
                                 f"{[c.value for c in Classification]}")

    jurisdiction = principal.jurisdiction
    content_hash, size = _ingest(file)
    document_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    now = _now()

    with cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (id, case_number, title, doc_type, jurisdiction, classification,
                created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (document_id, case_number, title, doc_type, jurisdiction,
             classification, now, principal.user_id),
        )
        cur.execute(
            """INSERT INTO document_versions
               (id, document_id, version_no, filename, media_type, size_bytes,
                content_hash, uploaded_at, uploaded_by, note, extraction_status)
               VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (version_id, document_id, file.filename, file.content_type, size,
             content_hash, now, principal.user_id, note),
        )

    _run_extraction(version_id, document_id, title, content_hash)
    _audit(principal, document_id, "create", True, f"v1 {content_hash[:12]}")
    return get_document(document_id, principal=principal)


@router.post("/{document_id}/versions", response_model=DocumentDetail)
def add_version(
    document_id: str,
    file: UploadFile = File(...),
    note: str | None = Form(default=None),
    principal: Principal = Depends(current_principal),
) -> DocumentDetail:
    """Supersede a document with a new version. Prior versions stay intact."""
    doc = _load_document(document_id)
    _require_read(principal, doc, "add_version")
    if not principal.may_upload:
        _deny(principal, document_id, "add_version",
              f"Role '{principal.role.value}' may not upload documents.")

    content_hash, size = _ingest(file)
    latest = _latest_version(document_id)
    next_no = (latest["version_no"] + 1) if latest else 1
    if latest and latest["content_hash"] == content_hash:
        raise HTTPException(
            409,
            f"Identical to version {latest['version_no']} (same content hash); "
            "nothing to supersede.",
        )

    version_id = str(uuid.uuid4())
    with cursor() as cur:
        cur.execute(
            """INSERT INTO document_versions
               (id, document_id, version_no, filename, media_type, size_bytes,
                content_hash, uploaded_at, uploaded_by, note, extraction_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (version_id, document_id, next_no, file.filename, file.content_type,
             size, content_hash, _now(), principal.user_id, note),
        )
    _run_extraction(version_id, document_id, doc["title"], content_hash)
    _audit(principal, document_id, "add_version", True, f"v{next_no}")
    return get_document(document_id, principal=principal)


@router.get("", response_model=list[DocumentSummary])
def list_documents(
    case_number: str | None = Query(default=None),
    doc_type: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(current_principal),
) -> list[DocumentSummary]:
    clauses, params = [], []
    if case_number:
        clauses.append("d.case_number = ?")
        params.append(case_number)
    if doc_type:
        clauses.append("d.doc_type = ?")
        params.append(doc_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    rows = query_all(
        f"""SELECT d.*,
                   (SELECT COUNT(*) FROM document_versions v WHERE v.document_id = d.id)
                       AS version_count,
                   (SELECT MAX(v.uploaded_at) FROM document_versions v
                        WHERE v.document_id = d.id) AS last_updated
            FROM documents d {where}
            ORDER BY d.created_at DESC LIMIT ?""",
        (*params, limit * 4),
    )
    visible = [
        r for r in rows
        if principal.may_read(r["classification"], r["jurisdiction"])
    ][:limit]
    return [
        DocumentSummary(
            document_id=r["id"], title=r["title"], case_number=r["case_number"],
            doc_type=r["doc_type"], jurisdiction=r["jurisdiction"],
            classification=r["classification"], created_at=r["created_at"],
            created_by=r["created_by"], version_count=r["version_count"],
            last_updated=r["last_updated"],
        )
        for r in visible
    ]


@router.get("/search", response_model=list[SearchHit])
def search_documents(
    q: str = Query(..., min_length=2, description="Full-text query over extracted document text"),
    limit: int = Query(default=25, le=100),
    principal: Principal = Depends(current_principal),
) -> list[SearchHit]:
    """Search the text Landing AI extracted, scoped to what this role may read."""
    hits = search.search(principal, q, limit)
    _audit(principal, None, "search", True, f"q={q!r} hits={len(hits)}")
    return [SearchHit(**h) for h in hits]


@router.get("/{document_id}", response_model=DocumentDetail)
def get_document(
    document_id: str,
    principal: Principal = Depends(current_principal),
) -> DocumentDetail:
    doc = _load_document(document_id)
    _require_read(principal, doc, "read")
    versions = query_all(
        "SELECT * FROM document_versions WHERE document_id = ? ORDER BY version_no",
        (document_id,),
    )
    return DocumentDetail(
        document_id=doc["id"], title=doc["title"], case_number=doc["case_number"],
        doc_type=doc["doc_type"], jurisdiction=doc["jurisdiction"],
        classification=doc["classification"], created_at=doc["created_at"],
        created_by=doc["created_by"],
        versions=[_version_out(v) for v in versions],
    )


@router.get("/{document_id}/content")
def download(
    document_id: str,
    version: int | None = Query(default=None, description="Defaults to the latest version"),
    principal: Principal = Depends(current_principal),
):
    """Serve the original file bytes for one version."""
    doc = _load_document(document_id)
    _require_read(principal, doc, "download")
    row = (
        query_one(
            "SELECT * FROM document_versions WHERE document_id = ? AND version_no = ?",
            (document_id, version),
        )
        if version is not None
        else _latest_version(document_id)
    )
    if not row:
        raise HTTPException(404, "Unknown version.")
    path = storage.ensure_local(row["content_hash"])
    if not path.exists():
        raise HTTPException(410, "Stored content is missing from the blob store.")
    return FileResponse(
        path,
        media_type=row["media_type"] or "application/octet-stream",
        filename=row["filename"],
    )


@router.get("/{document_id}/text")
def get_text(
    document_id: str,
    version: int | None = Query(default=None),
    principal: Principal = Depends(current_principal),
) -> dict:
    """The Markdown Landing AI produced -- the searchable representation."""
    doc = _load_document(document_id)
    _require_read(principal, doc, "read_text")
    row = (
        query_one(
            "SELECT * FROM document_versions WHERE document_id = ? AND version_no = ?",
            (document_id, version),
        )
        if version is not None
        else _latest_version(document_id)
    )
    if not row:
        raise HTTPException(404, "Unknown version.")
    return {
        "document_id": document_id,
        "version_no": row["version_no"],
        "extraction_status": row["extraction_status"],
        "markdown": row["markdown"],
        "fields": json.loads(row["fields_json"]) if row["fields_json"] else None,
        "extraction_error": row["extraction_error"],
    }


@router.post("/{document_id}/versions/{version_no}/reextract", response_model=VersionOut)
def reextract(
    document_id: str,
    version_no: int,
    principal: Principal = Depends(current_principal),
) -> VersionOut:
    """Retry Landing AI against an already-stored version.

    The path for documents uploaded while the API key was missing or the
    service was down -- the bytes are already here, only the text is absent.
    """
    doc = _load_document(document_id)
    _require_read(principal, doc, "reextract")
    if not principal.may_upload:
        _deny(principal, document_id, "reextract",
              f"Role '{principal.role.value}' may not trigger extraction.")
    row = query_one(
        "SELECT * FROM document_versions WHERE document_id = ? AND version_no = ?",
        (document_id, version_no),
    )
    if not row:
        raise HTTPException(404, "Unknown version.")
    if not extraction.is_configured():
        raise HTTPException(503, "VISION_AGENT_API_KEY is not configured on this server.")

    _run_extraction(row["id"], document_id, doc["title"], row["content_hash"])
    refreshed = query_one("SELECT * FROM document_versions WHERE id = ?", (row["id"],))
    return _version_out(refreshed)


@router.post("/{document_id}/versions/{version_no}/sign", response_model=AttestationOut)
def sign_version(
    document_id: str,
    version_no: int,
    reason: str | None = Form(default=None),
    principal: Principal = Depends(current_principal),
) -> AttestationOut:
    """Digitally sign one version, binding the signature to its content hash."""
    doc = _load_document(document_id)
    _require_read(principal, doc, "sign")
    if not principal.may_sign:
        _deny(principal, document_id, "sign",
              f"Role '{principal.role.value}' may not sign documents.")
    row = query_one(
        "SELECT * FROM document_versions WHERE document_id = ? AND version_no = ?",
        (document_id, version_no),
    )
    if not row:
        raise HTTPException(404, "Unknown version.")

    rec = seals.attest(
        version_id=row["id"], content_hash=row["content_hash"], kind="signature",
        actor_id=principal.user_id, actor_role=principal.role.value, reason=reason,
    )
    _audit(principal, document_id, "sign", True, f"v{version_no}")
    return AttestationOut(**seals.verify_attestation(rec, row["content_hash"]))


@router.post("/{document_id}/versions/{version_no}/seal", response_model=AttestationOut)
def seal_version(
    document_id: str,
    version_no: int,
    reason: str | None = Form(default=None),
    principal: Principal = Depends(current_principal),
) -> AttestationOut:
    """Apply the court seal to one version, issuing a quotable seal number."""
    doc = _load_document(document_id)
    _require_read(principal, doc, "seal")
    if not principal.may_seal:
        _deny(principal, document_id, "seal",
              f"Role '{principal.role.value}' may not apply the court seal.")
    row = query_one(
        "SELECT * FROM document_versions WHERE document_id = ? AND version_no = ?",
        (document_id, version_no),
    )
    if not row:
        raise HTTPException(404, "Unknown version.")

    rec = seals.attest(
        version_id=row["id"], content_hash=row["content_hash"], kind="seal",
        actor_id=principal.user_id, actor_role=principal.role.value, reason=reason,
    )
    _audit(principal, document_id, "seal", True,
           f"v{version_no} seal={rec['seal_number']}")
    return AttestationOut(**seals.verify_attestation(rec, row["content_hash"]))


@router.get("/{document_id}/integrity")
def integrity(
    document_id: str,
    principal: Principal = Depends(current_principal),
) -> dict:
    """Re-hash every stored version and re-verify every attestation."""
    doc = _load_document(document_id)
    _require_read(principal, doc, "integrity")
    versions = query_all(
        "SELECT * FROM document_versions WHERE document_id = ? ORDER BY version_no",
        (document_id,),
    )
    report = []
    for v in versions:
        # Re-hash once per version, then let every attestation on it be
        # judged against that result rather than re-reading the file.
        bytes_ok = storage.verify(v["content_hash"])
        attestations = seals.for_version(
            v["id"], v["content_hash"], blob_verified=bytes_ok
        )
        report.append({
            "version_no": v["version_no"],
            "content_hash": v["content_hash"],
            "blob_present": storage.ensure_local(v["content_hash"]).exists(),
            "bytes_match_hash": bytes_ok,
            "attestations": [
                {"kind": a["kind"], "actor_id": a["actor_id"],
                 "seal_number": a["seal_number"], "valid": a["valid"],
                 "signature_valid": a["signature_valid"],
                 "covers_current_content": a["covers_current_content"],
                 "blob_verified": a["blob_verified"]}
                for a in attestations
            ],
        })
    return {
        "document_id": document_id,
        "intact": all(v["bytes_match_hash"] for v in report),
        "versions": report,
    }


@router.get("/{document_id}/audit")
def audit_trail(
    document_id: str,
    limit: int = Query(default=100, le=500),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Who accessed this document, when, and whether they were allowed."""
    doc = _load_document(document_id)
    _require_read(principal, doc, "audit")
    if principal.role.value not in ("judge", "registrar"):
        _deny(principal, document_id, "audit",
              "Only a judge or registrar may read the audit trail.")
    return {
        "document_id": document_id,
        "entries": query_all(
            "SELECT at, actor_id, actor_role, jurisdiction, action, allowed, detail "
            "FROM access_log WHERE document_id = ? ORDER BY id DESC LIMIT ?",
            (document_id, limit),
        ),
    }
