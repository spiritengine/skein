"""Fulcio SCT verification contract.

Fulcio leaf certificates must carry a Signed Certificate Timestamp extension
from a trusted Sigstore CT log. These tests pin the SCT as cryptographic
evidence, not decorative certificate metadata.
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


def _signed_blob(crypto_factory, google_provider, canonical_bytes, monkeypatch):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes, google_provider)
    return result.bundle_json


def _bundle(canonical_bytes, blob):
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )


def test_verify_accepts_cert_with_valid_sct_from_trusted_ct_log(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    # Enforces: finding-20260512-eaft blocker #4. The happy path includes a
    # Fulcio SCT extension from a CT log in trusted_root.json's ctlogs set.
    blob = _signed_blob(
        crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
    )
    vr = signing.verify(canonical_bytes_simple, _bundle(canonical_bytes_simple, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED


def test_verify_cert_invalid_when_sct_extension_missing(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    # Enforces: finding-20260512-eaft blocker #4. Missing SCT extension
    # OID 1.3.6.1.4.1.11129.2.4.2 fails closed as CERT_INVALID.
    blob = _signed_blob(
        crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
    )
    stripped = crypto_factory.strip_sct(blob)
    vr = signing.verify(
        canonical_bytes_simple, _bundle(canonical_bytes_simple, stripped)
    )
    assert vr.status == signing.VerifyStatus.CERT_INVALID


def test_verify_cert_invalid_for_sct_from_untrusted_ct_log(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    # Enforces: finding-20260512-eaft blocker #4. An SCT with the right shape
    # but signed by a CT log outside the Sigstore trust root is not acceptable.
    blob = _signed_blob(
        crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
    )
    untrusted = crypto_factory.untrusted_ct_log_sct(blob)
    vr = signing.verify(
        canonical_bytes_simple, _bundle(canonical_bytes_simple, untrusted)
    )
    assert vr.status == signing.VerifyStatus.CERT_INVALID


def test_verify_cert_invalid_for_forged_sct_signature(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    # Enforces: finding-20260512-eaft blocker #4. The verifier must check the
    # SCT signature, not merely the presence of a parseable SCT extension.
    blob = _signed_blob(
        crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
    )
    forged = crypto_factory.tamper_sct(blob)
    vr = signing.verify(canonical_bytes_simple, _bundle(canonical_bytes_simple, forged))
    assert vr.status == signing.VerifyStatus.CERT_INVALID


def test_verify_cert_invalid_for_sct_bound_to_different_certificate(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    # Enforces: finding-20260512-eaft blocker #4. The SCT must be bound to this
    # exact leaf certificate; transplanting an SCT from another cert fails.
    blob = _signed_blob(
        crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
    )
    crossed = crypto_factory.cross_sct(blob)
    vr = signing.verify(
        canonical_bytes_simple, _bundle(canonical_bytes_simple, crossed)
    )
    assert vr.status == signing.VerifyStatus.CERT_INVALID
