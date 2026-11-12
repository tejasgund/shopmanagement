"""
services/document_storage.py - Agreement document storage

Same model as services/photo_storage.py (meter photos), and deliberately reuses
its path-safety and disk helpers rather than copying them:

    - Files are written to a folder on the server's own disk, OUTSIDE any
      static-served directory, and never get a public URL. The only way to
      fetch one is GET /api/agreement-documents/{id}/download, which checks
      the caller is an admin or the tenant the document belongs to.
    - Stored filenames are generated here (never taken from the client), so an
      upload name cannot traverse directories or overwrite another file.
    - The file type is decided from the file's leading bytes (magic bytes), not
      its extension, so renaming evil.exe to agreement.pdf is refused.
"""

import os
import uuid
from datetime import datetime
from typing import Tuple

from core.logger import get_logger
from services import photo_storage
from services.photo_storage import PhotoStorageError as DocumentStorageError  # noqa: F401  (re-export)

logger = get_logger("app")


class DocumentValidationError(Exception):
    """Raised when an uploaded file is not an acceptable agreement document."""


# Magic bytes -> (extension, mime). PDF for typed/scanned agreements, JPG/PNG
# for a phone photo of a signed paper copy.
_SIGNATURES = (
    (b"%PDF-",              "pdf", "application/pdf"),
    (b"\xff\xd8\xff",       "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n",  "png", "image/png"),
)

ALLOWED_LABEL = "PDF, JPG or PNG"


def validate(content: bytes, max_mb: int) -> Tuple[str, str]:
    """Check an upload before it is stored. Returns (extension, mime)."""
    if not content:
        raise DocumentValidationError("The uploaded file is empty.")

    max_bytes = int(max_mb) * 1024 * 1024
    if len(content) > max_bytes:
        raise DocumentValidationError(
            f"File is {len(content) / (1024 * 1024):.1f} MB, which is over the "
            f"{max_mb} MB limit."
        )

    if len(content) < 100:
        raise DocumentValidationError("The file appears to be corrupted or incomplete.")

    # A PDF may have a few junk bytes (e.g. a BOM) before the header; the spec
    # allows the header anywhere in the first 1024 bytes.
    if b"%PDF-" in content[:1024]:
        return "pdf", "application/pdf"
    for signature, ext, mime in _SIGNATURES:
        if content.startswith(signature):
            return ext, mime

    raise DocumentValidationError(f"Unsupported file. Please upload a {ALLOWED_LABEL} file.")


def build_key(user_id: int, shop_id: int, ext: str, when: datetime = None) -> str:
    """
    Relative storage key, e.g.
        user-12/shop-4/2026/11/20261113-a3f9c1e2b4d5.pdf
    """
    when = when or datetime.now()
    unique = uuid.uuid4().hex[:12]
    return (
        f"user-{int(user_id)}/shop-{int(shop_id)}/"
        f"{when:%Y}/{when:%m}/{when:%Y%m%d}-{unique}.{ext}"
    )


# Disk helpers: identical behaviour to meter photos, including the
# "never escape the base folder" check and "never overwrite" on save.
save = photo_storage.save
read = photo_storage.read
exists = photo_storage.exists
safe_original_name = photo_storage.safe_original_name


def delete(base_dir: str, key: str) -> bool:
    """
    Remove a stored file. Returns True if a file was removed.

    Never raises for a file that is already gone - the database row is the
    thing being deleted; a missing file must not block that.
    """
    try:
        target = photo_storage._full_path(base_dir, key)
    except photo_storage.PhotoStorageError:
        return False
    try:
        os.remove(target)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.error("Failed deleting agreement document %s: %s", target, exc)
        return False
