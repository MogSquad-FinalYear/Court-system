"""Full-text search over Landing AI-extracted document text.

The searchable corpus is the Markdown that ADE's parse() returns, not the
raw file -- that is the reason extraction is part of upload rather than an
optional extra. A scanned PDF is opaque to search until it has been parsed.

Results are filtered for access *after* the FTS query rather than inside it:
the index has no idea about roles, and encoding the policy into SQL would
mean two places to keep in sync with security.py. Over-fetching and
filtering keeps one source of truth for who may see what.
"""
from app.config import DATABASE_URL
from app.db import cursor, query_all
from app.security import Principal


def index_version(version_id: str, document_id: str, title: str, markdown: str) -> None:
    """(Re)index one version. Safe to call repeatedly after re-extraction."""
    with cursor() as cur:
        cur.execute("DELETE FROM document_fts WHERE version_id = ?", (version_id,))
        cur.execute(
            "INSERT INTO document_fts "
            "(version_id, document_id, title, markdown, search_vector) "
            "VALUES (?, ?, ?, ?, to_tsvector('simple', ?))"
            if DATABASE_URL else
            "INSERT INTO document_fts (version_id, document_id, title, markdown) "
            "VALUES (?, ?, ?, ?)",
            (version_id, document_id, title or "", markdown or "", markdown or "")
            if DATABASE_URL else
            (version_id, document_id, title or "", markdown or ""),
        )


def drop_document(document_id: str) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM document_fts WHERE document_id = ?", (document_id,))


def _escape(q: str) -> str:
    """Quote each term so user input can't be read as FTS5 syntax.

    Without this, a query containing a bare '*' or an unbalanced quote is a
    500 from sqlite rather than zero results.
    """
    terms = [t for t in q.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in terms)


def search(principal: Principal, q: str, limit: int = 25) -> list[dict]:
    match = _escape(q)
    if not match:
        return []

    sql = """
        SELECT f.version_id,
               f.document_id,
               d.title,
               d.case_number,
               d.doc_type,
               d.jurisdiction,
               d.classification,
               v.version_no,
               v.uploaded_at,
               {excerpt},
               {rank}
        FROM document_fts f
        JOIN documents d ON d.id = f.document_id
        JOIN document_versions v ON v.id = f.version_id
        WHERE {match}
        ORDER BY rank {order}
        LIMIT ?
        """.format(
            excerpt=(
                "ts_headline('simple', f.markdown, plainto_tsquery('simple', ?), "
                "'StartSel=[,StopSel=],MaxFragments=1,MaxWords=18') AS excerpt"
                if DATABASE_URL else
                "snippet(document_fts, 3, '[', ']', ' … ', 18) AS excerpt"
            ),
            rank=(
                "ts_rank(f.search_vector, plainto_tsquery('simple', ?)) AS rank"
                if DATABASE_URL else "bm25(document_fts) AS rank"
            ),
            match=(
                "f.search_vector @@ plainto_tsquery('simple', ?)"
                if DATABASE_URL else "document_fts MATCH ?"
            ),
            order="DESC" if DATABASE_URL else "ASC",
        )
    params = (match, match, match, limit * 4) if DATABASE_URL else (match, limit * 4)
    rows = query_all(sql, params)

    visible = [
        r for r in rows
        if principal.may_read(r["classification"], r["jurisdiction"])
    ]
    return visible[:limit]
