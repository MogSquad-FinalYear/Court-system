"""Integration Hub.

WHAT IS REAL HERE AND WHAT IS NOT
---------------------------------
Real: the calling framework. Idempotency (a repeated key returns the stored
response instead of calling again), retry with backoff, a circuit breaker
per connector, an outbound call log kept separate from the document access
log, and per-connector operation contracts.

Mock: every upstream. There is no prison management system, payment
gateway, identity register, legal aid authority or bar directory to talk
to, so each adapter returns deterministic synthetic data and says so in its
response (`"mock": true`). Swapping in a real integration means replacing
one adapter function; nothing in the framework changes.

This is the honest ceiling for this feature without real endpoints and
credentials. A connector that fabricated a custody confirmation or a
payment receipt and did *not* mark itself as mock would be worse than no
connector at all.
"""
from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.db import cursor, query_one

# Breaker: after this many consecutive failures, stop calling for the
# cooldown period so one dead upstream cannot stall filing for everyone.
FAIL_THRESHOLD = 3
COOLDOWN = timedelta(minutes=2)
MAX_ATTEMPTS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConnectorError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class CircuitOpen(RuntimeError):
    pass


# --------------------------------------------------------------------
# Mock adapters. Each returns ("mock": True) so a caller can never mistake
# synthetic data for an upstream answer.
# --------------------------------------------------------------------

def _mock_prison(operation: str, payload: dict) -> dict:
    if operation == "custody_status":
        pid = str(payload.get("party_id", ""))
        # Deterministic from the id so repeated calls agree with each other.
        in_custody = int(hashlib.sha256(pid.encode()).hexdigest(), 16) % 2 == 0
        return {
            "mock": True, "party_id": pid, "in_custody": in_custody,
            "facility": "Central Prison, Bengaluru" if in_custody else None,
            "video_link_available": in_custody,
        }
    if operation == "produce_warrant":
        return {
            "mock": True, "acknowledged": True,
            "warrant_id": f"PW-{uuid.uuid4().hex[:8].upper()}",
            "produce_on": payload.get("produce_on"),
        }
    raise ConnectorError(f"prison: unknown operation '{operation}'", retryable=False)


def _mock_payments(operation: str, payload: dict) -> dict:
    if operation == "collect":
        amount = float(payload.get("amount", 0))
        if amount <= 0:
            raise ConnectorError("payments: amount must be positive", retryable=False)
        return {
            "mock": True, "status": "captured", "amount": amount,
            "currency": payload.get("currency", "INR"),
            "receipt_no": f"RCPT-{uuid.uuid4().hex[:10].upper()}",
            "head_of_account": "0070-Other Administrative Services",
        }
    if operation == "refund":
        return {
            "mock": True, "status": "refunded",
            "amount": payload.get("amount"),
            "refund_no": f"RFND-{uuid.uuid4().hex[:10].upper()}",
        }
    raise ConnectorError(f"payments: unknown operation '{operation}'", retryable=False)


def _mock_identity(operation: str, payload: dict) -> dict:
    """Verify-only by design.

    The identity number is hashed on the way in and never stored or echoed.
    Returning it, or persisting it in the call log, would turn this system
    into a copy of the national identity database, which is exactly what
    data-minimisation rules exist to prevent.
    """
    if operation != "verify":
        raise ConnectorError(f"identity: unknown operation '{operation}'", retryable=False)
    number = str(payload.get("identity_number", ""))
    if len(number) < 8:
        raise ConnectorError("identity: malformed identity number", retryable=False)
    digest = hashlib.sha256(number.encode()).hexdigest()
    return {
        "mock": True,
        "verified": int(digest, 16) % 10 != 0,   # ~90% verify
        "name_match": payload.get("name") is not None,
        "identity_hash": digest[:16],            # never the number itself
        "note": "identity number is hashed on receipt and never stored",
    }


def _mock_legal_aid(operation: str, payload: dict) -> dict:
    if operation == "check_eligibility":
        income = float(payload.get("annual_income", 0))
        eligible = income <= 300_000
        return {
            "mock": True, "eligible": eligible,
            "ground": "income below statutory ceiling" if eligible else None,
            "reference": f"LSA-{uuid.uuid4().hex[:8].upper()}" if eligible else None,
        }
    if operation == "assign_counsel":
        return {
            "mock": True, "assigned": True,
            "counsel_name": "Smt. Latha Narayan (panel advocate)",
            "assignment_id": f"LA-{uuid.uuid4().hex[:8].upper()}",
        }
    raise ConnectorError(f"legal_aid: unknown operation '{operation}'", retryable=False)


def _mock_bar_directory(operation: str, payload: dict) -> dict:
    if operation != "verify_enrolment":
        raise ConnectorError(
            f"bar_directory: unknown operation '{operation}'", retryable=False)
    enrolment = str(payload.get("enrolment_number", ""))
    if not enrolment:
        raise ConnectorError("bar_directory: enrolment_number required",
                             retryable=False)
    return {
        "mock": True,
        "enrolled": True,
        "name": payload.get("name"),
        "enrolment_number": enrolment,
        "state_bar_council": "Karnataka",
        "standing": "active",
    }


@dataclass(frozen=True)
class Connector:
    id: str
    name: str
    purpose: str
    operations: tuple[str, ...]
    adapter: Callable[[str, dict], dict]


CONNECTORS: dict[str, Connector] = {
    c.id: c for c in [
        Connector("prison", "Prison management system",
                  "Custody status, produce-warrant acknowledgement, video-link "
                  "appearance booking for in-custody accused.",
                  ("custody_status", "produce_warrant"), _mock_prison),
        Connector("payments", "Payment gateway",
                  "Court fee collection, refunds on rejected filings, treasury "
                  "reconciliation.",
                  ("collect", "refund"), _mock_payments),
        Connector("identity", "National identity register",
                  "Party identity verification at filing. Verify-only: the "
                  "identity number is hashed on receipt and never stored.",
                  ("verify",), _mock_identity),
        Connector("legal_aid", "Legal aid authority",
                  "Fee-exemption eligibility and assignment of panel counsel "
                  "to unrepresented parties.",
                  ("check_eligibility", "assign_counsel"), _mock_legal_aid),
        Connector("bar_directory", "Bar association directory",
                  "Enrolment verification for advocates filing on a party's "
                  "behalf; the authority behind the 'advocate' role.",
                  ("verify_enrolment",), _mock_bar_directory),
    ]
}


# --------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------

def _breaker_state(connector_id: str) -> dict:
    row = query_one(
        "SELECT * FROM connector_health WHERE connector_id = ?", (connector_id,))
    if row:
        return row
    with cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO connector_health (connector_id, last_checked) "
            "VALUES (?, ?)", (connector_id, _now().isoformat()))
    return {"connector_id": connector_id, "consecutive_fails": 0,
            "opened_at": None, "last_checked": _now().isoformat()}


def _record_result(connector_id: str, ok: bool) -> None:
    state = _breaker_state(connector_id)
    fails = 0 if ok else state["consecutive_fails"] + 1
    opened_at = None
    if not ok and fails >= FAIL_THRESHOLD:
        opened_at = _now().isoformat()
    with cursor() as cur:
        cur.execute(
            """UPDATE connector_health
               SET consecutive_fails = ?, opened_at = ?, last_checked = ?
               WHERE connector_id = ?""",
            (fails, opened_at, _now().isoformat(), connector_id))


def breaker_open(connector_id: str) -> bool:
    state = _breaker_state(connector_id)
    if not state["opened_at"]:
        return False
    if _now() - datetime.fromisoformat(state["opened_at"]) > COOLDOWN:
        # Cooldown elapsed -- half-open: let the next call through.
        with cursor() as cur:
            cur.execute(
                "UPDATE connector_health SET opened_at = NULL, "
                "consecutive_fails = 0 WHERE connector_id = ?", (connector_id,))
        return False
    return True


def health() -> list[dict]:
    out = []
    for cid, c in CONNECTORS.items():
        st = _breaker_state(cid)
        out.append({
            "connector_id": cid, "name": c.name,
            "operations": list(c.operations),
            "implementation": "mock",
            "circuit": "open" if breaker_open(cid) else "closed",
            "consecutive_fails": st["consecutive_fails"],
        })
    return out


# --------------------------------------------------------------------
# Invocation
# --------------------------------------------------------------------

def _redact(connector_id: str, payload: dict) -> dict:
    """Never persist secrets or identity numbers in the call log."""
    redacted = dict(payload)
    for key in ("identity_number", "card_number", "cvv", "password", "otp"):
        if key in redacted:
            redacted[key] = "***redacted***"
    return redacted


def invoke(
    *,
    connector_id: str,
    operation: str,
    payload: dict,
    caller: str,
    idempotency_key: str | None = None,
    _fail_injection: bool = False,
) -> dict:
    """Call one connector, with idempotency, retry and circuit breaking."""
    connector = CONNECTORS.get(connector_id)
    if connector is None:
        raise ConnectorError(f"Unknown connector '{connector_id}'", retryable=False)
    if operation not in connector.operations:
        raise ConnectorError(
            f"{connector_id}: unknown operation '{operation}'. "
            f"Known: {list(connector.operations)}", retryable=False)

    key = idempotency_key or str(uuid.uuid4())
    prior = query_one(
        "SELECT * FROM connector_calls WHERE idempotency_key = ?", (key,))
    if prior and prior["status"] == "ok":
        return {
            "connector_id": connector_id, "operation": operation,
            "idempotency_key": key, "replayed": True,
            "result": json.loads(prior["response_json"] or "{}"),
        }

    if breaker_open(connector_id):
        raise CircuitOpen(
            f"Circuit breaker open for '{connector_id}' after "
            f"{FAIL_THRESHOLD} consecutive failures; not calling upstream.")

    started = time.time()
    last_error: Exception | None = None
    attempts = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        try:
            if _fail_injection:
                raise ConnectorError("injected failure (test hook)")
            result = connector.adapter(operation, payload)
            duration = int((time.time() - started) * 1000)
            with cursor() as cur:
                cur.execute(
                    """INSERT OR REPLACE INTO connector_calls
                       (idempotency_key, connector_id, operation, request_json,
                        response_json, status, attempts, called_at, called_by,
                        duration_ms)
                       VALUES (?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?)""",
                    (key, connector_id, operation,
                     json.dumps(_redact(connector_id, payload)),
                     json.dumps(result), attempts, _now().isoformat(), caller,
                     duration))
            _record_result(connector_id, ok=True)
            return {
                "connector_id": connector_id, "operation": operation,
                "idempotency_key": key, "replayed": False,
                "attempts": attempts, "duration_ms": duration,
                "result": result,
            }
        except ConnectorError as exc:
            last_error = exc
            if not exc.retryable:
                break
            if attempt < MAX_ATTEMPTS:
                time.sleep(min(0.05 * (2 ** (attempt - 1)), 0.4))

    duration = int((time.time() - started) * 1000)
    with cursor() as cur:
        cur.execute(
            """INSERT OR REPLACE INTO connector_calls
               (idempotency_key, connector_id, operation, request_json,
                response_json, status, attempts, called_at, called_by, duration_ms)
               VALUES (?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?)""",
            (key, connector_id, operation,
             json.dumps(_redact(connector_id, payload)),
             json.dumps({"error": str(last_error)}), attempts,
             _now().isoformat(), caller, duration))
    _record_result(connector_id, ok=False)
    raise ConnectorError(str(last_error), retryable=False)
