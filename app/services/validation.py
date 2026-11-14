"""Filing scrutiny -- the checks a registry clerk performs before a filing
is accepted and given a case number.

Each check returns objections rather than raising, because a filing with
four defects should come back with all four, not with the first one. That
is how physical scrutiny works and it is the difference between one
resubmission and four.

Limitation periods below are *** PLACEHOLDER SAMPLE VALUES ***, same caveat
as fees.py: the engine is real, the statutory periods must be replaced with
your jurisdiction's actual limitation law before this is used on anything.
"""
from __future__ import annotations

from datetime import date, timedelta

# case_type -> documents that must be attached, by role
REQUIRED_DOCUMENTS: dict[str, list[str]] = {
    "CRL.P": ["petition", "impugned_order", "vakalatnama"],
    "CRL.A": ["memorandum_of_appeal", "certified_copy_of_judgment", "vakalatnama"],
    "W.P": ["petition", "affidavit", "vakalatnama"],
    "O.S": ["plaint", "list_of_documents", "vakalatnama"],
    "M.A": ["application", "affidavit"],
    "EXEC": ["execution_petition", "decree_copy"],
}

# *** PLACEHOLDER *** limitation period in days from the cause of action.
# None means no limitation period is enforced for that case type here.
LIMITATION_DAYS: dict[str, int | None] = {
    "CRL.P": None,
    "CRL.A": 90,
    "W.P": None,
    "O.S": 1095,     # 3 years
    "M.A": None,
    "EXEC": 4380,    # 12 years
}


def _objection(code: str, message: str, remedy: str) -> dict:
    return {"code": code, "message": message, "remedy": remedy}


def validate(
    *,
    case_type: str,
    parties: list[dict],
    attached_roles: list[str],
    cause_of_action_date: date | None = None,
    filing_date: date | None = None,
    filed_by_role: str = "advocate",
) -> list[dict]:
    """Return every objection found. Empty list means the filing is clean."""
    objections: list[dict] = []
    on = filing_date or date.today()

    if case_type not in REQUIRED_DOCUMENTS:
        return [_objection(
            "unknown_case_type",
            f"Case type '{case_type}' is not accepted by this court.",
            f"Use one of: {sorted(REQUIRED_DOCUMENTS)}",
        )]

    # --- required documents ------------------------------------------
    required = set(REQUIRED_DOCUMENTS[case_type])
    # A vakalatnama authorises an advocate to act. A party filing in person
    # does not need one, and demanding it would block litigants in person.
    if filed_by_role == "litigant":
        required.discard("vakalatnama")
    missing = sorted(required - set(attached_roles))
    for role in missing:
        objections.append(_objection(
            "missing_document",
            f"Required document not attached: {role}.",
            f"Attach a document with role='{role}' and resubmit.",
        ))

    # --- parties ------------------------------------------------------
    petitioners = [p for p in parties if p.get("side") == "petitioner"]
    respondents = [p for p in parties if p.get("side") == "respondent"]
    if not petitioners:
        objections.append(_objection(
            "no_petitioner", "At least one petitioner is required.",
            "Add a party with side='petitioner'.",
        ))
    if not respondents:
        objections.append(_objection(
            "no_respondent", "At least one respondent is required.",
            "Add a party with side='respondent'.",
        ))
    for p in parties:
        if not (p.get("name") or "").strip():
            objections.append(_objection(
                "party_name_missing", "Every party must have a name.",
                "Supply 'name' for each party.",
            ))
        # Service of notice has to reach the party somehow. A party in
        # custody is the exception -- they are served through the prison.
        if not p.get("email") and not p.get("phone") and not p.get("in_custody"):
            objections.append(_objection(
                "no_contact",
                f"No email or phone for party '{p.get('name') or '?'}'; "
                "notices and hearing reminders cannot be served.",
                "Supply an email or phone, or set in_custody=true.",
            ))

    # --- limitation ---------------------------------------------------
    limit = LIMITATION_DAYS.get(case_type)
    if limit is not None:
        if cause_of_action_date is None:
            objections.append(_objection(
                "cause_of_action_date_missing",
                f"{case_type} carries a {limit}-day limitation period, so the "
                "cause-of-action date is required.",
                "Supply cause_of_action_date (YYYY-MM-DD).",
            ))
        else:
            elapsed = (on - cause_of_action_date).days
            if elapsed < 0:
                objections.append(_objection(
                    "cause_of_action_in_future",
                    "Cause-of-action date is after the filing date.",
                    "Correct the date.",
                ))
            elif elapsed > limit:
                objections.append(_objection(
                    "time_barred",
                    f"Filed {elapsed} days after the cause of action; the "
                    f"limitation period is {limit} days.",
                    "File an application for condonation of delay along with "
                    "this filing.",
                ))

    return objections


def deadline_for(case_type: str, cause_of_action_date: date) -> date | None:
    limit = LIMITATION_DAYS.get(case_type)
    return None if limit is None else cause_of_action_date + timedelta(days=limit)
