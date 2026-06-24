"""Canonical serialization — the one producer of a folio's signed/hashed bytes.

A folio's identity is the hash of its canonical bytes, and signing must sign
EXACTLY those same bytes. So "the canonical bytes of a folio" must be ONE
function, shared by the hasher (:mod:`skein.identity`) and the
signer/verifier (:mod:`skein.signing`, wired in at the publish boundary). This
module owns that producer.

It is the seam the publish-path spec (brief-20260601-nqtj) and the signing brief
(brief-20260522-q8k0) both point at: ``folio_canonical_bytes`` is what gets
signed and verified, and ``identity.compute_folio_hash`` hashes the same bytes —
so the signed bytes and the hashed bytes can never drift. The field selection
and knurl serialization that used to live in ``identity.py`` now live here; that
move is byte-for-byte identical (the test suite is the guard), so existing hashes
are unchanged.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, List, Mapping, Optional, Union

from knurl import canon as _knurl_canon
from knurl.canon import CanonError

# Fractional-seconds component of an ISO timestamp (the digits after ":SS."). The
# lookbehind anchors it to the seconds field so it never matches a stray dot.
_FRACTION_RE = re.compile(r"(?<=:\d\d)\.(\d+)")

# The five fields that constitute folio identity (canon sorts keys, so order here
# is just for readers). Kept explicit so callers and reviewers see the basis.
CANONICAL_FIELDS = ("type", "title", "content", "created_at", "created_by")

CreatedAt = Union[str, datetime, None]


def normalize_created_at(value: CreatedAt) -> Optional[str]:
    """Normalize any ``created_at`` representation to one canonical string.

    Accepts a ``datetime`` (naive or aware) or a string (isoformat, ``Z``-suffixed,
    space-separated SQLite form, or with an explicit offset). Returns a
    timezone-aware UTC isoformat string. ``None`` passes through as ``None``.

    Rules:
    - A naive datetime/string is assumed to already be in UTC.
    - An aware value is converted to UTC.
    - Sub-second precision is preserved (it is the corpus-wide uniqueness basis);
      whole-second inputs that denote the same instant collapse to one string.

    Raises ``ValueError`` if a string cannot be parsed as a timestamp,
    ``TypeError`` if the value is neither datetime, str, nor None.
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = _parse_timestamp(value)
    else:
        raise TypeError(
            f"created_at must be a datetime, str, or None, got {type(value).__name__}"
        )

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _parse_timestamp(value: str) -> datetime:
    s = value.strip()
    # datetime.fromisoformat handles 'Z' natively only on 3.11+; normalize anyway
    # so behavior is identical across supported interpreters.
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    # Before 3.11, fromisoformat only accepts fractional seconds of exactly 3 or 6
    # digits (".1" raises); 3.11+ accepts any length. Normalize the fraction to
    # exactly 6 digits (right-pad, truncate beyond microsecond precision) so the
    # parse — and thus the canonical hash input — is identical across 3.10-3.12.
    s = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), s)
    try:
        return datetime.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"Unparseable created_at timestamp: {value!r}") from e


def _require_str_or_none(name: str, value: Any) -> Optional[str]:
    """Enforce the str-or-None canonical-scalar contract for one field.

    Every canonical field except ``created_at`` (normalized separately) must be a
    string or ``None``. A non-string scalar — int, bool, float — serializes as a
    JSON number/bool through knurl, so it would pass the wire integrity gate; but
    the store columns have TEXT affinity, so SQLite coerces ``99``->"99" and
    ``True``->1 on insert, and the served body then re-hashes to a DIFFERENT
    address than the one ingest bound (the body<->address binding breaks). Raising
    ``CanonError`` here — the SAME error a float already triggers — routes such a
    value through the wire gate's ``CanonError -> "invalid fields"`` rejection, so
    it is refused at ingest for every caller that shares this canon path.
    """
    if value is not None and not isinstance(value, str):
        raise CanonError(
            f"canonical field {name!r} must be a string or None, got "
            f"{type(value).__name__}"
        )
    return value


def folio_canonical_fields(fields: Mapping[str, Any]) -> dict:
    """The immutable five-field dict with ``created_at`` normalized.

    Only ``type, title, content, created_at, created_by`` participate; any other
    keys in ``fields`` are ignored. Every field except ``created_at`` must be a
    string or ``None`` (see :func:`_require_str_or_none` for why a non-str scalar
    breaks the body<->address binding).
    """
    return {
        "type": _require_str_or_none("type", fields.get("type")),
        "title": _require_str_or_none("title", fields.get("title")),
        "content": _require_str_or_none("content", fields.get("content")),
        "created_at": normalize_created_at(fields.get("created_at")),
        "created_by": _require_str_or_none("created_by", fields.get("created_by")),
    }


def folio_canonical_bytes(fields: Mapping[str, Any]) -> bytes:
    """The canonical bytes of a folio — the input to both hashing and signing."""
    return _knurl_canon.serialize(folio_canonical_fields(fields))


def thread_canonical_fields(
    from_id: Optional[str],
    to_id: Optional[str],
    type: Optional[str],
    weaver: Optional[str],
    created_at: CreatedAt,
    content: Optional[str],
) -> dict:
    """The six-field dict that constitutes a thread's identity (``created_at`` normalized).

    Every field except ``created_at`` must be a string or ``None`` — a non-str
    scalar breaks the body<->address binding under TEXT affinity, so it is
    rejected here as ``CanonError`` (see :func:`_require_str_or_none`)."""
    return {
        "from_id": _require_str_or_none("from_id", from_id),
        "to_id": _require_str_or_none("to_id", to_id),
        "type": _require_str_or_none("type", type),
        "weaver": _require_str_or_none("weaver", weaver),
        "created_at": normalize_created_at(created_at),
        "content": _require_str_or_none("content", content),
    }


def thread_canonical_bytes(
    from_id: Optional[str],
    to_id: Optional[str],
    type: Optional[str],
    weaver: Optional[str],
    created_at: CreatedAt,
    content: Optional[str],
) -> bytes:
    """The canonical bytes of a thread."""
    return _knurl_canon.serialize(
        thread_canonical_fields(from_id, to_id, type, weaver, created_at, content)
    )


# --- Merkle manifest (RFC 6962) ---------------------------------------------
#
# The unified signing model signs a small *descriptor* over the Merkle root of a
# publish's constituent content hashes (folios AND threads, kind-agnostic). The
# tree is RFC 6962 §2.1 exactly — the construction Rekor uses — so a constituent's
# membership is proven by recomputing the root from the full leaf list, with no
# inclusion-proof code in v0 (finding-20260613-q0ha / boundary 81fm). Two domain
# separations never collide: the 0x00/0x01 leaf/node prefixes live INSIDE the tree
# over raw digests; the profile||NUL prefix (profile.py) is on the signed
# descriptor preimage.

_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"

# A leaf datum is the RAW 32-byte SHA-256 digest decoded from a constituent's
# ``sha256::<64 hex>`` address (Q4) — never the ASCII address string.
_SHA256_ADDRESS_RE = re.compile(r"^sha256::([0-9a-f]{64})$")


class MerkleError(ValueError):
    """Base for Merkle-construction failures (a typed, total failure mode)."""


class MalformedLeafAddress(MerkleError):
    """A constituent address is not a well-formed ``sha256::<64 hex>`` (Q4/MK2)."""

    def __init__(self, addr: Any):
        super().__init__(f"malformed leaf address: {addr!r}")
        self.addr = addr


class EmptyManifest(MerkleError):
    """A manifest with zero constituents — rejected, never a sentinel root (MK7)."""


def address_to_leaf_datum(addr: str) -> bytes:
    """Decode a ``sha256::<64 hex>`` address to its raw 32-byte leaf datum (MK1).

    A malformed / non-sha256 / non-str address is rejected BEFORE tree
    construction (MK2): v0 has exactly one hash algo, so a non-sha256 address can
    never be a leaf.
    """
    if not isinstance(addr, str):
        raise MalformedLeafAddress(addr)
    m = _SHA256_ADDRESS_RE.match(addr)
    if m is None:
        raise MalformedLeafAddress(addr)
    return bytes.fromhex(m.group(1))


def merkle_leaf_hash(datum: bytes) -> bytes:
    """RFC 6962 leaf hash: ``SHA256(0x00 || datum)`` (MK3)."""
    return hashlib.sha256(_LEAF_PREFIX + datum).digest()


def merkle_node_hash(left: bytes, right: bytes) -> bytes:
    """RFC 6962 interior node hash: ``SHA256(0x01 || left || right)`` (MK3)."""
    return hashlib.sha256(_NODE_PREFIX + left + right).digest()


def _merkle_tree_hash(leaves: List[bytes]) -> bytes:
    """RFC 6962 §2.1 MTH over an already sorted+deduped, non-empty list of data."""
    n = len(leaves)
    if n == 1:
        return merkle_leaf_hash(leaves[0])
    # k = the largest power of two STRICTLY less than n (RFC 6962 odd-node split;
    # the lone promoted node rides up UNWRAPPED — no Bitcoin-style duplication).
    k = 1
    while k * 2 < n:
        k *= 2
    return merkle_node_hash(_merkle_tree_hash(leaves[:k]), _merkle_tree_hash(leaves[k:]))


def merkle_root(leaf_data: List[bytes]) -> bytes:
    """The RFC-6962 Merkle root over raw constituent digests.

    Leaves are SORTED ASCENDING by raw digest then DEDUPED before the tree is
    built (MK4), so the same constituent set yields a byte-identical root in any
    input order and with duplicates. A manifest of one is its leaf hash with no
    node applied (MK6); an empty manifest is rejected (MK7).
    """
    if not leaf_data:
        raise EmptyManifest("empty manifest has no Merkle root")
    unique = sorted(set(leaf_data))
    return _merkle_tree_hash(unique)


def merkle_root_for_addresses(addresses: List[str]) -> str:
    """The ``sha256::<hex>`` framed root over a list of constituent addresses.

    Each address is decoded to its raw leaf datum (rejecting a malformed one) and
    fed to :func:`merkle_root`. Returns the framed address form so it can ride in
    a descriptor / be compared to ``descriptor["root"]``.
    """
    data = [address_to_leaf_datum(a) for a in addresses]
    return "sha256::" + merkle_root(data).hex()


def dedup_leaf_count(addresses: List[str]) -> int:
    """The signed ``leaf_count``: the number of DISTINCT leaf data (MK4/VM9)."""
    return len({address_to_leaf_datum(a) for a in addresses})


def manifest_descriptor_fields(root: str, leaf_count: int) -> dict:
    """The signed descriptor body: just ``root`` + ``leaf_count`` (Q1/P4).

    ``root`` is the ``sha256::<hex>`` Merkle root; ``leaf_count`` is the count of
    DISTINCT leaves. The algorithm and leaf-construction binding live in the
    PROFILE STRING (skein.manifest.canon/v1), not in the descriptor body.
    """
    return {"root": root, "leaf_count": leaf_count}


def manifest_descriptor_canonical_bytes(root: str, leaf_count: int) -> bytes:
    """The canonical bytes of a manifest descriptor — what gets signed."""
    return _knurl_canon.serialize(manifest_descriptor_fields(root, leaf_count))


# --- redeem challenge (the invite-redeem ceremony preimage) ------------------
#
# The invite-redeem ceremony (INV-1) signs a small CHALLENGE descriptor that binds
# the Sigstore proof to one specific invite token + this station + this route +
# this ceremony. The signed bytes are these canonical bytes under the redeem
# profile (profile.CANON_PROFILE_REDEEM_V1 supplies the domain separation). The
# station RECONSTRUCTS these bytes from its OWN authoritative token_hash / origin /
# route plus the client-supplied nonce + issued_at, then hands them to verify_multi,
# which fails closed (canonical_mismatch -> SIGNATURE_MISMATCH) if the bundle was
# signed over any different challenge — so a harvested bundle, or a proof minted for
# another token/origin/route, can never bind here.

# The five fields that constitute a redeem challenge (canon sorts keys; order here
# is for readers). Must match profile._REDEEM_V1.fields exactly.
REDEEM_CHALLENGE_FIELDS = ("token_hash", "origin", "route", "nonce", "issued_at")


def redeem_challenge_fields(
    token_hash: str,
    origin: str,
    route: str,
    nonce: str,
    issued_at: str,
) -> dict:
    """The five-field redeem-challenge dict — what the ceremony signs (INV-1).

    Every field is a string. ``token_hash`` is the hex SHA-256 of the invite token
    (the plaintext token never appears here, only its hash); ``origin`` is the
    station's canonical origin (e.g. ``https://interskein.com``); ``route`` is the
    redeem route name; ``nonce`` is a per-ceremony CSPRNG value; ``issued_at`` is an
    ISO-8601 stamp. Non-str inputs are rejected (``CanonError``) on the same
    body<->preimage-binding rationale as the folio/thread fields.
    """
    return {
        "token_hash": _require_str_or_none("token_hash", token_hash),
        "origin": _require_str_or_none("origin", origin),
        "route": _require_str_or_none("route", route),
        "nonce": _require_str_or_none("nonce", nonce),
        "issued_at": _require_str_or_none("issued_at", issued_at),
    }


def redeem_challenge_canonical_bytes(
    token_hash: str,
    origin: str,
    route: str,
    nonce: str,
    issued_at: str,
) -> bytes:
    """The canonical bytes of a redeem challenge — the preimage signed and verified."""
    return _knurl_canon.serialize(
        redeem_challenge_fields(token_hash, origin, route, nonce, issued_at)
    )


def manifest_membership(leaf_list: List[str], signed_root: str, constituent_hash: str) -> bool:
    """Whether ``constituent_hash`` is a member under ``signed_root`` (v0 recompute).

    Membership is established ONLY by recomputing the root from the FULL leaf list
    (re-run :func:`merkle_root` over the sorted-deduped leaves) and checking both
    that it equals the signed root AND that the constituent's leaf datum is in the
    decoded leaf set. There is NO inclusion-path / Merkle-audit-proof verifier in
    v0 (VM10) — the inclusion-proof seam is a documented later feature. Total over
    a malformed leaf_list / constituent address (returns False, never raises)."""
    try:
        recomputed = merkle_root_for_addresses(leaf_list)
        datum = address_to_leaf_datum(constituent_hash)
        members = {address_to_leaf_datum(a) for a in leaf_list}
    except MerkleError:
        return False
    return recomputed == signed_root and datum in members
