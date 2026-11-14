"""Case Initiation & Filing.

Accept filings electronically, calculate fees, validate documents, assign a
case number, and confirm to all parties.

Order matters here and is deliberate: validate -> compute fee -> allocate
number. A filing that fails scrutiny never reaches the allocator, so it
never burns a case number (see services/casenumbers.py on why gaps in a
court register are a problem, not a cosmetic issue).
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.config import COURT_ID
from app.db import cursor, query_all, query_one
from app.security import Principal, current_principal
from app.services import casenumbers, connectors, fees, validation

router = APIRouter(prefix="/api/filing", tags=["Case Initiation & Filing"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{field} must be YYYY-MM-DD, got {value!r}")


@router.get("/case-types")
def case_types() -> dict:
    """Accepted case types, with what each requires."""
    return {
        "case_types": [
            {
                "case_type": ct,
                "required_documents": validation.REQUIRED_DOCUMENTS[ct],
                "limitation_days": validation.LIMITATION_DAYS.get(ct),
                "next_number": casenumbers.peek(COURT_ID, ct),
            }
            for ct in sorted(validation.REQUIRED_DOCUMENTS)
        ],
        "exemptions": fees.EXEMPTIONS,
    }


@router.post("/fees/calculate")
def calculate_fees(
    body: dict = Body(...),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Compute court fees for a prospective filing, with a line breakdown."""
    try:
        return fees.calculate(
            case_type=body.get("case_type", ""),
            claim_value=body.get("claim_value"),
            filing_date=_parse_date(body.get("filing_date"), "filing_date"),
            exemptions=body.get("exemptions") or [],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/validate")
def validate_filing(
    body: dict = Body(...),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Pre-flight a filing without submitting it.

    Returns every objection at once rather than the first -- four defects
    should mean one resubmission, not four.
    """
    objections = validation.validate(
        case_type=body.get("case_type", ""),
        parties=body.get("parties") or [],
        attached_roles=[d.get("role", "") for d in (body.get("documents") or [])],
        cause_of_action_date=_parse_date(
            body.get("cause_of_action_date"), "cause_of_action_date"),
        filing_date=_parse_date(body.get("filing_date"), "filing_date"),
        filed_by_role=principal.role.value,
    )
    return {"clean": not objections, "objections": objections}


@router.post("", status_code=201)
def submit_filing(
    body: dict = Body(...),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Submit a filing. Accepted filings get a case number; rejected ones don't.

    A rejected filing is still recorded, with its objections, rather than
    discarded -- the attempt and its date are part of the record.
    """
    if not principal.may_upload:
        raise HTTPException(
            403, f"Role '{principal.role.value}' may not submit filings.")

    case_type = body.get("case_type", "")
    title = (body.get("title") or "").strip()
    parties = body.get("parties") or []
    documents = body.get("documents") or []
    jurisdiction = body.get("jurisdiction") or principal.jurisdiction
    coa = _parse_date(body.get("cause_of_action_date"), "cause_of_action_date")
    filing_date = _parse_date(body.get("filing_date"), "filing_date") or date.today()

    if not title:
        raise HTTPException(400, "title is required.")

    # Every attached document must actually exist and be in this jurisdiction.
    for d in documents:
        doc = query_one("SELECT * FROM documents WHERE id = ?",
                        (d.get("document_id"),))
        if not doc:
            raise HTTPException(
                400, f"Unknown document_id {d.get('document_id')!r}.")
        if doc["jurisdiction"] != jurisdiction:
            raise HTTPException(
                400,
                f"Document {doc['id']} belongs to {doc['jurisdiction']}, "
                f"not {jurisdiction}.")

    objections = validation.validate(
        case_type=case_type,
        parties=parties,
        attached_roles=[d.get("role", "") for d in documents],
        cause_of_action_date=coa,
        filing_date=filing_date,
        filed_by_role=principal.role.value,
    )

    try:
        fee = fees.calculate(
            case_type=case_type,
            claim_value=body.get("claim_value"),
            filing_date=filing_date,
            exemptions=body.get("exemptions") or [],
        )
    except ValueError as exc:
        # An unknown case type already shows up as an objection; keep the
        # filing rejectable rather than 400-ing the whole request.
        fee = None
        objections.append({
            "code": "fee_not_calculable", "message": str(exc),
            "remedy": "Correct the case type or claim value.",
        })

    filing_id = str(uuid.uuid4())
    accepted = not objections
    case_id: str | None = None
    case_number: str | None = None

    with cursor() as cur:
        if accepted:
            case_id = str(uuid.uuid4())
            # Allocated inside this same transaction -- see casenumbers.py.
            case_number = casenumbers.allocate(
                cur, court_id=COURT_ID, case_type=case_type, on=filing_date)
            cur.execute(
                """INSERT INTO cases
                   (id, case_number, case_type, court_id, jurisdiction, title,
                    status, filed_at, filed_by, cause_of_action_date)
                   VALUES (?, ?, ?, ?, ?, ?, 'filed', ?, ?, ?)""",
                (case_id, case_number, case_type, COURT_ID, jurisdiction, title,
                 _now(), principal.user_id, coa.isoformat() if coa else None))
            for p in parties:
                cur.execute(
                    """INSERT INTO parties
                       (id, case_id, side, name, advocate, email, phone, in_custody)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), case_id, p.get("side"), p.get("name"),
                     p.get("advocate"), p.get("email"), p.get("phone"),
                     1 if p.get("in_custody") else 0))

        cur.execute(
            """INSERT INTO filings
               (id, case_id, case_type, court_id, jurisdiction, title, status,
                fee_amount, fee_currency, fee_breakdown, objections,
                submitted_at, submitted_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (filing_id, case_id, case_type, COURT_ID, jurisdiction, title,
             "accepted" if accepted else "rejected",
             fee["total"] if fee else None,
             fee["currency"] if fee else None,
             json.dumps(fee) if fee else None,
             json.dumps(objections), _now(), principal.user_id))

        if accepted:
            for d in documents:
                cur.execute(
                    "INSERT OR IGNORE INTO filing_documents "
                    "(filing_id, document_id, role) VALUES (?, ?, ?)",
                    (filing_id, d["document_id"], d.get("role", "")))

    # Confirmation to all parties. Queued through the (mock) notification
    # connectors rather than claimed as sent -- see services/connectors.py.
    confirmations = []
    if accepted:
        for p in parties:
            dest = p.get("email") or p.get("phone")
            confirmations.append({
                "party": p.get("name"),
                "destination": dest,
                "channel": "email" if p.get("email") else
                           ("sms" if p.get("phone") else None),
                "status": "queued" if dest else
                          ("served_through_prison" if p.get("in_custody")
                           else "no_contact_on_record"),
            })

    return {
        "filing_id": filing_id,
        "status": "accepted" if accepted else "rejected",
        "case_id": case_id,
        "case_number": case_number,
        "objections": objections,
        "fee": fee,
        "confirmations": confirmations,
        "note": None if accepted else
                "No case number was allocated; rejected filings do not consume "
                "one. Correct the objections and resubmit.",
    }


@router.get("/{filing_id}")
def get_filing(
    filing_id: str,
    principal: Principal = Depends(current_principal),
) -> dict:
    f = query_one("SELECT * FROM filings WHERE id = ?", (filing_id,))
    if not f:
        raise HTTPException(404, "Unknown filing id.")
    if f["jurisdiction"] != principal.jurisdiction and \
            principal.role.value != "registrar":
        raise HTTPException(404, "Unknown filing id.")
    return {
        **f,
        "objections": json.loads(f["objections"] or "[]"),
        "fee_breakdown": json.loads(f["fee_breakdown"] or "null"),
        "documents": query_all(
            """SELECT fd.role, d.id AS document_id, d.title, d.doc_type
               FROM filing_documents fd JOIN documents d ON d.id = fd.document_id
               WHERE fd.filing_id = ?""", (filing_id,)),
    }


@router.get("/cases/{case_id}")
def get_case(
    case_id: str,
    principal: Principal = Depends(current_principal),
) -> dict:
    c = query_one("SELECT * FROM cases WHERE id = ?", (case_id,))
    if not c:
        raise HTTPException(404, "Unknown case id.")
    if c["jurisdiction"] != principal.jurisdiction and \
            principal.role.value != "registrar":
        raise HTTPException(404, "Unknown case id.")
    return {
        **c,
        "parties": query_all(
            "SELECT side, name, advocate, email, phone, in_custody "
            "FROM parties WHERE case_id = ?", (case_id,)),
        "hearings": query_all(
            """SELECT h.id, h.starts_at, h.ends_at, h.status, h.purpose,
                      j.name AS judge_name, r.name AS courtroom_name
               FROM hearings h
               JOIN judges j ON j.id = h.judge_id
               JOIN courtrooms r ON r.id = h.courtroom_id
               WHERE h.case_id = ? ORDER BY h.starts_at""", (case_id,)),
    }


@router.get("")
def list_cases(
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(current_principal),
) -> dict:
    rows = query_all(
        "SELECT * FROM cases ORDER BY filed_at DESC LIMIT ?", (limit * 4,))
    visible = [
        r for r in rows
        if r["jurisdiction"] == principal.jurisdiction
        or principal.role.value == "registrar"
    ][:limit]
    return {"cases": visible}
