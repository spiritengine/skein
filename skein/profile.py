"""The signed-preimage profile registry (brief-20260603-ujwx §3-4).

A ``profile`` string — ``skein.folio.canon/v1`` — names, in one token, the trust
domain (``skein``), the object kind (``folio``), and the canonicalization version
(``v1``). It is the axis envelope rev 1 was missing. Two jobs:

1. **Domain separation.** The thing signed is NOT the raw canonical bytes but
   ``profile-string || canonical_bytes``. Sigstore signs opaque bytes, and a
   federated peer can present any bundle, so without a SKEIN-specific prefix a
   signature made over bytes for some other purpose that happen to equal a SKEIN
   canonical preimage could be replayed as SKEIN authorship. The profile prefix
   binds every SKEIN signature to the SKEIN canon/kind. (knurl's structured JSON
   already has unambiguous field boundaries, so this is domain separation, not
   classical PAE-for-concatenation; the NUL separator below just makes the two
   regions explicit.)

2. **An explicit verifier contract.** Each profile maps to a fixed tuple — object
   kind, canonical field set, hash algorithm, and the preimage-construction rule.
   A verifier reads the profile to select that tuple. **An unknown profile is a
   hard verification failure — never a fallback or downgrade** (§3). ``v1`` is the
   only profile; there is no grandfathered raw-v0 path.

This is the field that ``SignatureBundle.canon_version`` (``skein/signing.py``)
becomes: it defaulted to a dead ``"knurl-1.0"`` that ``verify_multi`` never read;
the SKEIN signer now sets it to the profile string and the SKEIN verifier reads
and enforces it (§11). The generic signing primitive stays generic (it signs and
verifies opaque bytes); the profile semantics live here, at the folio boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from .canon import CANONICAL_FIELDS

# The v1 folio profile string. Bound into every signature; stored as the bundle's
# ``canon_version``; surfaced as the envelope's ``proof.profile``.
CANON_PROFILE_V1 = "skein.folio.canon/v1"

# The v1 manifest profile string. The unified signing model signs a Merkle-root
# DESCRIPTOR under this profile; the descriptor body is just ``root`` + ``leaf_count``
# (Q1), and this string supplies the domain separation + algorithm / leaf-construction
# binding (NOT in-body fields), exactly as a folio binds its recipe via its profile.
CANON_PROFILE_MANIFEST_V1 = "skein.manifest.canon/v1"

# The v1 redeem profile string. The invite-redeem ceremony signs a REDEEM CHALLENGE
# under this profile — a descriptor whose body binds the proof to the invite token
# (its hash), the station origin, the route, and a per-ceremony nonce/issued-at
# (INV-1). It is a DISTINCT kind from folio/manifest, so a harvested folio/manifest
# bundle presented on the redeem path resolves to kind 'redeem' != its own and is
# rejected 'wrong kind' before any crypto — and, conversely, a redeem bundle can
# never cross into the publish path. The token-hash inside the signed challenge is
# what makes a harvested redeem bundle for ANOTHER token fail closed
# (canonical_mismatch -> SIGNATURE_MISMATCH).
CANON_PROFILE_REDEEM_V1 = "skein.redeem.canon/v1"

# The byte that separates the profile prefix from the canonical bytes. NUL cannot
# occur in either region — the profile is a fixed ASCII token, and knurl canonical
# JSON is UTF-8 with all control characters escaped — so the split is unambiguous.
_SEPARATOR = b"\x00"


class UnknownProfile(Exception):
    """A profile string not in the registry. Verification fails hard on this."""

    def __init__(self, profile: str):
        super().__init__(f"unknown signed-preimage profile: {profile!r}")
        self.profile = profile


@dataclass(frozen=True)
class CanonProfile:
    """The fixed tuple a profile string resolves to."""

    profile: str
    kind: str
    fields: Tuple[str, ...]
    hash_algo: str


_FOLIO_V1 = CanonProfile(
    profile=CANON_PROFILE_V1,
    kind="folio",
    fields=CANONICAL_FIELDS,
    hash_algo="sha256",
)

# The manifest descriptor profile, registered ALONGSIDE the folio profile from day
# one (do NOT repeat the knurl-1.0 unknown-profile hard-fail, finding-20260608-8qsj).
# The signed descriptor's field tuple is ('root', 'leaf_count'); kind 'manifest' is
# pinned both directions at the verify seams (P6/P7).
_MANIFEST_V1 = CanonProfile(
    profile=CANON_PROFILE_MANIFEST_V1,
    kind="manifest",
    fields=("root", "leaf_count"),
    hash_algo="sha256",
)

# The redeem challenge profile, registered ALONGSIDE the others from day one (the
# same anti-pattern guard as the manifest profile: never an unknown-profile hard
# fail for our own kind). The signed challenge's field tuple is the redeem-binding
# basis; kind 'redeem' is pinned at the verify seam (verify_wire_redeem), so a
# folio/manifest bundle is 'wrong kind' there and a redeem bundle is 'wrong kind'
# on the folio/manifest seams.
_REDEEM_V1 = CanonProfile(
    profile=CANON_PROFILE_REDEEM_V1,
    kind="redeem",
    fields=("token_hash", "origin", "route", "nonce", "issued_at"),
    hash_algo="sha256",
)

_REGISTRY: Dict[str, CanonProfile] = {
    _FOLIO_V1.profile: _FOLIO_V1,
    _MANIFEST_V1.profile: _MANIFEST_V1,
    _REDEEM_V1.profile: _REDEEM_V1,
}


def get_profile(profile: str) -> CanonProfile:
    """Resolve a profile string to its tuple, or fail hard (``UnknownProfile``).

    There is no default and no fallback: a profile the registry does not know is a
    hard verification failure, never a downgrade to some assumed canon.
    """
    try:
        return _REGISTRY[profile]
    except KeyError:
        raise UnknownProfile(profile)


def profiled_preimage(profile: str, canonical_bytes: bytes) -> bytes:
    """The domain-separated bytes that are actually signed and verified.

    ``profile-string || NUL || canonical_bytes``. The profile is validated
    against the registry first, so a preimage can never be built under an unknown
    profile (sign and verify both go through here, so they cannot disagree).
    """
    get_profile(profile)  # reject an unknown profile before constructing anything
    return profile.encode("utf-8") + _SEPARATOR + canonical_bytes
