from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from werkzeug.security import generate_password_hash


@pytest.fixture()
def client(tmp_path, monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    import app.config as config
    import app.db as db
    import app.security as security
    import app.routers.auth as auth
    config.DB_PATH = tmp_path / "test.db"
    config.DATABASE_URL = ""
    db.DB_PATH = config.DB_PATH
    db.DATABASE_URL = ""
    config.JWT_PRIVATE_KEY = private_pem
    config.JWT_PUBLIC_KEY = public_pem
    config.AUTH_COOKIE_SECURE = False
    import app.main as main
    security.JWT_PUBLIC_KEY = public_pem
    auth.JWT_PRIVATE_KEY = private_pem
    auth.AUTH_COOKIE_SECURE = False
    db.init_db()
    with db.cursor() as cur:
        for user_id, role, jurisdiction in (
            ("judge-1", "judge", "KA-BLR"),
            ("public-1", "public", "KA-BLR"),
            ("advocate-1", "advocate", "KA-BLR"),
            ("litigant-1", "litigant", "KA-BLR"),
        ):
            cur.execute(
                """INSERT INTO users
                   (id, password_hash, display_name, role, jurisdiction, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, generate_password_hash("correct-password"), user_id,
                 role, jurisdiction, datetime.now(timezone.utc).isoformat()),
            )
    return TestClient(main.app)


def login(client, user_id="judge-1"):
    response = client.post("/api/auth/login",
                           json={"user_id": user_id, "password": "correct-password"})
    assert response.status_code == 200
    return response.json()["access_token"]


def test_protected_routes_require_bearer_and_headers_cannot_spoof(client):
    assert client.get("/api/filing/case-types").status_code == 401
    token = login(client)
    response = client.get(
        "/api/filing/case-types",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Role": "public",
            "X-User-Id": "attacker",
            "X-Jurisdiction": "OTHER",
        },
    )
    assert response.status_code == 200


def test_login_is_generic_and_me_comes_from_verified_claims(client):
    bad = client.post("/api/auth/login",
                      json={"user_id": "missing", "password": "wrong"})
    assert bad.status_code == 401
    assert bad.json()["detail"] == "Invalid credentials."
    token = login(client)
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["user"]["id"] == "judge-1"
    assert "documents:sign" in me.json()["user"]["permissions"]


def test_refresh_rotates_and_reuse_revokes_family(client):
    login(client)
    first = client.cookies.get("veritas_refresh")
    assert first
    rotated = client.post("/api/auth/refresh")
    assert rotated.status_code == 200
    second = client.cookies.get("veritas_refresh")
    assert second and second != first
    client.cookies.set("veritas_refresh", first)
    reused = client.post("/api/auth/refresh")
    assert reused.status_code == 401
    client.cookies.set("veritas_refresh", second)
    assert client.post("/api/auth/refresh").status_code == 401


def test_wrong_issuer_and_expired_tokens_are_rejected(client):
    import app.config as config
    token = jwt.encode({
        "sub": "judge-1", "iss": "wrong", "aud": config.JWT_AUDIENCE,
        "iat": datetime.now(timezone.utc) - timedelta(minutes=2),
        "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
        "jti": "bad-token", "typ": "access", "role": "judge",
        "jurisdiction": "KA-BLR",
    }, config.JWT_PRIVATE_KEY, algorithm="RS256")
    assert client.get("/api/filing/case-types",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_role_gate_is_server_enforced(client):
    token = login(client, "public-1")
    response = client.post(
        "/api/integrations/identity/invoke",
        headers={"Authorization": f"Bearer {token}"},
        json={"operation": "verify", "payload": {"identity_number": "12345678"}},
    )
    assert response.status_code == 403


def test_mutation_scope_comes_from_principal_not_request_fields(client):
    token = login(client)
    response = client.post(
        "/api/documents",
        headers={"Authorization": f"Bearer {token}"},
        data={"title": "scope test", "jurisdiction": "KA-MYS",
              "classification": "public"},
        files={"file": ("sample.pdf", b"not-a-real-pdf", "application/pdf")},
    )
    assert response.status_code == 201
    assert response.json()["jurisdiction"] == "KA-BLR"


def test_connector_idempotency_is_user_scoped(client):
    first = login(client, "advocate-1")
    response = client.post(
        "/api/integrations/payments/invoke",
        headers={"Authorization": f"Bearer {first}", "Idempotency-Key": "same-key"},
        json={"operation": "collect", "payload": {"amount": 10}},
    )
    assert response.status_code == 200
    assert response.json()["replayed"] is False
    second = login(client, "litigant-1")
    response = client.post(
        "/api/integrations/payments/invoke",
        headers={"Authorization": f"Bearer {second}", "Idempotency-Key": "same-key"},
        json={"operation": "collect", "payload": {"amount": 10}},
    )
    assert response.status_code == 200
    assert response.json()["replayed"] is False


def test_change_password_revokes_refresh_sessions(client):
    token = login(client)
    assert client.post(
        "/api/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "correct-password",
              "new_password": "a-new-password-123"},
    ).status_code == 200
    assert client.post(
        "/api/auth/login",
        json={"user_id": "judge-1", "password": "correct-password"},
    ).status_code == 401
    assert client.post(
        "/api/auth/login",
        json={"user_id": "judge-1", "password": "a-new-password-123"},
    ).status_code == 200


def test_refresh_rejects_untrusted_origin(client):
    login(client)
    response = client.post(
        "/api/auth/refresh",
        headers={"Origin": "https://untrusted.example"},
    )
    assert response.status_code == 403
