"""API response models."""
from typing import Any

from pydantic import BaseModel


class AttestationOut(BaseModel):
    id: str
    version_id: str
    kind: str                       # 'signature' | 'seal'
    actor_id: str
    actor_role: str
    court_id: str | None = None
    seal_number: str | None = None
    content_hash: str
    signature: str
    created_at: str
    reason: str | None = None
    # Recomputed on every read, never stored:
    signature_valid: bool
    covers_current_content: bool
    # None = the stored bytes were not re-hashed on this request (too costly
    # for a plain read); True/False only from the integrity endpoint.
    blob_verified: bool | None = None
    valid: bool


class VersionOut(BaseModel):
    version_id: str
    version_no: int
    filename: str
    media_type: str | None = None
    size_bytes: int
    content_hash: str
    uploaded_at: str
    uploaded_by: str
    note: str | None = None
    extraction_status: str          # pending | extracted | skipped | failed
    page_count: int | None = None
    element_count: int | None = None
    fields: dict[str, Any] | None = None
    extraction_error: str | None = None
    attestations: list[AttestationOut] = []


class DocumentSummary(BaseModel):
    document_id: str
    title: str
    case_number: str | None = None
    doc_type: str
    jurisdiction: str
    classification: str
    created_at: str
    created_by: str
    version_count: int
    last_updated: str | None = None


class DocumentDetail(BaseModel):
    document_id: str
    title: str
    case_number: str | None = None
    doc_type: str
    jurisdiction: str
    classification: str
    created_at: str
    created_by: str
    versions: list[VersionOut] = []


class SearchHit(BaseModel):
    version_id: str
    document_id: str
    title: str
    case_number: str | None = None
    doc_type: str
    jurisdiction: str
    classification: str
    version_no: int
    uploaded_at: str
    excerpt: str
    rank: float
