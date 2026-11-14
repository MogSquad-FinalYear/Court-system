"""Role- and jurisdiction-scoped access control.

The principal is read from request headers (X-User-Id / X-Role /
X-Jurisdiction). That is the one deliberate stub in this module: a real
deployment replaces `current_principal` with JWT/SSO verification and
nothing else here changes, because every policy decision below works off
the Principal object, not off the headers.
"""
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Depends, Header, HTTPException


class Role(StrEnum):
    JUDGE = "judge"
    REGISTRAR = "registrar"
    CLERK = "clerk"
    ADVOCATE = "advocate"
    LITIGANT = "litigant"
    PRISON_OFFICER = "prison_officer"
    PUBLIC = "public"


class Classification(StrEnum):
    PUBLIC = "public"          # published orders, cause lists
    RESTRICTED = "restricted"  # ordinary case filings
    SEALED = "sealed"          # in-camera, POCSO, sealed-cover material


# Which classifications each role may read at all. Jurisdiction is checked
# separately -- clearing this table is necessary but not sufficient.
_READ_MATRIX: dict[Role, set[Classification]] = {
    Role.JUDGE: {Classification.PUBLIC, Classification.RESTRICTED, Classification.SEALED},
    Role.REGISTRAR: {Classification.PUBLIC, Classification.RESTRICTED, Classification.SEALED},
    Role.CLERK: {Classification.PUBLIC, Classification.RESTRICTED},
    Role.ADVOCATE: {Classification.PUBLIC, Classification.RESTRICTED},
    Role.LITIGANT: {Classification.PUBLIC, Classification.RESTRICTED},
    Role.PRISON_OFFICER: {Classification.PUBLIC},
    Role.PUBLIC: {Classification.PUBLIC},
}

_CAN_UPLOAD = {Role.JUDGE, Role.REGISTRAR, Role.CLERK, Role.ADVOCATE, Role.LITIGANT}
_CAN_SIGN = {Role.JUDGE, Role.REGISTRAR, Role.ADVOCATE}
_CAN_SEAL = {Role.JUDGE, Role.REGISTRAR}
# Roles whose reach is not limited to their own jurisdiction.
_CROSS_JURISDICTION = {Role.REGISTRAR}


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: Role
    jurisdiction: str

    def may_read(self, classification: str, jurisdiction: str) -> bool:
        try:
            cls = Classification(classification)
        except ValueError:
            return False  # unknown classification: deny, don't guess
        if cls not in _READ_MATRIX.get(self.role, set()):
            return False
        if cls is Classification.PUBLIC:
            return True  # published material is not jurisdiction-scoped
        if self.role in _CROSS_JURISDICTION:
            return True
        return self.jurisdiction == jurisdiction

    @property
    def may_upload(self) -> bool:
        return self.role in _CAN_UPLOAD

    @property
    def may_sign(self) -> bool:
        return self.role in _CAN_SIGN

    @property
    def may_seal(self) -> bool:
        return self.role in _CAN_SEAL


def current_principal(
    x_user_id: str = Header(default="", alias="X-User-Id"),
    x_role: str = Header(default="", alias="X-Role"),
    x_jurisdiction: str = Header(default="", alias="X-Jurisdiction"),
) -> Principal:
    if not x_user_id or not x_role or not x_jurisdiction:
        raise HTTPException(
            status_code=401,
            detail="X-User-Id, X-Role and X-Jurisdiction headers are required.",
        )
    try:
        role = Role(x_role.lower())
    except ValueError:
        raise HTTPException(
            status_code=403,
            detail=f"Unknown role '{x_role}'. Known roles: {[r.value for r in Role]}",
        )
    return Principal(user_id=x_user_id, role=role, jurisdiction=x_jurisdiction)


PrincipalDep = Depends(current_principal)
