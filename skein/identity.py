"""Content-hash identity for skein folios.

Folio identity is the content hash of five canonical fields:
``type, title, content, created_at, created_by``. This is the exact field set
and knurl canonicalization used by the legacy store (``skein/storage.py``
``compute_folio_hash``) — adopted deliberately, not re-guessed.

The one place identity can silently break is ``created_at``: the same logical
instant arrives over the wire in incompatible encodings (timezone-naive, aware
with an offset, ``Z``-suffixed, a raw SQLite string, whole-second resolution).
``normalize_created_at`` collapses all of them to one canonical representation —
timezone-aware UTC isoformat — *before* the value enters the canonical bytes, so
a folio hashes identically no matter how its timestamp was expressed. See
open-call #1 in brief-20260529-i6fy.

Address spelling note: the stored hash uses the ``sha256::<hex>`` address form.
The addressing RSP (brief-20260529-0yjl) owns the final spelling; this module
just stays consistent with it. The underlying SHA-256 digest is identical to the
legacy ``folio:sha256:<hex>`` digest — only the prefix framing differs.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from knurl import hash as knurl_hash

# The canonical-bytes producer now lives in canon.py so the signer can sign
# EXACTLY the bytes the hasher hashes (publish-path + signing specs). identity
# stays the address layer: it frames those bytes' digest as a sha256:: address.
# Re-exported here so existing importers (store.py, wire.py) keep working.
from .canon import (  # noqa: F401
    CANONICAL_FIELDS,
    CreatedAt,
    folio_canonical_bytes,
    normalize_created_at,
    thread_canonical_bytes,
)


def content_hash_for_bytes(canonical_bytes: bytes) -> str:
    """The ``sha256::<hex>`` address of already-canonicalized bytes.

    Hashing the raw bytes (``knurl.hash.compute_bytes``) — not a
    ``bytes -> str -> re-encode`` round-trip — makes "the bytes hashed are exactly
    the bytes signed" structurally true rather than an implicit contract: the
    signer signs ``folio_canonical_bytes(...)`` (bytes), and this hashes the same
    bytes object, so the two can never disagree even if canon ever emitted
    non-UTF-8 or knurl's string path re-encoded differently (zr29 CRITICAL #1).

    Public so a caller that already holds the canonical bytes (the verifier) can
    take the hash without re-serializing them.
    """
    # knurl returns 'sha256:<hex>'; reframe to the address form 'sha256::<hex>'.
    digest = knurl_hash.compute_bytes(canonical_bytes)
    return "sha256::" + digest.split(":", 1)[1]


def hash_token(token: str) -> str:
    """The hex SHA-256 of an invite token — its at-rest identity (INV/schema).

    The plaintext token is NEVER stored; only this hash. ONE helper so the mint
    side, the redeem ceremony (the token-bound challenge), and the store's
    lookup/CAS all compute the IDENTICAL hash and can never disagree. Returns a
    bare 64-char lowercase hex digest (NOT the ``sha256::`` address form — this is a
    secret's fingerprint, not a content address)."""
    import hashlib

    if not isinstance(token, str):
        raise TypeError(f"hash_token requires a str token, got {type(token).__name__}")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def compute_folio_hash(fields: Mapping[str, Any]) -> str:
    """Compute a folio's content hash from its five canonical fields.

    Only ``type, title, content, created_at, created_by`` participate; any other
    keys in ``fields`` are ignored. The canonical bytes come from
    :func:`skein.canon.folio_canonical_bytes` — the same bytes the signer
    signs — so a folio's hash and its signature can never disagree on what was
    hashed.
    """
    return content_hash_for_bytes(folio_canonical_bytes(fields))


def compute_thread_hash(
    from_id: Optional[str],
    to_id: Optional[str],
    type: Optional[str],
    weaver: Optional[str],
    created_at: CreatedAt,
    content: Optional[str],
) -> str:
    """Compute a thread's content hash from its canonical fields.

    Threads are content-addressed like folios so save is idempotent. The
    canonical bytes come from :func:`skein.canon.thread_canonical_bytes`.
    """
    return content_hash_for_bytes(
        thread_canonical_bytes(from_id, to_id, type, weaver, created_at, content)
    )
