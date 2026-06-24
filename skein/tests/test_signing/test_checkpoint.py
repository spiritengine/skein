"""Rekor checkpoint signed-note binding contract."""

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


def _signed_bundle(crypto_factory, google_provider, canonical_bytes, monkeypatch):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes, google_provider)
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )


def _replace_only_bundle_blob(signature_bundle, blob):
    return signing.SignatureBundle(
        identity_scheme=signature_bundle.identity_scheme,
        bundles=[blob],
        canonical_bytes=signature_bundle.canonical_bytes,
        canon_version=signature_bundle.canon_version,
        trust_root_pin=signature_bundle.trust_root_pin,
    )


def test_verify_inclusion_failed_on_tampered_checkpoint_signature(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    # Enforces: finding-20260512-eaft actionable #2. The checkpoint is a C2SP
    # signed note; verify() must reject a present checkpoint whose signature
    # does not verify under the Rekor key selected by log_id.
    sb = _signed_bundle(
        crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
    )
    tampered = crypto_factory.tamper_checkpoint_signature(sb.bundles[0])
    vr = signing.verify(
        canonical_bytes_simple,
        _replace_only_bundle_blob(sb, tampered),
    )
    assert vr.status == signing.VerifyStatus.INCLUSION_FAILED


def test_verify_inclusion_failed_when_checkpoint_binds_different_root_hash(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    # Enforces: finding-20260512-eaft actionable #2. A valid checkpoint
    # signature is insufficient unless its root_hash equals proof.root_hash.
    sb = _signed_bundle(
        crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
    )
    mismatched = crypto_factory.checkpoint_with_different_root(sb.bundles[0])
    vr = signing.verify(
        canonical_bytes_simple,
        _replace_only_bundle_blob(sb, mismatched),
    )
    assert vr.status == signing.VerifyStatus.INCLUSION_FAILED


def test_verify_inclusion_failed_when_checkpoint_binds_different_tree_size(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    # Enforces: finding-20260512-eaft actionable #2. The signed checkpoint must
    # bind the same tree_size as the inclusion proof, not just a valid root.
    sb = _signed_bundle(
        crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
    )
    mismatched = crypto_factory.checkpoint_with_different_tree_size(sb.bundles[0])
    vr = signing.verify(
        canonical_bytes_simple,
        _replace_only_bundle_blob(sb, mismatched),
    )
    assert vr.status == signing.VerifyStatus.INCLUSION_FAILED


def test_verify_inclusion_failed_when_checkpoint_signed_by_rotated_out_rekor_key(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    # Enforces: finding-20260512-eaft actionable #2 plus finding-20260513-w5hq.
    # log_id selects the Rekor key; a checkpoint signed by a rotated-out key for
    # the wrong era must not verify merely because the key is historical.
    sb = _signed_bundle(
        crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
    )
    rotated = crypto_factory.checkpoint_signed_by_old_rekor_key(sb.bundles[0])
    vr = signing.verify(
        canonical_bytes_simple,
        _replace_only_bundle_blob(sb, rotated),
    )
    assert vr.status in {
        signing.VerifyStatus.INCLUSION_FAILED,
        signing.VerifyStatus.TRUST_ROOT_STALE,
    }


@pytest.mark.parametrize("kind", ["extra-newlines", "missing-origin"])
def test_verify_bundle_malformed_on_malformed_checkpoint_signed_note(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch, kind
):
    # Enforces: finding-20260512-eaft actionable #2. Signed-note syntax errors
    # are malformed bundle material, distinct from a valid note with a bad
    # cryptographic binding.
    sb = _signed_bundle(
        crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
    )
    malformed = crypto_factory.malformed_checkpoint(sb.bundles[0], kind=kind)
    vr = signing.verify(
        canonical_bytes_simple,
        _replace_only_bundle_blob(sb, malformed),
    )
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED
