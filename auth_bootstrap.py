"""Create one server-provisioned account from environment variables.

Set AUTH_BOOTSTRAP_* only for this one-shot command, then remove them.
"""
import os
from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

from app.db import cursor, init_db, query_one
from app.security import Role

init_db()
required = ("AUTH_BOOTSTRAP_USER_ID", "AUTH_BOOTSTRAP_PASSWORD",
            "AUTH_BOOTSTRAP_ROLE", "AUTH_BOOTSTRAP_JURISDICTION")
missing = [key for key in required if not os.environ.get(key)]
if missing:
    raise SystemExit(f"Missing environment variables: {', '.join(missing)}")
try:
    role = Role(os.environ["AUTH_BOOTSTRAP_ROLE"])
except ValueError as exc:
    raise SystemExit(f"Invalid role: {exc}")
user_id = os.environ["AUTH_BOOTSTRAP_USER_ID"]
if query_one("SELECT 1 FROM users WHERE id = ?", (user_id,)):
    raise SystemExit("User already exists.")
with cursor() as cur:
    cur.execute(
        """INSERT INTO users (id, password_hash, display_name, role, jurisdiction, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, generate_password_hash(os.environ["AUTH_BOOTSTRAP_PASSWORD"]),
         os.environ.get("AUTH_BOOTSTRAP_DISPLAY_NAME", user_id), role.value,
         os.environ["AUTH_BOOTSTRAP_JURISDICTION"], datetime.now(timezone.utc).isoformat()),
    )
print(f"Created {user_id} with role {role.value}.")
