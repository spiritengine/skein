"""identity_scheme discriminator contract.

The Identity Scheme Registry (Identity rev 5) is the load-bearing forward-compat
mechanism. v0 ships "sigstore-public-v1"; reserved values (sigstore-public-v2,
sigstore-private-v1, did-key-v1) MUST be fail-closed until their profiles
are implemented.

Phase 1 q776/qejb/sbal convergent: the polymorphic / unversioned discriminator
was a load-bearing wrong call in rev 4; rev 5 versioned it. These tests pin
the versioning contract.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


# ---------------------------------------------------------------------------
# Known scheme dispatches to a working verifier
# ---------------------------------------------------------------------------


# Enforces: "sigstore-public-v1" is the v0 default and dispatches to the
# Rekor v2 + RFC3161 + Fulcio public profile.
def test_sigstore_public_v1_is_active_scheme(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    assert (
        signing.verify(canonical_bytes_simple, sb).status
        == signing.VerifyStatus.VERIFIED
    )


# ---------------------------------------------------------------------------
# Unknown / reserved schemes are fail-closed
# ---------------------------------------------------------------------------


# Enforces: ALL reserved registry entries currently fail-closed. A future
# implementation may flip any of these to active; this test must be updated at
# THAT spec amendment, not silently.
@pytest.mark.parametrize(
    "scheme",
    [
        "sigstore-public-v2",  # reserved: post-PQC migration
        "sigstore-private-v1",  # reserved: private Sigstore
        "did-key-v1",  # reserved: DID-based long-term keypair
    ],
)
def test_reserved_schemes_fail_closed(canonical_bytes_simple, make_bundle, scheme):
    sb = make_bundle(
        canonical_bytes=canonical_bytes_simple,
        bundles=["<irrelevant>"],
        identity_scheme=scheme,
    )
    result = signing.verify(canonical_bytes_simple, sb)
    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: typo or garbage scheme string is BUNDLE_MALFORMED.
@pytest.mark.parametrize(
    "scheme",
    [
        "",
        "sigstore-public",  # legacy unversioned — rev 5 explicitly forbids
        "Sigstore-Public-V1",  # case-sensitive
        "sigstore_public_v1",  # wrong separator
        "sigstore-public-v1 ",  # trailing whitespace
        "https://example.com/scheme",
        "0",
    ],
)
def test_unknown_or_typo_schemes_fail_closed(
    canonical_bytes_simple, make_bundle, scheme
):
    sb = make_bundle(
        canonical_bytes=canonical_bytes_simple,
        bundles=["<irrelevant>"],
        identity_scheme=scheme,
    )
    result = signing.verify(canonical_bytes_simple, sb)
    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: rev 4 spelling "sigstore-public" is rejected. Rev 5 explicitly
# invalidates this as too coarse per IETF fully-specified-algorithms lesson.
def test_unversioned_sigstore_public_fail_closed(canonical_bytes_simple, make_bundle):
    sb = make_bundle(
        canonical_bytes=canonical_bytes_simple,
        bundles=["<irrelevant>"],
        identity_scheme="sigstore-public",
    )
    result = signing.verify(canonical_bytes_simple, sb)
    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# ---------------------------------------------------------------------------
# Schema integrity: identity_scheme is per-bundle (not per-signer)
# ---------------------------------------------------------------------------


# Enforces: identity_scheme is a top-level field of SignatureBundle, not a
# per-bundle attribute inside bundles[]. All signers operate under the same
# scheme. Rev 5: "Each identity_scheme value pins a complete profile."
def test_identity_scheme_is_global_per_signature_bundle(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=f"signer{i}@example.com",
            issuer="https://accounts.google.com",
        )
        for i in range(2)
    ]
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    # No per-signer scheme; the field lives on SignatureBundle.
    assert sb.identity_scheme == "sigstore-public-v1"


# ---------------------------------------------------------------------------
# verify() return on unknown scheme has NO issuer/subject
# ---------------------------------------------------------------------------


# Enforces: when identity_scheme is unknown, the wrapper short-circuits BEFORE
# attempting parsing or identity extraction. issuer=None and subject=None — we
# never expose identity from a bundle we cannot verify.
def test_unknown_scheme_returns_no_identity(canonical_bytes_simple, make_bundle):
    sb = make_bundle(
        canonical_bytes=canonical_bytes_simple,
        bundles=["<irrelevant>"],
        identity_scheme="sigstore-future-vNext",
    )
    result = signing.verify(canonical_bytes_simple, sb)
    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED
    assert result.issuer is None
    assert result.subject is None


# Enforces: missing identity_scheme field is BUNDLE_MALFORMED at Pydantic level
# or fails fast at verify() — either is acceptable. Tested at construction.
def test_missing_identity_scheme_rejected_at_construction(canonical_bytes_simple):
    with pytest.raises(Exception):
        signing.SignatureBundle(
            bundles=["<irrelevant>"],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
