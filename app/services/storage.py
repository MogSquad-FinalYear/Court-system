"""Content-addressed blob store.

Files are stored under their SHA-256, not their filename. Three properties
fall out of that for free, all of which this feature needs:

  * tamper evidence -- the path *is* the hash, so a modified byte cannot
    occupy the same slot as the bytes that were signed or sealed,
  * deduplication -- re-filing an identical annexure across cases costs one
    copy, while each case still gets its own version row,
  * safe filenames -- the user's original filename never touches the
    filesystem, so path traversal via upload name is not possible.
"""
import hashlib
import shutil
from pathlib import Path
from typing import BinaryIO

from app.config import BLOB_DIR

_CHUNK = 1024 * 1024


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def blob_path(content_hash: str) -> Path:
    """Two-level fan-out so one directory never holds a million entries."""
    return BLOB_DIR / content_hash[:2] / content_hash[2:4] / content_hash


def store_stream(src: BinaryIO) -> tuple[str, int]:
    """Stream a stream to a temp file, hash as we go, then move into place.

    Streaming rather than reading into memory because court filings are
    routinely scanned PDFs of several hundred pages. Returns
    (content_hash, size_bytes).
    """
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BLOB_DIR / f".incoming-{id(src)}"
    h = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as out:
            while chunk := src.read(_CHUNK):
                h.update(chunk)
                size += len(chunk)
                out.write(chunk)
        digest = h.hexdigest()
        final = blob_path(digest)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            tmp.unlink()        # identical bytes already stored
        else:
            shutil.move(str(tmp), str(final))
        return digest, size
    finally:
        if tmp.exists():
            tmp.unlink()


def verify(content_hash: str) -> bool:
    """Re-hash a stored blob and compare. Used by the integrity endpoint."""
    path = blob_path(content_hash)
    return path.exists() and hash_file(path) == content_hash
