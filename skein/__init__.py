"""skein: content-hash-native parallel station.

Slice 1 — the content-hash-native store. Folio identity is the content hash of
five canonical fields (type, title, content, created_at, created_by); there is
no human folio_id. See brief-20260529-i6fy.
"""

from .bridge import (
    ImportReport,
    classify_endpoint,
    import_project,
    open_legacy,
)
from .identity import (
    compute_folio_hash,
    compute_thread_hash,
    normalize_created_at,
)
from .store import SkeinStore

__all__ = [
    "SkeinStore",
    "compute_folio_hash",
    "compute_thread_hash",
    "normalize_created_at",
    "ImportReport",
    "classify_endpoint",
    "import_project",
    "open_legacy",
]
