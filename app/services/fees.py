"""Court fee calculation.

READ THIS BEFORE TRUSTING ANY NUMBER BELOW.

The *engine* here is real: ad valorem slabs, fixed fees by case type,
exemptions, and versioning by effective date all work as written. The
*figures* are a *** PLACEHOLDER SAMPLE ***. They are not the fee schedule of
any actual court. Court fees are set by statute and state schedules; using
these to charge a litigant would be wrong.

Replace `SCHEDULES` with your jurisdiction's real figures. The structure is
deliberately plain data so that replacement is an edit, not a rewrite.

Versioning by effective date is not decoration: a filing must be able to be
recomputed years later exactly as it was charged at the time, when a
schedule has since changed.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class FeeSchedule:
    effective_from: date
    currency: str
    # case_type -> fixed filing fee
    fixed: dict[str, float]
    # (upper_bound_of_claim, rate) ascending; rate applies to the portion of
    # the claim falling inside that slab. Final slab uses float('inf').
    ad_valorem_slabs: list[tuple[float, float]]
    ad_valorem_cap: float
    # flat additions that apply to every filing
    surcharges: dict[str, float] = field(default_factory=dict)


# *** PLACEHOLDER SAMPLE DATA -- NOT A REAL FEE SCHEDULE ***
SCHEDULES: list[FeeSchedule] = [
    FeeSchedule(
        effective_from=date(2020, 4, 1),
        currency="INR",
        fixed={
            "CRL.P": 500.0,      # criminal petition
            "CRL.A": 750.0,      # criminal appeal
            "W.P": 1000.0,       # writ petition
            "O.S": 250.0,        # original suit (plus ad valorem on the claim)
            "M.A": 300.0,        # miscellaneous application
            "EXEC": 200.0,       # execution petition
        },
        ad_valorem_slabs=[(50_000.0, 0.01), (500_000.0, 0.02), (float("inf"), 0.03)],
        ad_valorem_cap=200_000.0,
        surcharges={"legal_services_authority": 25.0, "advocates_welfare": 15.0},
    ),
    FeeSchedule(
        effective_from=date(2024, 4, 1),
        currency="INR",
        fixed={
            "CRL.P": 600.0,
            "CRL.A": 900.0,
            "W.P": 1200.0,
            "O.S": 300.0,
            "M.A": 350.0,
            "EXEC": 250.0,
        },
        ad_valorem_slabs=[(100_000.0, 0.01), (1_000_000.0, 0.02), (float("inf"), 0.03)],
        ad_valorem_cap=250_000.0,
        surcharges={"legal_services_authority": 50.0, "advocates_welfare": 25.0},
    ),
]

CASE_TYPES = sorted(SCHEDULES[-1].fixed)

# Grounds that waive the whole fee. Legal-aid eligibility is decided by the
# legal aid authority, not here -- the Integration Hub's legal_aid connector
# is what would confirm it; passing the flag without that confirmation is
# accepting the applicant's word.
EXEMPTIONS = {
    "legal_aid": "Legal aid beneficiary (requires legal_aid authority confirmation)",
    "in_forma_pauperis": "Permission to sue as an indigent person",
    "state_government": "State as petitioner",
}


def schedule_for(on: date) -> FeeSchedule:
    """The schedule in force on a given date."""
    starts = [s.effective_from for s in SCHEDULES]
    idx = bisect_right(starts, on) - 1
    if idx < 0:
        raise ValueError(f"No fee schedule in force on {on.isoformat()}")
    return SCHEDULES[idx]


def _ad_valorem(claim_value: float, sched: FeeSchedule) -> tuple[float, list[dict]]:
    """Slab-wise, so only the portion inside each slab takes that rate."""
    remaining = claim_value
    lower = 0.0
    total = 0.0
    lines: list[dict] = []
    for upper, rate in sched.ad_valorem_slabs:
        if remaining <= 0:
            break
        portion = min(remaining, upper - lower)
        amount = portion * rate
        total += amount
        lines.append({
            "slab": f"{lower:,.0f}–{'∞' if upper == float('inf') else format(upper, ',.0f')}",
            "rate": rate,
            "portion": round(portion, 2),
            "amount": round(amount, 2),
        })
        remaining -= portion
        lower = upper
    if total > sched.ad_valorem_cap:
        lines.append({
            "slab": "cap applied",
            "rate": 0.0,
            "portion": 0.0,
            "amount": round(sched.ad_valorem_cap - total, 2),
        })
        total = sched.ad_valorem_cap
    return total, lines


def calculate(
    *,
    case_type: str,
    claim_value: float | None = None,
    filing_date: date | None = None,
    exemptions: list[str] | None = None,
) -> dict:
    """Compute the fee for a prospective filing.

    Returns the total plus a line-by-line breakdown, because a litigant is
    entitled to see how a fee was arrived at, not just what it came to.
    """
    on = filing_date or date.today()
    sched = schedule_for(on)
    if case_type not in sched.fixed:
        raise ValueError(
            f"Unknown case_type '{case_type}'. Known: {sorted(sched.fixed)}"
        )

    exemptions = exemptions or []
    unknown = [e for e in exemptions if e not in EXEMPTIONS]
    if unknown:
        raise ValueError(f"Unknown exemption(s): {unknown}. Known: {sorted(EXEMPTIONS)}")

    lines = [{
        "label": f"Filing fee ({case_type})",
        "amount": round(sched.fixed[case_type], 2),
    }]

    av_detail: list[dict] = []
    if claim_value:
        av_total, av_detail = _ad_valorem(float(claim_value), sched)
        lines.append({
            "label": f"Ad valorem on claim of {claim_value:,.2f}",
            "amount": round(av_total, 2),
        })

    for name, amount in sched.surcharges.items():
        lines.append({"label": f"Surcharge: {name}", "amount": round(amount, 2)})

    subtotal = round(sum(line["amount"] for line in lines), 2)

    waived = 0.0
    if exemptions:
        waived = subtotal
        lines.append({
            "label": "Exemption: " + ", ".join(EXEMPTIONS[e] for e in exemptions),
            "amount": round(-waived, 2),
        })

    return {
        "case_type": case_type,
        "filing_date": on.isoformat(),
        "schedule_effective_from": sched.effective_from.isoformat(),
        "currency": sched.currency,
        "subtotal": subtotal,
        "waived": round(waived, 2),
        "total": round(subtotal - waived, 2),
        "lines": lines,
        "ad_valorem_slabs": av_detail,
    }
