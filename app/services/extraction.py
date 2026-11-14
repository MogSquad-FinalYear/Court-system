"""Landing AI Agentic Document Extraction (ADE).

Two-step pipeline, matching the SDK:

  1. parse()   -> document as Markdown, plus a structure tree of pages and
                  positioned elements. The Markdown is the searchable text;
                  it is what goes into the FTS index.
  2. extract() -> typed fields pulled out of that Markdown against a schema.

Why ADE and not plain OCR: court filings are tables, stamps, annexures and
multi-column cause lists. Tesseract flattens all of that into a stream of
words with the structure destroyed; ADE returns Markdown that preserves
tables and reading order, with each element carrying its own page and bounding
box, which is what makes "show me where in the document this came from"
possible at all.

Degradation is deliberate: with no API key, or when the API errors, upload
still stores and versions the file. The version row records
extraction_status='skipped'|'failed' and the reason. Extraction can then be
retried later against the stored blob -- see routers/documents.py:reextract.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.config import (
    LANDING_AI_ENVIRONMENT,
    LANDING_AI_TIMEOUT_S,
    VISION_AGENT_API_KEY,
)


class CourtDocumentFields(BaseModel):
    """Schema handed to ADE's extract() for any court filing.

    Every field is Optional because this one schema is applied to petitions,
    affidavits, orders, charge sheets and annexures alike -- a field that
    does not exist in the document should come back null, not be invented.
    The descriptions are the actual extraction prompt, so they are written
    for the model, not for a developer reading the class.
    """

    case_number: str | None = Field(
        default=None,
        description="The case or cause number exactly as printed, including "
                    "the year and case-type prefix (e.g. 'CRL.A. 1184/2024').",
    )
    court_name: str | None = Field(
        default=None, description="Full name of the court the document is addressed to or issued by."
    )
    document_title: str | None = Field(
        default=None, description="The document's own title or heading, e.g. 'Petition under Section 482 CrPC'."
    )
    filing_date: str | None = Field(
        default=None, description="Date the document was filed, signed or issued, as printed on the document."
    )
    petitioners: list[str] = Field(
        default_factory=list, description="Names of the petitioners, applicants, plaintiffs or complainants."
    )
    respondents: list[str] = Field(
        default_factory=list, description="Names of the respondents, defendants or accused."
    )
    advocates: list[str] = Field(
        default_factory=list, description="Names of advocates or counsel on record named in the document."
    )
    statutes_cited: list[str] = Field(
        default_factory=list,
        description="Statutory provisions cited, as printed (e.g. 'Section 420 IPC', 'Article 226').",
    )
    relief_sought: str | None = Field(
        default=None, description="A one-sentence summary of the relief, order or prayer being sought."
    )
    hearing_date: str | None = Field(
        default=None, description="Any next hearing or listing date stated in the document."
    )


@dataclass
class ExtractionOutcome:
    """What the document store records about one extraction attempt."""

    status: str                      # 'extracted' | 'skipped' | 'failed'
    markdown: str | None = None
    page_count: int | None = None
    element_count: int | None = None   # structural elements ADE identified
    fields: dict[str, Any] | None = None
    error: str | None = None

    @property
    def fields_json(self) -> str | None:
        return json.dumps(self.fields) if self.fields is not None else None


_client = None


def _get_client():
    """Lazily build the SDK client so import never depends on a key."""
    global _client
    if _client is None:
        from landingai_ade import LandingAIADE

        _client = LandingAIADE(
            apikey=VISION_AGENT_API_KEY,
            environment=LANDING_AI_ENVIRONMENT,
        )
    return _client


def is_configured() -> bool:
    return bool(VISION_AGENT_API_KEY)


def extract_document(path: Path, *, with_fields: bool = True) -> ExtractionOutcome:
    """Run parse (+ optional extract) over one stored document.

    Never raises: a Landing AI failure must not cost the caller a file they
    already uploaded, so the failure is returned as data for the version row.
    """
    if not is_configured():
        return ExtractionOutcome(
            status="skipped",
            error="VISION_AGENT_API_KEY is not set; stored without extraction.",
        )

    from landingai_ade import LandingAIADE  # noqa: F401  (import cost only when used)

    try:
        client = _get_client()
        with path.open("rb") as fh:
            parsed = client.v2.parse(
                document=fh,
                timeout=LANDING_AI_TIMEOUT_S,
            )
    except Exception as exc:  # SDK raises its own APIError subclasses
        return ExtractionOutcome(status="failed", error=f"parse failed: {exc}")

    markdown = parsed.markdown or ""
    meta = parsed.metadata
    pages = parsed.structure.children if parsed.structure else []
    outcome = ExtractionOutcome(
        status="extracted",
        markdown=markdown,
        # page_count from metadata where the gateway reports it, else from
        # the structure tree, which always has one node per page.
        page_count=(getattr(meta, "page_count", None) or len(pages) or None),
        element_count=sum(len(p.children or []) for p in pages) or None,
    )

    # A page ADE could not read is a real gap in a court record, not a
    # detail -- surface it rather than letting a short Markdown look clean.
    failed_pages = getattr(meta, "failed_pages", None)
    if failed_pages:
        outcome.error = f"pages not parsed: {failed_pages}"

    if not with_fields or not markdown.strip():
        return outcome

    try:
        extracted = client.v2.extract(
            schema=CourtDocumentFields,
            markdown=markdown,
            timeout=LANDING_AI_TIMEOUT_S,
        )
        # `extraction` is already Dict[str, object] on V2ExtractResult.
        outcome.fields = dict(extracted.extraction or {})

        notes = []
        if extracted.schema_violation_error:
            notes.append(f"schema violation: {extracted.schema_violation_error}")
        for w in extracted.warnings or []:
            notes.append(f"{w.get('code', 'warning')}: {w.get('msg', w)}")
        if notes:
            outcome.error = "; ".join(filter(None, [outcome.error, *notes]))
    except Exception as exc:
        # Parse succeeded, so keep the Markdown -- it is the searchable
        # artefact and the more valuable half. Only note the field failure.
        outcome.error = "; ".join(filter(None, [
            outcome.error, f"field extraction failed (markdown retained): {exc}"
        ]))

    return outcome
