"""Hearing scheduling: conflicts, availability and cause lists.

A slot is only free if *three* scarce resources are simultaneously free --
the judge, the courtroom, and every advocate appearing in the matter. The
advocate leg is the hard one and the one most systems get wrong: an
advocate can be listed before several different courts on the same morning,
so their conflicts have to be computed across every hearing in the system,
not just the calendar of the court doing the scheduling. That is why
`_advocate_conflicts` joins through parties rather than filtering by court.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.db import query_all, query_one

SLOT_MINUTES = 30


def _parse(dt: str) -> datetime:
    return datetime.fromisoformat(dt)


def _overlap_clause(column_start: str, column_end: str) -> str:
    """Half-open overlap: [a,b) intersects [c,d) iff a < d and c < b.

    Half-open matters -- a hearing ending at 11:00 and one starting at
    11:00 do not conflict, and a closed comparison would wrongly reject it.
    """
    return f"({column_start} < ? AND ? < {column_end})"


def is_holiday(court_id: str, day: date) -> dict | None:
    return query_one(
        "SELECT * FROM court_holidays WHERE court_id = ? AND day = ?",
        (court_id, day.isoformat()),
    )


def _judge_conflicts(judge_id: str, starts_at: str, ends_at: str,
                     exclude_hearing_id: str | None) -> list[dict]:
    sql = f"""SELECT h.*, c.case_number FROM hearings h
              JOIN cases c ON c.id = h.case_id
              WHERE h.judge_id = ? AND h.status != 'adjourned'
                AND {_overlap_clause('h.starts_at', 'h.ends_at')}"""
    params: list = [judge_id, ends_at, starts_at]
    if exclude_hearing_id:
        sql += " AND h.id != ?"
        params.append(exclude_hearing_id)
    return query_all(sql, tuple(params))


def _courtroom_conflicts(courtroom_id: str, starts_at: str, ends_at: str,
                         exclude_hearing_id: str | None) -> list[dict]:
    sql = f"""SELECT h.*, c.case_number FROM hearings h
              JOIN cases c ON c.id = h.case_id
              WHERE h.courtroom_id = ? AND h.status != 'adjourned'
                AND {_overlap_clause('h.starts_at', 'h.ends_at')}"""
    params: list = [courtroom_id, ends_at, starts_at]
    if exclude_hearing_id:
        sql += " AND h.id != ?"
        params.append(exclude_hearing_id)
    return query_all(sql, tuple(params))


def _advocate_conflicts(case_id: str, starts_at: str, ends_at: str,
                        exclude_hearing_id: str | None) -> list[dict]:
    """Advocates in this case who are already listed anywhere in that slot.

    Deliberately not scoped to one court: a double-listing across two courts
    at the same hour is exactly the conflict that wastes a hearing.
    """
    advocates = [
        r["advocate"] for r in query_all(
            "SELECT DISTINCT advocate FROM parties "
            "WHERE case_id = ? AND advocate IS NOT NULL AND advocate != ''",
            (case_id,),
        )
    ]
    if not advocates:
        return []

    placeholders = ",".join("?" for _ in advocates)
    sql = f"""SELECT DISTINCT p.advocate, h.id AS hearing_id, h.starts_at,
                     h.ends_at, c.case_number, c.court_id
              FROM hearings h
              JOIN cases c ON c.id = h.case_id
              JOIN parties p ON p.case_id = c.id
              WHERE p.advocate IN ({placeholders})
                AND h.status != 'adjourned'
                AND h.case_id != ?
                AND {_overlap_clause('h.starts_at', 'h.ends_at')}"""
    params: list = [*advocates, case_id, ends_at, starts_at]
    if exclude_hearing_id:
        sql += " AND h.id != ?"
        params.append(exclude_hearing_id)
    return query_all(sql, tuple(params))


def check_conflicts(
    *,
    case_id: str,
    judge_id: str,
    courtroom_id: str,
    starts_at: str,
    ends_at: str,
    requires_video: bool = False,
    exclude_hearing_id: str | None = None,
) -> list[dict]:
    """Every reason this slot cannot be used. Empty list means it is free."""
    problems: list[dict] = []
    start, end = _parse(starts_at), _parse(ends_at)

    if end <= start:
        return [{"kind": "invalid_window",
                 "detail": "ends_at must be after starts_at."}]

    judge = query_one("SELECT * FROM judges WHERE id = ?", (judge_id,))
    room = query_one("SELECT * FROM courtrooms WHERE id = ?", (courtroom_id,))
    if not judge:
        problems.append({"kind": "unknown_judge", "detail": judge_id})
    if not room:
        problems.append({"kind": "unknown_courtroom", "detail": courtroom_id})
    if problems:
        return problems

    if not judge["active"]:
        problems.append({"kind": "judge_inactive", "detail": judge["name"]})
    if not room["active"]:
        problems.append({"kind": "courtroom_inactive", "detail": room["name"]})

    # Courtroom and judge must belong to the same court as each other.
    if judge["court_id"] != room["court_id"]:
        problems.append({
            "kind": "cross_court",
            "detail": f"Judge sits in {judge['court_id']} but courtroom is in "
                      f"{room['court_id']}.",
        })

    hol = is_holiday(judge["court_id"], start.date())
    if hol:
        problems.append({
            "kind": "court_holiday",
            "detail": f"{start.date().isoformat()}: {hol['reason'] or 'holiday'}",
        })

    # Sitting hours
    s_start = time.fromisoformat(judge["sitting_start"])
    s_end = time.fromisoformat(judge["sitting_end"])
    if start.time() < s_start or end.time() > s_end:
        problems.append({
            "kind": "outside_sitting_hours",
            "detail": f"{judge['name']} sits {judge['sitting_start']}–"
                      f"{judge['sitting_end']}; requested "
                      f"{start.time().isoformat(timespec='minutes')}–"
                      f"{end.time().isoformat(timespec='minutes')}.",
        })

    # An in-custody appearance needs a room wired for video.
    if requires_video and not room["video_link"]:
        problems.append({
            "kind": "no_video_link",
            "detail": f"{room['name']} has no video link; an in-custody "
                      "appearance cannot be heard there.",
        })

    for c in _judge_conflicts(judge_id, starts_at, ends_at, exclude_hearing_id):
        problems.append({
            "kind": "judge_busy",
            "detail": f"{judge['name']} is listed in {c['case_number']} "
                      f"({c['starts_at']}–{c['ends_at']}).",
        })
    for c in _courtroom_conflicts(courtroom_id, starts_at, ends_at, exclude_hearing_id):
        problems.append({
            "kind": "courtroom_busy",
            "detail": f"{room['name']} is occupied by {c['case_number']} "
                      f"({c['starts_at']}–{c['ends_at']}).",
        })
    for c in _advocate_conflicts(case_id, starts_at, ends_at, exclude_hearing_id):
        problems.append({
            "kind": "advocate_double_listed",
            "detail": f"{c['advocate']} is already listed in {c['case_number']} "
                      f"before {c['court_id']} ({c['starts_at']}–{c['ends_at']}).",
        })

    return problems


def availability(
    *,
    case_id: str,
    judge_id: str,
    courtroom_id: str,
    day: date,
    duration_minutes: int = SLOT_MINUTES,
    requires_video: bool = False,
) -> list[dict]:
    """Walk the sitting day in slots and report which are actually free."""
    judge = query_one("SELECT * FROM judges WHERE id = ?", (judge_id,))
    if not judge:
        return []
    s_start = time.fromisoformat(judge["sitting_start"])
    s_end = time.fromisoformat(judge["sitting_end"])

    cursor_dt = datetime.combine(day, s_start)
    end_of_day = datetime.combine(day, s_end)
    step = timedelta(minutes=duration_minutes)
    slots: list[dict] = []

    while cursor_dt + step <= end_of_day:
        slot_end = cursor_dt + step
        problems = check_conflicts(
            case_id=case_id, judge_id=judge_id, courtroom_id=courtroom_id,
            starts_at=cursor_dt.isoformat(), ends_at=slot_end.isoformat(),
            requires_video=requires_video,
        )
        slots.append({
            "starts_at": cursor_dt.isoformat(),
            "ends_at": slot_end.isoformat(),
            "free": not problems,
            "blocked_by": [p["kind"] for p in problems] or None,
        })
        cursor_dt = slot_end
    return slots


# Part-heard matters are taken up before fresh ones: the judge already has
# the case in mind, and leaving it to the end of the list risks it not being
# reached at all.
_STATUS_ORDER = {"part_heard": 0, "scheduled": 1, "adjourned": 2, "disposed": 3}


def cause_list(court_id: str, day: date) -> list[dict]:
    """The day's cause list for a court, in the order items will be taken."""
    start = datetime.combine(day, time.min).isoformat()
    end = datetime.combine(day, time.max).isoformat()
    rows = query_all(
        """SELECT h.*, c.case_number, c.title, c.case_type, c.jurisdiction,
                  j.name AS judge_name, r.name AS courtroom_name
           FROM hearings h
           JOIN cases c ON c.id = h.case_id
           JOIN judges j ON j.id = h.judge_id
           JOIN courtrooms r ON r.id = h.courtroom_id
           WHERE j.court_id = ? AND h.starts_at BETWEEN ? AND ?
           ORDER BY h.starts_at""",
        (court_id, start, end),
    )
    rows.sort(key=lambda r: (
        r["judge_name"],
        _STATUS_ORDER.get(r["status"], 9),
        r["starts_at"],
    ))
    for i, r in enumerate(rows, start=1):
        r["item_no"] = i
        r["advocates"] = [
            p["advocate"] for p in query_all(
                "SELECT DISTINCT advocate FROM parties "
                "WHERE case_id = ? AND advocate IS NOT NULL AND advocate != ''",
                (r["case_id"],),
            )
        ]
    return rows
