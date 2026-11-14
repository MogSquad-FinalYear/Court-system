"""Scheduling & Hearings.

Conflict-aware hearing scheduling, courtroom capacity, cause lists and
reminder queueing.

Reminders are *queued*, never claimed as sent: dispatch would go through
the SMS/email connectors in the Integration Hub, and those are mocks. A
system that reported "reminder sent" when nothing left the building would
be worse than one that admits it is a queue, because a party who misses a
hearing on the strength of a reminder that never arrived has a due-process
complaint.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.config import COURT_ID
from app.db import cursor, query_all, query_one
from app.security import Principal, current_principal
from app.services import calendar as cal

router = APIRouter(prefix="/api/scheduling", tags=["Scheduling & Hearings"])

_MAY_SCHEDULE = {"judge", "registrar", "clerk"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_day(value: str, field: str = "day") -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(400, f"{field} must be YYYY-MM-DD, got {value!r}")


# --------------------------------------------------------------------
# Resource registry
# --------------------------------------------------------------------

@router.get("/resources")
def resources(principal: Principal = Depends(current_principal)) -> dict:
    return {
        "judges": query_all("SELECT * FROM judges ORDER BY name"),
        "courtrooms": query_all("SELECT * FROM courtrooms ORDER BY name"),
        "holidays": query_all(
            "SELECT * FROM court_holidays ORDER BY day"),
    }


@router.post("/resources/judges", status_code=201)
def add_judge(body: dict = Body(...),
              principal: Principal = Depends(current_principal)) -> dict:
    if principal.role.value != "registrar":
        raise HTTPException(403, "Only a registrar may manage court resources.")
    jid = body.get("id") or str(uuid.uuid4())[:8]
    with cursor() as cur:
        cur.execute(
            """INSERT INTO judges (id, name, court_id, sitting_start, sitting_end)
               VALUES (?, ?, ?, ?, ?)""",
            (jid, body["name"], body.get("court_id", COURT_ID),
             body.get("sitting_start", "10:30"), body.get("sitting_end", "16:30")))
    return query_one("SELECT * FROM judges WHERE id = ?", (jid,))


@router.post("/resources/courtrooms", status_code=201)
def add_courtroom(body: dict = Body(...),
                  principal: Principal = Depends(current_principal)) -> dict:
    if principal.role.value != "registrar":
        raise HTTPException(403, "Only a registrar may manage court resources.")
    rid = body.get("id") or str(uuid.uuid4())[:8]
    with cursor() as cur:
        cur.execute(
            """INSERT INTO courtrooms (id, name, court_id, capacity, video_link)
               VALUES (?, ?, ?, ?, ?)""",
            (rid, body["name"], body.get("court_id", COURT_ID),
             int(body.get("capacity", 40)), 1 if body.get("video_link") else 0))
    return query_one("SELECT * FROM courtrooms WHERE id = ?", (rid,))


@router.post("/resources/holidays", status_code=201)
def add_holiday(body: dict = Body(...),
                principal: Principal = Depends(current_principal)) -> dict:
    if principal.role.value != "registrar":
        raise HTTPException(403, "Only a registrar may manage court resources.")
    day = _parse_day(body["day"])
    with cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO court_holidays (court_id, day, reason) "
            "VALUES (?, ?, ?)",
            (body.get("court_id", COURT_ID), day.isoformat(), body.get("reason")))
    return {"court_id": body.get("court_id", COURT_ID), "day": day.isoformat(),
            "reason": body.get("reason")}


# --------------------------------------------------------------------
# Availability and scheduling
# --------------------------------------------------------------------

@router.get("/availability")
def check_availability(
    case_id: str = Query(...),
    judge_id: str = Query(...),
    courtroom_id: str = Query(...),
    day: str = Query(...),
    duration_minutes: int = Query(default=30, ge=5, le=480),
    requires_video: bool = Query(default=False),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Slot-by-slot availability for a judge/courtroom/day."""
    case = query_one("SELECT * FROM cases WHERE id = ?", (case_id,))
    if not case:
        raise HTTPException(404, "Unknown case id.")
    if case["jurisdiction"] != principal.jurisdiction and \
            principal.role.value != "registrar":
        raise HTTPException(404, "Unknown case id.")
    slots = cal.availability(
        case_id=case_id, judge_id=judge_id, courtroom_id=courtroom_id,
        day=_parse_day(day), duration_minutes=duration_minutes,
        requires_video=requires_video)
    free = [s for s in slots if s["free"]]
    return {
        "day": day, "judge_id": judge_id, "courtroom_id": courtroom_id,
        "duration_minutes": duration_minutes,
        "free_count": len(free), "total_slots": len(slots),
        "slots": slots,
    }


@router.post("/hearings", status_code=201)
def schedule_hearing(
    body: dict = Body(...),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Place a hearing on the calendar, refusing any conflicting slot."""
    if principal.role.value not in _MAY_SCHEDULE:
        raise HTTPException(
            403, f"Role '{principal.role.value}' may not schedule hearings.")

    case = query_one("SELECT * FROM cases WHERE id = ?", (body.get("case_id"),))
    if not case:
        raise HTTPException(404, "Unknown case id.")
    if case["jurisdiction"] != principal.jurisdiction and \
            principal.role.value != "registrar":
        raise HTTPException(404, "Unknown case id.")

    starts_at = body.get("starts_at")
    ends_at = body.get("ends_at")
    if not starts_at or not ends_at:
        raise HTTPException(400, "starts_at and ends_at (ISO8601) are required.")

    # An in-custody party means the room must have a video link.
    in_custody = query_one(
        "SELECT COUNT(*) AS n FROM parties WHERE case_id = ? AND in_custody = 1",
        (case["id"],))["n"] > 0
    requires_video = bool(body.get("requires_video", in_custody))

    conflicts = cal.check_conflicts(
        case_id=case["id"], judge_id=body.get("judge_id", ""),
        courtroom_id=body.get("courtroom_id", ""),
        starts_at=starts_at, ends_at=ends_at, requires_video=requires_video)
    if conflicts:
        raise HTTPException(409, {
            "message": "Slot unavailable.",
            "conflicts": conflicts,
        })

    hid = str(uuid.uuid4())
    with cursor() as cur:
        cur.execute(
            """INSERT INTO hearings
               (id, case_id, judge_id, courtroom_id, starts_at, ends_at,
                purpose, status, requires_video, created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, ?)""",
            (hid, case["id"], body["judge_id"], body["courtroom_id"],
             starts_at, ends_at, body.get("purpose"),
             1 if requires_video else 0, _now(), principal.user_id))

    return {
        "hearing_id": hid, "case_number": case["case_number"],
        "starts_at": starts_at, "ends_at": ends_at,
        "requires_video": requires_video,
        "reminders": queue_reminders_for(hid),
    }


@router.get("/hearings/{hearing_id}")
def get_hearing(hearing_id: str,
                principal: Principal = Depends(current_principal)) -> dict:
    h = query_one(
        """SELECT h.*, c.case_number, c.title, c.jurisdiction,
                  j.name AS judge_name, r.name AS courtroom_name, r.capacity
           FROM hearings h
           JOIN cases c ON c.id = h.case_id
           JOIN judges j ON j.id = h.judge_id
           JOIN courtrooms r ON r.id = h.courtroom_id
           WHERE h.id = ?""", (hearing_id,))
    if not h:
        raise HTTPException(404, "Unknown hearing id.")
    if h["jurisdiction"] != principal.jurisdiction and \
            principal.role.value != "registrar":
        raise HTTPException(404, "Unknown hearing id.")
    h["reminders"] = query_all(
        "SELECT channel, destination, scheduled_for, status FROM reminders "
        "WHERE hearing_id = ?", (hearing_id,))
    return h


@router.post("/hearings/{hearing_id}/adjourn")
def adjourn(hearing_id: str, body: dict = Body(default={}),
            principal: Principal = Depends(current_principal)) -> dict:
    """Adjourn a hearing, freeing its slot for other matters."""
    if principal.role.value not in _MAY_SCHEDULE:
        raise HTTPException(403, "Not permitted to adjourn hearings.")
    hearing = query_one(
        """SELECT h.id, c.jurisdiction FROM hearings h
           JOIN cases c ON c.id = h.case_id WHERE h.id = ?""", (hearing_id,))
    if not hearing:
        raise HTTPException(404, "Unknown hearing id.")
    if hearing["jurisdiction"] != principal.jurisdiction and \
            principal.role.value != "registrar":
        raise HTTPException(404, "Unknown hearing id.")
    with cursor() as cur:
        cur.execute("UPDATE hearings SET status = 'adjourned' WHERE id = ?",
                    (hearing_id,))
        # Pending reminders for an adjourned hearing must not go out.
        cur.execute(
            "UPDATE reminders SET status = 'skipped', "
            "last_error = 'hearing adjourned' "
            "WHERE hearing_id = ? AND status = 'queued'", (hearing_id,))
    return {"hearing_id": hearing_id, "status": "adjourned",
            "reason": body.get("reason")}


# --------------------------------------------------------------------
# Cause list
# --------------------------------------------------------------------

@router.get("/cause-list")
def cause_list(
    day: str = Query(...),
    court_id: str = Query(default=COURT_ID),
    principal: Principal = Depends(current_principal),
) -> dict:
    """The day's cause list, ordered part-heard first, then by time."""
    d = _parse_day(day)
    items = cal.cause_list(court_id, d)
    hol = cal.is_holiday(court_id, d)
    visible = [
        i for i in items
        if i["jurisdiction"] == principal.jurisdiction
        or principal.role.value in ("registrar", "judge")
    ]
    return {
        "court_id": court_id, "day": day,
        "holiday": hol["reason"] if hol else None,
        "item_count": len(visible),
        "items": [
            {
                "item_no": i["item_no"], "case_number": i["case_number"],
                "title": i["title"], "status": i["status"],
                "starts_at": i["starts_at"], "judge": i["judge_name"],
                "courtroom": i["courtroom_name"], "purpose": i["purpose"],
                "advocates": i["advocates"],
            }
            for i in visible
        ],
    }


# --------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------

def queue_reminders_for(hearing_id: str, days_before: int = 3) -> list[dict]:
    """Queue one reminder per contactable party, per channel.

    The UNIQUE constraint on (hearing_id, party_id, channel, scheduled_for)
    is what guarantees a reminder is never queued twice for the same
    hearing, however many times this is called.
    """
    h = query_one("SELECT * FROM hearings WHERE id = ?", (hearing_id,))
    if not h:
        return []
    when = (datetime.fromisoformat(h["starts_at"]) -
            timedelta(days=days_before)).isoformat()
    queued: list[dict] = []
    for p in query_all("SELECT * FROM parties WHERE case_id = ?", (h["case_id"],)):
        targets = []
        if p["email"]:
            targets.append(("email", p["email"]))
        if p["phone"]:
            targets.append(("sms", p["phone"]))
        if not targets:
            queued.append({
                "party": p["name"],
                "status": "served_through_prison" if p["in_custody"]
                          else "no_contact_on_record",
            })
            continue
        for channel, dest in targets:
            with cursor() as cur:
                cur.execute(
                    """INSERT OR IGNORE INTO reminders
                       (id, hearing_id, party_id, channel, destination,
                        scheduled_for, status)
                       VALUES (?, ?, ?, ?, ?, ?, 'queued')""",
                    (str(uuid.uuid4()), hearing_id, p["id"], channel, dest, when))
            queued.append({"party": p["name"], "channel": channel,
                           "destination": dest, "scheduled_for": when,
                           "status": "queued"})
    return queued


@router.post("/reminders")
def queue_reminders(body: dict = Body(...),
                    principal: Principal = Depends(current_principal)) -> dict:
    if principal.role.value not in _MAY_SCHEDULE:
        raise HTTPException(403, "Not permitted to queue reminders.")
    hearing_id = body.get("hearing_id")
    hearing = query_one(
        """SELECT h.id, c.jurisdiction FROM hearings h
           JOIN cases c ON c.id = h.case_id WHERE h.id = ?""", (hearing_id,))
    if not hearing:
        raise HTTPException(404, "Unknown hearing id.")
    if hearing["jurisdiction"] != principal.jurisdiction and \
            principal.role.value != "registrar":
        raise HTTPException(404, "Unknown hearing id.")
    return {
        "hearing_id": hearing_id,
        "queued": queue_reminders_for(hearing_id,
                                      int(body.get("days_before", 3))),
        "note": "Queued only. Dispatch goes through the SMS/email connectors, "
                "which are mocks -- nothing leaves the building.",
    }


@router.get("/reminders/due")
def due_reminders(
    before: str | None = Query(default=None, description="ISO8601; default now"),
    principal: Principal = Depends(current_principal),
) -> dict:
    """What a dispatch worker would pick up. Nothing is sent here."""
    cutoff = before or _now()
    rows = query_all(
        """SELECT r.*, c.case_number, h.starts_at AS hearing_at
           FROM reminders r
           JOIN hearings h ON h.id = r.hearing_id
           JOIN cases c ON c.id = h.case_id
           WHERE r.status = 'queued' AND r.scheduled_for <= ?
           ORDER BY r.scheduled_for""", (cutoff,))
    return {"cutoff": cutoff, "count": len(rows), "reminders": rows}
