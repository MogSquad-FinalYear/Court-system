"""Digital signatures and court seal stamping.

Both are the same primitive with a different payload: an HMAC-SHA256 over a
canonical string that always includes the exact content hash being attested
to. Binding to the content hash rather than to a document id is the whole
point -- an attestation stays valid only for the bytes that existed when it
was made, so uploading a new version silently invalidates nothing, it simply
leaves the new version unsigned.

Scope limit, stated plainly: HMAC proves *this server* attested, it does not
prove *which judge* attested to a third party, because verification needs
the same secret that signing needed. That is adequate for in-system
tamper-evidence and inadequate as a legal e-signature. Production needs
asymmetric keys (X.509 / PKCS#7 detached signatures, per-officer keys in an
HSM or smart card) so a third party can verify without being able to forge.
`sign_payload` is the single seam where that swap happens.
"""
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from hashlib import sha256

from app.config import COURT_ID, SEAL_SIGNING_KEY
from app.db import cursor, query_all


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sign_payload(payload: str) -> str:
    return hmac.new(SEAL_SIGNING_KEY, payload.encode(), sha256).hexdigest()


def _canonical(kind: str, content_hash: str, actor_id: str, actor_role: str,
               created_at: str, seal_number: str | None) -> str:
    """Field-separated so no two different inputs can produce one string."""
    return "|".join([
        kind, content_hash, actor_id, actor_role, created_at, seal_number or "-"
    ])


def attest(
    *,
    version_id: str,
    content_hash: str,
    kind: str,
    actor_id: str,
    actor_role: str,
    reason: str | None = None,
) -> dict:
    """Record a signature ('signature') or a court seal ('seal')."""
    if kind not in ("signature", "seal"):
        raise ValueError("kind must be 'signature' or 'seal'")

    created_at = _now()
    seal_number = None
    court_id = None
    if kind == "seal":
        court_id = COURT_ID
        # Human-quotable seal number; the cryptographic binding is the HMAC.
        seal_number = f"{COURT_ID}/{datetime.now(timezone.utc):%Y}/{secrets.token_hex(3).upper()}"

    signature = sign_payload(
        _canonical(kind, content_hash, actor_id, actor_role, created_at, seal_number)
    )
    row = {
        "id": str(uuid.uuid4()),
        "version_id": version_id,
        "kind": kind,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "court_id": court_id,
        "seal_number": seal_number,
        "content_hash": content_hash,
        "signature": signature,
        "created_at": created_at,
        "reason": reason,
    }
    with cursor() as cur:
        cur.execute(
            """INSERT INTO attestations
               (id, version_id, kind, actor_id, actor_role, court_id, seal_number,
                content_hash, signature, created_at, reason)
               VALUES (:id, :version_id, :kind, :actor_id, :actor_role, :court_id,
                       :seal_number, :content_hash, :signature, :created_at, :reason)""",
            row,
        )
    return row


def verify_attestation(
    row: dict,
    current_content_hash: str,
    *,
    blob_verified: bool | None = None,
) -> dict:
    """Recompute the HMAC and re-check what it covers.

    Three independent things have to hold for an attestation to mean what a
    reader assumes it means, and they fail in different ways:

      signature_valid        -- the HMAC matches, i.e. this server really
                                did attest to that hash
      covers_current_content -- the hash signed is still this version's
                                recorded hash (a later version does not
                                retroactively invalidate an older one, but
                                it does leave the newer bytes unsigned)
      blob_verified          -- the bytes on disk *still hash to* the
                                attested hash

    The third needs an actual re-hash of the file, which is too expensive to
    do on every document read, so callers pass it in when they have already
    paid for it (the integrity endpoint) and leave it None otherwise. `valid`
    deliberately never reports True on an unverified-and-known-bad blob:
    None means "not checked here", False means "checked and wrong".
    """
    expected = sign_payload(
        _canonical(
            row["kind"], row["content_hash"], row["actor_id"],
            row["actor_role"], row["created_at"], row["seal_number"],
        )
    )
    signature_valid = hmac.compare_digest(expected, row["signature"])
    covers_current = row["content_hash"] == current_content_hash
    return {
        **row,
        "signature_valid": signature_valid,
        "covers_current_content": covers_current,
        "blob_verified": blob_verified,
        "valid": signature_valid and covers_current and blob_verified is not False,
    }


def for_version(
    version_id: str,
    current_content_hash: str,
    *,
    blob_verified: bool | None = None,
) -> list[dict]:
    rows = query_all(
        "SELECT * FROM attestations WHERE version_id = ? ORDER BY created_at",
        (version_id,),
    )
    return [
        verify_attestation(r, current_content_hash, blob_verified=blob_verified)
        for r in rows
    ]
