"""Seed court resources (judges, courtrooms, holidays) for a usable demo."""
import sys
sys.path.insert(0, ".")
from app.db import cursor, init_db
from app.config import COURT_ID

init_db()
JUDGES = [
    ("j-anand",  "Hon'ble Mr Justice Anand Rao", "10:30", "16:30"),
    ("j-fatima", "Hon'ble Ms Justice Fatima Khan", "11:00", "17:00"),
]
ROOMS = [
    ("cr-1", "Court Hall 1", 60, 1),
    ("cr-2", "Court Hall 2", 40, 0),
]
HOLIDAYS = [("2026-12-25", "Christmas"), ("2026-01-26", "Republic Day")]

with cursor() as cur:
    for jid, name, s, e in JUDGES:
        cur.execute("INSERT OR REPLACE INTO judges (id,name,court_id,sitting_start,sitting_end) "
                    "VALUES (?,?,?,?,?)", (jid, name, COURT_ID, s, e))
    for rid, name, cap, vid in ROOMS:
        cur.execute("INSERT OR REPLACE INTO courtrooms (id,name,court_id,capacity,video_link) "
                    "VALUES (?,?,?,?,?)", (rid, name, COURT_ID, cap, vid))
    for day, reason in HOLIDAYS:
        cur.execute("INSERT OR REPLACE INTO court_holidays (court_id,day,reason) "
                    "VALUES (?,?,?)", (COURT_ID, day, reason))
print(f"seeded {len(JUDGES)} judges, {len(ROOMS)} courtrooms, {len(HOLIDAYS)} holidays")
