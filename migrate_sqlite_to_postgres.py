import sqlite3
from pathlib import Path

import cloudinary.uploader
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.config import (
    BLOB_DIR, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET, CLOUDINARY_CLOUD_NAME,
    CLOUDINARY_URL, DATABASE_URL, DB_PATH,
)
from app.db import init_db, query_all
from app.services.search import index_version

if CLOUDINARY_URL:
    import cloudinary
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
    )

TABLES = (
    "documents",
    "document_versions",
    "attestations",
    "access_log",
    "case_number_sequence",
    "cases",
    "parties",
    "filings",
    "filing_documents",
    "judges",
    "courtrooms",
    "court_holidays",
    "hearings",
    "reminders",
    "connector_calls",
    "connector_health",
    "users",
    "refresh_tokens",
    "revoked_access_tokens",
    "security_events",
)


def migrate_table(source: sqlite3.Connection, target, table: str) -> int:
    columns = [row[1] for row in source.execute(f"PRAGMA table_info({table})")]
    rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
    if not rows:
        return 0
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
    )
    target.executemany(statement, rows)
    return len(rows)


def migrate_blobs() -> int:
    if not CLOUDINARY_URL:
        return 0
    count = 0
    for path in BLOB_DIR.glob("*/*/*"):
        if path.is_file() and not path.name.startswith(".incoming-"):
            cloudinary.uploader.upload(
                str(path),
                resource_type="raw",
                public_id=f"court/blobs/{path.name}",
                overwrite=True,
                unique_filename=False,
            )
            count += 1
    return count


def main() -> None:
    if not DATABASE_URL:
        raise SystemExit("DATABASE_URL is required.")
    if not Path(DB_PATH).exists():
        raise SystemExit(f"SQLite database not found: {DB_PATH}")
    init_db()
    source = sqlite3.connect(DB_PATH)
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as connection:
        with connection.cursor() as target:
            copied = {table: migrate_table(source, target, table) for table in TABLES}
        connection.commit()
    source.close()
    blobs = migrate_blobs()
    for row in query_all(
        """SELECT v.id, v.document_id, d.title, v.markdown
           FROM document_versions v
           JOIN documents d ON d.id = v.document_id
           WHERE v.markdown IS NOT NULL"""
    ):
        index_version(row["id"], row["document_id"], row["title"], row["markdown"])
    print({"tables": copied, "cloudinary_blobs": blobs})


if __name__ == "__main__":
    main()
