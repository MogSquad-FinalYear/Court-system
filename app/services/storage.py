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
from urllib.error import HTTPError
from urllib.request import urlopen
from pathlib import Path
from typing import BinaryIO

from app.config import (
    BLOB_DIR, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET, CLOUDINARY_CLOUD_NAME,
    CLOUDINARY_URL,
)

if CLOUDINARY_URL:
    import cloudinary
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
    )

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
        if CLOUDINARY_URL:
            import cloudinary.uploader
            cloudinary.uploader.upload(
                str(final),
                resource_type="raw",
                public_id=f"court/blobs/{digest}",
                overwrite=True,
                unique_filename=False,
            )
        return digest, size
    finally:
        if tmp.exists():
            tmp.unlink()


def verify(content_hash: str) -> bool:
    """Re-hash a stored blob and compare. Used by the integrity endpoint."""
    path = ensure_local(content_hash)
    return path.exists() and hash_file(path) == content_hash


def ensure_local(content_hash: str) -> Path:
    path = blob_path(content_hash)
    if path.exists() or not CLOUDINARY_URL:
        return path
    import cloudinary.utils
    url, _ = cloudinary.utils.cloudinary_url(
        f"court/blobs/{content_hash}", resource_type="raw", secure=True
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urlopen(url, timeout=60) as response, path.open("wb") as target:
            shutil.copyfileobj(response, target)
    except HTTPError:
        return path
    if hash_file(path) != content_hash:
        path.unlink()
        raise ValueError("Downloaded blob failed integrity verification.")
    return path
