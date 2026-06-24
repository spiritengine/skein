"""The resolve verb: one address string -> one local content hash.

This is the front half of the content-addressed resolver (brief-20260603-tgg8).
It does pure address math over the rev3 grammar (``skein.address``) plus the one
store touch needed to lengthen a short hash, and nothing else: it never reads a
folio body, never builds an envelope, never verifies a signature. Existence is
checked downstream (``store.get_folio`` returning ``None`` is ``not_found``), so
this layer stays a thin, total mapping ``address -> sha256::<full>`` or a typed
:class:`ResolveError`.

What it enforces, all from the spec:

- **Short hashes are local-only** (§2). A ``web::`` address carrying a short
  digest is rejected (``short_hash_unsupported_remote``) — a remote instance
  cannot disambiguate an 8-hex prefix, and the grammar would otherwise let one
  through because alias/web forms permit short digests by construction.
- **Fragment enforcement** (§2). If the address carries a ``#sha256::<digest>``
  verifier fragment, the resolved full hash MUST equal it or resolution fails
  with ``hash_mismatch`` — the fragment is not decorative.
- **Legacy-id fallback.** The corpus was migrated from human ids; threads and
  pasted references still carry ids like ``finding-20260603-zr29``. Those are not
  valid content-hash addresses, so when address parsing fails we try the store's
  alias table before giving up. This keeps the freshly-migrated corpus navigable
  without weakening the wire contract (a real malformed address with no alias
  still fails ``invalid_address``).
"""

from __future__ import annotations

from typing import List, Optional

from skein import address

# Error codes are the spec's (§6); kept as a frozenset so a typo in a raise site
# is catchable in tests rather than silently minting a new code.
ERROR_CODES = frozenset(
    {
        "not_found",
        "hash_mismatch",
        "ambiguous_short_hash",
        "short_hash_unsupported_remote",
        "invalid_address",
    }
)


class ResolveError(Exception):
    """A resolution failure carrying a spec error code and the offending address.

    ``origin`` is set when the address named a remote ``web::`` authority, so the
    error envelope can distinguish "the origin said no" from "never existed here"
    (§6's ``links.origin``).
    """

    def __init__(self, code: str, address_str: str, *, origin: Optional[str] = None):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown resolve error code: {code!r}")
        super().__init__(f"{code}: {address_str}")
        self.code = code
        self.address = address_str
        self.origin = origin


class _StoreStationIndex:
    """Adapt the native store to the address module's ``StationIndex`` protocol.

    ``address.resolve`` asks for the FULL lowercase-hex digests sharing a prefix;
    the store indexes the prefix against the full ``sha256::<hex>`` address, so we
    prepend the algorithm to query and strip it back off to return bare digests.
    """

    def __init__(self, store):
        self._store = store

    def folios_with_prefix(self, algo: str, prefix: str) -> List[str]:
        matches = self._store.find_by_prefix(f"{algo}::{prefix}", limit=10)
        return [m.split("::", 1)[1] for m in matches]


def resolve_to_hash(address_str: str, store, *, local_authority: Optional[str] = None) -> str:
    """Resolve ``address_str`` to a local ``sha256::<full>`` content hash.

    ``local_authority`` (when set) is this instance's own ``web::`` authority; a
    ``web::`` address naming a *different* authority is remote and cannot be
    resolved here in Phase 1 (federation is later), so it raises ``not_found``
    carrying that authority as the origin.

    Raises :class:`ResolveError` with a spec error code on any failure. Does NOT
    check that the folio exists — a well-formed full hash resolves to itself; the
    caller's ``store.get_folio`` is the existence gate.
    """
    try:
        parsed = address.parse(address_str)
    except address.AddressError:
        # Not a content-hash address. The freshly-migrated corpus still carries
        # human ids in thread endpoints and pasted refs; resolve those through
        # the alias table before declaring the address malformed.
        aliased = store.resolve_alias(address_str)
        if aliased:
            return aliased
        raise ResolveError("invalid_address", address_str)

    folio = parsed.folio

    if parsed.type == "web":
        if folio.is_short:
            raise ResolveError("short_hash_unsupported_remote", address_str)
        if local_authority is not None and parsed.authority != local_authority:
            # A foreign origin. Phase 1 serves only local content; surface the
            # origin so a federating caller knows where the bytes actually live.
            origin = address.construct(type="web", folio=folio, authority=parsed.authority)
            raise ResolveError("not_found", address_str, origin=origin)

    try:
        full_digest = address.resolve(parsed, _StoreStationIndex(store))
    except address.AmbiguousShortHash:
        raise ResolveError("ambiguous_short_hash", address_str)
    except address.ShortHashNotFound:
        raise ResolveError("not_found", address_str)

    content_hash = f"{folio.algo}::{full_digest}"

    if parsed.fragment is not None:
        fragment_hash = f"{parsed.fragment.algo}::{parsed.fragment.digest}"
        if fragment_hash != content_hash:
            raise ResolveError("hash_mismatch", address_str)

    return content_hash
