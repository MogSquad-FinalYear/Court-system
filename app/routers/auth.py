from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from werkzeug.security import check_password_hash, generate_password_hash

from app.config import (
    AUTH_ALLOW_REGISTRATION,
    AUTH_COOKIE_SECURE,
    AUTH_REGISTRATION_JURISDICTION,
    AUTH_REFRESH_COOKIE,
    JWT_ACCESS_TTL_MINUTES,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_PRIVATE_KEY,
    JWT_REFRESH_TTL_DAYS,
)
from app.db import cursor, query_one
from app.security import Principal, Role, current_principal

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
_attempts: dict[str, list[datetime]] = {}
_attempts_lock = threading.Lock()


class LoginIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class RegisterIn(LoginIn):
    display_name: str = Field(min_length=1, max_length=200)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(request: Request, action: str, result: str, actor: Principal | None = None,
           detail: str | None = None) -> None:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO security_events
               (at, actor_id, actor_role, jurisdiction, action, result, request_id, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now().isoformat(), actor.user_id if actor else None,
             actor.role.value if actor else None, actor.jurisdiction if actor else None,
             action, result, request.headers.get("X-Request-ID"), detail),
        )


def _configured() -> None:
    if not JWT_PRIVATE_KEY:
        raise HTTPException(503, "Authentication is not configured.")


def _user_view(user: dict) -> dict:
    return {
        "id": user["id"],
        "display_name": user["display_name"],
        "roles": [user["role"]],
        "permissions": _permissions(Role(user["role"])),
        "jurisdictions": [user["jurisdiction"]],
        "active": bool(user["active"]),
    }


def _permissions(role: Role) -> list[str]:
    permissions = ["documents:read", "filing:read", "scheduling:read"]
    if role in {Role.JUDGE, Role.REGISTRAR, Role.CLERK, Role.ADVOCATE, Role.LITIGANT}:
        permissions += ["documents:upload", "filing:submit"]
    if role in {Role.JUDGE, Role.REGISTRAR, Role.ADVOCATE}:
        permissions.append("documents:sign")
    if role in {Role.JUDGE, Role.REGISTRAR}:
        permissions += ["documents:audit", "integrations:calls"]
    if role in {Role.JUDGE, Role.REGISTRAR, Role.CLERK}:
        permissions += ["scheduling:write"]
    if role is Role.REGISTRAR:
        permissions.append("resources:manage")
    return permissions


def _tokens(user: dict, family_id: str | None = None) -> tuple[str, str, str]:
    _configured()
    now = _now()
    jti = str(uuid.uuid4())
    permissions = _permissions(Role(user["role"]))
    access = jwt.encode({
        "sub": user["id"], "iss": JWT_ISSUER, "aud": JWT_AUDIENCE,
        "iat": now, "exp": now + timedelta(minutes=JWT_ACCESS_TTL_MINUTES),
        "jti": jti, "typ": "access", "role": user["role"],
        "jurisdiction": user["jurisdiction"], "permissions": permissions,
    }, JWT_PRIVATE_KEY, algorithm="RS256")
    raw_refresh = uuid.uuid4().hex + uuid.uuid4().hex
    token_hash = hashlib.sha256(raw_refresh.encode()).hexdigest()
    expires = now + timedelta(days=JWT_REFRESH_TTL_DAYS)
    with cursor() as cur:
        cur.execute(
            """INSERT INTO refresh_tokens
               (token_hash, user_id, family_id, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token_hash, user["id"], family_id or str(uuid.uuid4()), expires.isoformat(),
             now.isoformat()),
        )
    return access, raw_refresh, jti


def _too_many_attempts(client: str) -> bool:
    now = _now()
    with _attempts_lock:
        recent = [t for t in _attempts.get(client, []) if now - t < timedelta(minutes=1)]
        _attempts[client] = recent
        return len(recent) >= 10


def _record_attempt(client: str) -> None:
    with _attempts_lock:
        _attempts.setdefault(client, []).append(_now())


@router.post("/register", status_code=201)
def register(body: RegisterIn, request: Request) -> dict:
    if not AUTH_ALLOW_REGISTRATION:
        raise HTTPException(403, "Self-registration is disabled.")
    if not AUTH_REGISTRATION_JURISDICTION:
        raise HTTPException(503, "Self-registration is not configured.")
    if query_one("SELECT 1 FROM users WHERE id = ?", (body.user_id,)):
        raise HTTPException(409, "Unable to create account.")
    with cursor() as cur:
        cur.execute(
            """INSERT INTO users (id, password_hash, display_name, role, jurisdiction, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (body.user_id, generate_password_hash(body.password), body.display_name,
             Role.LITIGANT.value, AUTH_REGISTRATION_JURISDICTION,
             _now().isoformat()),
        )
    user = query_one("SELECT * FROM users WHERE id = ?", (body.user_id,))
    _event(request, "register", "success", detail="account created")
    return {"user": _user_view(user)}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    client = request.client.host if request.client else "unknown"
    if _too_many_attempts(client):
        raise HTTPException(429, "Too many login attempts. Try again later.")
    user = query_one("SELECT * FROM users WHERE id = ?", (body.user_id,))
    if not user or not user["active"] or not check_password_hash(
            user["password_hash"] if user else "", body.password):
        _record_attempt(client)
        _event(request, "login", "failure", detail="invalid credentials")
        raise HTTPException(401, "Invalid credentials.")
    access, refresh, _ = _tokens(user)
    response.set_cookie(
        AUTH_REFRESH_COOKIE, refresh, httponly=True, secure=AUTH_COOKIE_SECURE,
        samesite="strict", max_age=JWT_REFRESH_TTL_DAYS * 86400, path="/api/auth",
    )
    _event(request, "login", "success", Principal(user["id"], Role(user["role"]),
                                                  user["jurisdiction"]))
    return {"access_token": access, "token_type": "Bearer",
            "expires_in": JWT_ACCESS_TTL_MINUTES * 60, "user": _user_view(user)}


@router.post("/refresh")
def refresh(request: Request, response: Response,
            refresh_token: str | None = Cookie(default=None, alias=AUTH_REFRESH_COOKIE)) -> dict:
    if not refresh_token:
        raise HTTPException(401, "Authentication required.")
    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    row = query_one("SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,))
    if not row:
        raise HTTPException(401, "Authentication required.")
    if row["revoked_at"]:
        with cursor() as cur:
            cur.execute("UPDATE refresh_tokens SET revoked_at = ? WHERE family_id = ? "
                        "AND revoked_at IS NULL", (_now().isoformat(), row["family_id"]))
        _event(request, "token_reuse", "denied", detail="refresh token replay")
        raise HTTPException(401, "Authentication required.")
    if datetime.fromisoformat(row["expires_at"]) <= _now():
        raise HTTPException(401, "Authentication required.")
    user = query_one("SELECT * FROM users WHERE id = ?", (row["user_id"],))
    if not user or not user["active"]:
        raise HTTPException(401, "Authentication required.")
    with cursor() as cur:
        cur.execute("UPDATE refresh_tokens SET revoked_at = ? WHERE token_hash = ?",
                    (_now().isoformat(), token_hash))
    access, new_refresh, _ = _tokens(user, row["family_id"])
    response.set_cookie(
        AUTH_REFRESH_COOKIE, new_refresh, httponly=True, secure=AUTH_COOKIE_SECURE,
        samesite="strict", max_age=JWT_REFRESH_TTL_DAYS * 86400, path="/api/auth",
    )
    return {"access_token": access, "token_type": "Bearer",
            "expires_in": JWT_ACCESS_TTL_MINUTES * 60, "user": _user_view(user)}


@router.post("/logout")
def logout(request: Request, response: Response,
           refresh_token: str | None = Cookie(default=None, alias=AUTH_REFRESH_COOKIE)) -> dict:
    if refresh_token:
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        row = query_one("SELECT family_id FROM refresh_tokens WHERE token_hash = ?",
                        (token_hash,))
        if row:
            with cursor() as cur:
                cur.execute("UPDATE refresh_tokens SET revoked_at = ? WHERE family_id = ?",
                            (_now().isoformat(), row["family_id"]))
    response.delete_cookie(AUTH_REFRESH_COOKIE, path="/api/auth")
    _event(request, "logout", "success")
    return {"status": "logged_out"}


@router.get("/me")
def me(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    user = query_one("SELECT * FROM users WHERE id = ?", (principal.user_id,))
    return {"user": _user_view(user)}


@router.post("/change-password")
def change_password(body: ChangePasswordIn, request: Request,
                    principal: Principal = Depends(current_principal)) -> dict:
    user = query_one("SELECT * FROM users WHERE id = ?", (principal.user_id,))
    if not user or not check_password_hash(user["password_hash"], body.current_password):
        _event(request, "change_password", "failure", principal, "invalid current password")
        raise HTTPException(400, "Current password is incorrect.")
    if body.current_password == body.new_password:
        raise HTTPException(400, "New password must be different.")
    with cursor() as cur:
        cur.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(body.new_password), principal.user_id))
        cur.execute("UPDATE refresh_tokens SET revoked_at = ? "
                    "WHERE user_id = ? AND revoked_at IS NULL",
                    (_now().isoformat(), principal.user_id))
    _event(request, "change_password", "success", principal)
    return {"status": "password_changed"}
