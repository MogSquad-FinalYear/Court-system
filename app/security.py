from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import JWT_AUDIENCE, JWT_ISSUER, JWT_PUBLIC_KEY
from app.db import query_one


class Role(StrEnum):
    JUDGE = "judge"
    REGISTRAR = "registrar"
    CLERK = "clerk"
    ADVOCATE = "advocate"
    LITIGANT = "litigant"
    PRISON_OFFICER = "prison_officer"
    PUBLIC = "public"


class Classification(StrEnum):
    PUBLIC = "public"
    RESTRICTED = "restricted"
    SEALED = "sealed"


_READ_MATRIX = {
    Role.JUDGE: set(Classification),
    Role.REGISTRAR: set(Classification),
    Role.CLERK: {Classification.PUBLIC, Classification.RESTRICTED},
    Role.ADVOCATE: {Classification.PUBLIC, Classification.RESTRICTED},
    Role.LITIGANT: {Classification.PUBLIC, Classification.RESTRICTED},
    Role.PRISON_OFFICER: {Classification.PUBLIC},
    Role.PUBLIC: {Classification.PUBLIC},
}
_CAN_UPLOAD = {Role.JUDGE, Role.REGISTRAR, Role.CLERK, Role.ADVOCATE, Role.LITIGANT}
_CAN_SIGN = {Role.JUDGE, Role.REGISTRAR, Role.ADVOCATE}
_CAN_SEAL = {Role.JUDGE, Role.REGISTRAR}
_CROSS_JURISDICTION = {Role.REGISTRAR}


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: Role
    jurisdiction: str
    permissions: tuple[str, ...] = ()

    def may_read(self, classification: str, jurisdiction: str) -> bool:
        try:
            cls = Classification(classification)
        except ValueError:
            return False
        return (
            cls in _READ_MATRIX.get(self.role, set())
            and (cls is Classification.PUBLIC
                 or self.role in _CROSS_JURISDICTION
                 or self.jurisdiction == jurisdiction)
        )

    @property
    def may_upload(self) -> bool:
        return self.role in _CAN_UPLOAD

    @property
    def may_sign(self) -> bool:
        return self.role in _CAN_SIGN

    @property
    def may_seal(self) -> bool:
        return self.role in _CAN_SEAL


_bearer = HTTPBearer(auto_error=False)


def _auth_error() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Authentication required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """Verify an access token and derive every identity attribute from it."""
    if not credentials or credentials.scheme.lower() != "bearer":
        raise _auth_error()
    if not JWT_PUBLIC_KEY:
        raise HTTPException(503, "Authentication is not configured.")
    try:
        claims = jwt.decode(
            credentials.credentials,
            JWT_PUBLIC_KEY,
            algorithms=["RS256"],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            options={"require": ["sub", "iss", "aud", "iat", "exp", "jti", "typ"]},
        )
    except jwt.PyJWTError:
        raise _auth_error()
    if claims.get("typ") != "access":
        raise _auth_error()
    user = query_one("SELECT id, active FROM users WHERE id = ?", (claims["sub"],))
    if not user or not user["active"]:
        raise _auth_error()
    if query_one("SELECT 1 FROM revoked_access_tokens WHERE jti = ?", (claims["jti"],)):
        raise _auth_error()
    try:
        role = Role(claims["role"])
    except (KeyError, ValueError):
        raise _auth_error()
    jurisdiction = claims.get("jurisdiction")
    if not isinstance(jurisdiction, str) or not jurisdiction:
        raise _auth_error()
    request.state.principal = Principal(
        user_id=claims["sub"], role=role, jurisdiction=jurisdiction,
        permissions=tuple(claims.get("permissions") or ()),
    )
    return request.state.principal


PrincipalDep = Depends(current_principal)
