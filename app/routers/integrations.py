"""Integration Hub.

Connectors for prison systems, payment gateways, national identity
registers, legal aid authorities and bar association directories.

Every adapter is a MOCK -- there are no real upstream systems or
credentials here, and each response carries `"mock": true` so synthetic
data can never be mistaken for an upstream answer. What *is* real is the
framework around them: idempotency, retry, circuit breaking, redaction and
the outbound call log. See services/connectors.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

from app.db import query_all
from app.security import Principal, current_principal
from app.services import connectors

router = APIRouter(prefix="/api/integrations", tags=["Integration Hub"])

# Which roles may reach which connector. A clerk settling a fee is ordinary;
# a clerk querying the national identity register is not.
_ALLOWED_ROLES = {
    "prison": {"judge", "registrar", "clerk", "prison_officer"},
    "payments": {"registrar", "clerk", "advocate", "litigant"},
    "identity": {"registrar", "clerk"},
    "legal_aid": {"registrar", "clerk", "advocate", "litigant"},
    "bar_directory": {"registrar", "clerk"},
}


@router.get("")
def list_connectors(principal: Principal = Depends(current_principal)) -> dict:
    """The registry, with live circuit-breaker state."""
    return {
        "implementation_note": (
            "All adapters are mocks. The calling framework (idempotency, "
            "retry, circuit breaker, redaction, call log) is real."
        ),
        "connectors": [
            {**c, "allowed_roles": sorted(_ALLOWED_ROLES.get(c["connector_id"], []))}
            for c in connectors.health()
        ],
    }


@router.post("/{connector_id}/invoke")
def invoke(
    connector_id: str,
    body: dict = Body(default={}),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Call one connector operation.

    Pass an `Idempotency-Key` header to make the call safe to repeat: a key
    that already succeeded returns the stored response instead of calling
    again. A double-charged court fee or a duplicate produce warrant are
    both serious enough to make this the default expectation, not an
    optimisation.
    """
    if connector_id not in connectors.CONNECTORS:
        raise HTTPException(
            404, f"Unknown connector. Known: {sorted(connectors.CONNECTORS)}")

    allowed = _ALLOWED_ROLES.get(connector_id, set())
    if principal.role.value not in allowed:
        raise HTTPException(
            403,
            f"Role '{principal.role.value}' may not use the '{connector_id}' "
            f"connector. Permitted: {sorted(allowed)}")

    operation = body.get("operation")
    if not operation:
        raise HTTPException(
            400,
            f"'operation' is required. Available for '{connector_id}': "
            f"{list(connectors.CONNECTORS[connector_id].operations)}")

    try:
        return connectors.invoke(
            connector_id=connector_id,
            operation=operation,
            payload=body.get("payload") or {},
            caller=principal.user_id,
            owner_user_id=principal.user_id,
            idempotency_key=idempotency_key,
            _fail_injection=bool(body.get("_fail_injection")),
        )
    except connectors.CircuitOpen as exc:
        # 503 + Retry-After: the upstream is down, the request is not wrong.
        raise HTTPException(503, str(exc))
    except connectors.ConnectorError as exc:
        raise HTTPException(502, str(exc))


@router.get("/calls")
def call_log(
    connector_id: str | None = Query(default=None),
    limit: int = Query(default=50, le=200),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Outbound call log, kept separate from the document access log."""
    if principal.role.value not in ("judge", "registrar"):
        raise HTTPException(
            403, "Only a judge or registrar may read the outbound call log.")
    sql = ("SELECT idempotency_key, connector_id, operation, status, attempts, "
           "called_at, called_by, duration_ms, request_json "
           "FROM connector_calls")
    params: tuple = ()
    if connector_id:
        sql += " WHERE connector_id = ?"
        params = (connector_id,)
    sql += " ORDER BY called_at DESC LIMIT ?"
    params = (*params, limit)
    return {"calls": query_all(sql, params),
            "note": "Identity numbers and card data are redacted before logging."}
