"""Case number allocation.

Court case numbers are gap-free sequences per (court, year, case type) and
are quoted in orders, cause lists and correspondence forever. Two
properties matter and both are easy to get wrong:

  * no duplicates -- two filings must never take the same number,
  * no gaps -- a number must not be consumed by a filing that then fails
    scrutiny, because a missing number in a register looks like a destroyed
    record.

Both fall out of allocating inside the *same* transaction that writes the
case row, and only after the filing has already passed validation. A
rejected filing never reaches here, so it never burns a number.
"""
from datetime import date

from app.db import cursor

FORMAT = "{case_type} {serial}/{year}"


def allocate(cur, *, court_id: str, case_type: str, on: date | None = None) -> str:
    """Take the next number in sequence. Must be called with an open cursor.

    Takes the caller's cursor rather than opening its own precisely so the
    increment and the INSERT of the case share one transaction -- if the
    case write fails, the increment rolls back with it.
    """
    year = (on or date.today()).year
    cur.execute(
        """INSERT INTO case_number_sequence (court_id, year, case_type, last_no)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(court_id, year, case_type)
           DO UPDATE SET last_no = last_no + 1
           RETURNING last_no""",
        (court_id, year, case_type),
    )
    row = cur.fetchone()
    serial = row["last_no"] if isinstance(row, dict) else row[0]
    return FORMAT.format(case_type=case_type, serial=serial, year=year)


def peek(court_id: str, case_type: str, on: date | None = None) -> str:
    """What the next number *would* be, without consuming it."""
    year = (on or date.today()).year
    with cursor() as cur:
        row = cur.execute(
            "SELECT last_no FROM case_number_sequence "
            "WHERE court_id = ? AND year = ? AND case_type = ?",
            (court_id, year, case_type),
        ).fetchone()
    return FORMAT.format(
        case_type=case_type,         serial=((row["last_no"] if isinstance(row, dict) else row[0])
                if row else 0) + 1, year=year
    )
