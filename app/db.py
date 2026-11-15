"""SQLite schema and access helpers.

SQLite (stdlib, no server) is deliberate for a scaffold: FTS5 gives real
full-text search over extracted document text with zero extra
infrastructure, and the whole store is one file you can inspect or delete.
Swapping to Postgres later means replacing this module, not the routers --
every query lives here.
"""
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import DATABASE_URL, DB_PATH

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- A logical document. Its content lives in document_versions; this row
-- only carries identity, routing and access metadata.
CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    case_number     TEXT,
    title           TEXT NOT NULL,
    doc_type        TEXT NOT NULL DEFAULT 'unclassified',
    jurisdiction    TEXT NOT NULL,
    classification  TEXT NOT NULL DEFAULT 'restricted',
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_case ON documents(case_number);
CREATE INDEX IF NOT EXISTS idx_documents_jurisdiction ON documents(jurisdiction);

-- Immutable versions. Never UPDATEd -- a correction is a new row.
CREATE TABLE IF NOT EXISTS document_versions (
    id              TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version_no      INTEGER NOT NULL,
    filename        TEXT NOT NULL,
    media_type      TEXT,
    size_bytes      INTEGER NOT NULL,
    content_hash    TEXT NOT NULL,          -- sha256 of the stored bytes
    uploaded_at     TEXT NOT NULL,
    uploaded_by     TEXT NOT NULL,
    note            TEXT,
    -- Landing AI output
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    markdown        TEXT,
    page_count      INTEGER,
    element_count   INTEGER,
    fields_json     TEXT,                   -- extract() result
    extraction_error TEXT,
    UNIQUE (document_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_versions_doc ON document_versions(document_id);
CREATE INDEX IF NOT EXISTS idx_versions_hash ON document_versions(content_hash);

-- Signatures and court seals, both bound to one exact version.
CREATE TABLE IF NOT EXISTS attestations (
    id              TEXT PRIMARY KEY,
    version_id      TEXT NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,          -- 'signature' | 'seal'
    actor_id        TEXT NOT NULL,
    actor_role      TEXT NOT NULL,
    court_id        TEXT,
    seal_number     TEXT,
    content_hash    TEXT NOT NULL,          -- hash attested to, at signing time
    signature       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_attestations_version ON attestations(version_id);

-- Append-only access log. Every read of a document body lands here.
CREATE TABLE IF NOT EXISTS access_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    actor_id        TEXT NOT NULL,
    actor_role      TEXT NOT NULL,
    jurisdiction    TEXT NOT NULL,
    document_id     TEXT,
    action          TEXT NOT NULL,
    allowed         INTEGER NOT NULL,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_access_doc ON access_log(document_id);

-- ===================================================================
-- Case Initiation & Filing
-- ===================================================================

-- Gap-free case-number allocation. One row per (court, year, case_type);
-- the allocator bumps last_no inside the same transaction that writes the
-- case, so two concurrent filings cannot take the same number.
CREATE TABLE IF NOT EXISTS case_number_sequence (
    court_id    TEXT NOT NULL,
    year        INTEGER NOT NULL,
    case_type   TEXT NOT NULL,
    last_no     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (court_id, year, case_type)
);

CREATE TABLE IF NOT EXISTS cases (
    id            TEXT PRIMARY KEY,
    case_number   TEXT NOT NULL UNIQUE,
    case_type     TEXT NOT NULL,
    court_id      TEXT NOT NULL,
    jurisdiction  TEXT NOT NULL,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'filed',
    filed_at      TEXT NOT NULL,
    filed_by      TEXT NOT NULL,
    cause_of_action_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_juris ON cases(jurisdiction);

CREATE TABLE IF NOT EXISTS parties (
    id          TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    side        TEXT NOT NULL,          -- petitioner | respondent
    name        TEXT NOT NULL,
    advocate    TEXT,                   -- advocate on record, if any
    email       TEXT,
    phone       TEXT,
    in_custody  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_parties_case ON parties(case_id);

-- A filing is an *attempt*. It becomes a case only once it clears scrutiny,
-- so rejected filings stay on record with their objections rather than
-- vanishing.
CREATE TABLE IF NOT EXISTS filings (
    id              TEXT PRIMARY KEY,
    case_id         TEXT REFERENCES cases(id) ON DELETE SET NULL,
    case_type       TEXT NOT NULL,
    court_id        TEXT NOT NULL,
    jurisdiction    TEXT NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL,      -- accepted | rejected
    fee_amount      REAL,
    fee_currency    TEXT,
    fee_breakdown   TEXT,               -- JSON
    objections      TEXT,               -- JSON list; empty when accepted
    submitted_at    TEXT NOT NULL,
    submitted_by    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filings_case ON filings(case_id);

-- Which Document Management documents were attached to a filing.
CREATE TABLE IF NOT EXISTS filing_documents (
    filing_id   TEXT NOT NULL REFERENCES filings(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,          -- which required slot it satisfies
    PRIMARY KEY (filing_id, document_id)
);

-- ===================================================================
-- Scheduling & Hearings
-- ===================================================================

CREATE TABLE IF NOT EXISTS judges (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    court_id      TEXT NOT NULL,
    sitting_start TEXT NOT NULL DEFAULT '10:30',
    sitting_end   TEXT NOT NULL DEFAULT '16:30',
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS courtrooms (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    court_id      TEXT NOT NULL,
    capacity      INTEGER NOT NULL DEFAULT 40,
    video_link    INTEGER NOT NULL DEFAULT 0,   -- in-custody appearances
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS court_holidays (
    court_id  TEXT NOT NULL,
    day       TEXT NOT NULL,            -- YYYY-MM-DD
    reason    TEXT,
    PRIMARY KEY (court_id, day)
);

CREATE TABLE IF NOT EXISTS hearings (
    id           TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    judge_id     TEXT NOT NULL REFERENCES judges(id),
    courtroom_id TEXT NOT NULL REFERENCES courtrooms(id),
    starts_at    TEXT NOT NULL,         -- ISO8601
    ends_at      TEXT NOT NULL,
    purpose      TEXT,
    status       TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled|part_heard|disposed|adjourned
    item_no      INTEGER,               -- position on the cause list
    requires_video INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hearings_judge ON hearings(judge_id, starts_at);
CREATE INDEX IF NOT EXISTS idx_hearings_room ON hearings(courtroom_id, starts_at);
CREATE INDEX IF NOT EXISTS idx_hearings_case ON hearings(case_id);

-- Queued only. Nothing is actually dispatched -- see services/connectors.py:
-- the SMS and email connectors are mocks.
CREATE TABLE IF NOT EXISTS reminders (
    id            TEXT PRIMARY KEY,
    hearing_id    TEXT NOT NULL REFERENCES hearings(id) ON DELETE CASCADE,
    party_id      TEXT NOT NULL REFERENCES parties(id) ON DELETE CASCADE,
    channel       TEXT NOT NULL,        -- sms | email
    destination   TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued|sent|failed|skipped
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    UNIQUE (hearing_id, party_id, channel, scheduled_for)
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, scheduled_for);

-- ===================================================================
-- Integration Hub
-- ===================================================================

-- Every outbound call, keyed by idempotency key. A repeated key returns the
-- stored response instead of calling again -- a double-charged court fee or
-- a duplicate produce warrant are both serious.
CREATE TABLE IF NOT EXISTS connector_calls (
    idempotency_key TEXT PRIMARY KEY,
    connector_id    TEXT NOT NULL,
    operation       TEXT NOT NULL,
    request_json    TEXT,
    response_json   TEXT,
    status          TEXT NOT NULL,      -- ok | failed
    attempts        INTEGER NOT NULL DEFAULT 1,
    called_at       TEXT NOT NULL,
    called_by       TEXT NOT NULL,
    duration_ms      INTEGER,
    owner_user_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_connector_calls ON connector_calls(connector_id, called_at);

-- Circuit breaker state per connector, so one dead upstream cannot stall
-- filing for everyone.
CREATE TABLE IF NOT EXISTS connector_health (
    connector_id      TEXT PRIMARY KEY,
    consecutive_fails INTEGER NOT NULL DEFAULT 0,
    opened_at         TEXT,             -- non-null while the breaker is open
    last_checked      TEXT
);

-- Full-text index over extracted text. Kept in sync explicitly by
-- services/search.py rather than by trigger, so a re-extraction can
-- replace a version's text without a delete/insert dance in SQL.
CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    version_id UNINDEXED,
    document_id UNINDEXED,
    title,
    markdown,
    tokenize = 'porter unicode61'
);

-- Authentication and authorization state. Password hashes are generated by
-- Werkzeug and refresh tokens are stored only as SHA-256 digests.
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    password_hash   TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    role            TEXT NOT NULL,
    jurisdiction    TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id       TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    revoked_at      TEXT,
    replaced_by     TEXT
);
CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id);
CREATE TABLE IF NOT EXISTS revoked_access_tokens (
    jti             TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    revoked_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS security_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    actor_id        TEXT,
    actor_role      TEXT,
    jurisdiction    TEXT,
    action          TEXT NOT NULL,
    result          TEXT NOT NULL,
    request_id      TEXT,
    detail          TEXT
);
"""


def _postgres_schema() -> str:
    schema = re.sub(r"^PRAGMA .*?;\n", "", SCHEMA, flags=re.MULTILINE)
    schema = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    schema = schema.replace("REAL", "DOUBLE PRECISION")
    schema = schema.replace(
        """CREATE VIRTUAL TABLE IF NOT EXISTS document_fts USING fts5(
    version_id UNINDEXED,
    document_id UNINDEXED,
    title,
    markdown,
    tokenize = 'porter unicode61'
);""",
        """CREATE TABLE IF NOT EXISTS document_fts (
    version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL,
    search_vector tsvector NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_document_fts_search ON document_fts USING GIN(search_vector);""",
    )
    return schema


def _connect():
    if DATABASE_URL:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        if DATABASE_URL:
            conn.execute(_postgres_schema())
        else:
            conn.executescript(SCHEMA)
            columns = {r["name"] for r in conn.execute(
                "PRAGMA table_info(connector_calls)").fetchall()}
            if "owner_user_id" not in columns:
                conn.execute("ALTER TABLE connector_calls ADD COLUMN owner_user_id TEXT")


class _CursorAdapter:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, params: tuple[Any, ...] = ()):
        if DATABASE_URL:
            sql = sql.replace("?", "%s")
        return self._cur.execute(sql, params)

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


@contextmanager
def cursor() -> Iterator:
    """Transactional cursor -- commits on clean exit, rolls back on error."""
    conn = _connect()
    try:
        cur = _CursorAdapter(conn.cursor())
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict]:
    with cursor() as cur:
        return [dict(r) for r in cur.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple[Any, ...] = ()) -> dict | None:
    with cursor() as cur:
        row = cur.execute(sql, params).fetchone()
        return dict(row) if row else None
