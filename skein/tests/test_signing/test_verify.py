"""verify() contract — single-bundle path.

happy path + one test per VerifyStatus case + identity extraction +
canonical_bytes binding + offline/online + verify() refuses multi-signer
bundles per clarification 2.

verify() is the read path. Every folio render hits this. The 8 statuses are
the federation/UX vocabulary; collapsing or mis-mapping any of them is the
load-bearing-wrong failure mode Phase 1 surfaced (rev 5 §verify return shape).

Per clarification 2: verify() requires len(signature_bundle.bundles) == 1.
Multi-signer bundles raise MultiSignerBundle.
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


CERT_NOT_BEFORE = 1_800_000_000_000_000
CERT_NOT_AFTER = CERT_NOT_BEFORE + 10 * 60 * 1_000_000
VERIFY_AFTER_CERT_EXPIRY = CERT_NOT_AFTER + 30 * 24 * 60 * 60 * 1_000_000


def _signature_bundle(
    canonical_bytes: bytes,
    blob: str,
    *,
    trust_root_pin: str | None = None,
) -> signing.SignatureBundle:
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
        trust_root_pin=trust_root_pin,
    )


# ---------------------------------------------------------------------------
# Happy path — VERIFIED
# ---------------------------------------------------------------------------


# Enforces: verify() on a fresh sign() bundle returns status=VERIFIED with
# issuer+subject populated. This is the read-path golden case.
def test_verify_returns_verified_for_fresh_bundle(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        identity="alice@example.com",
    )
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.issuer == "https://accounts.google.com"
    assert vr.subject == "alice@example.com"


# Enforces: VERIFIED result carries non-None Evidence with validity_window.
# Phase 1 sbal §"edge cases to test #5": signing_timestamp must come from
# Rekor SET / RFC3161, not client clock.
def test_verify_verified_carries_evidence(
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
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.evidence is not None
    assert vr.evidence.validity_window is not None
    start, end = vr.evidence.validity_window
    # Cert validity is ~10 minutes per Fulcio.
    assert 0 < end - start <= 11 * 60 * 1_000_000  # microseconds


# Enforces: VERIFIED result's Evidence.rekor_inclusion is a RekorInclusionProof
# with the 7 fields per clarification 4 + finding-20260513-w5hq — independent
# verifier can re-run Merkle inclusion and select the correct Rekor key without
# calling Rekor.
def test_verify_verified_evidence_carries_rekor_inclusion_proof(
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
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.evidence.rekor_inclusion is not None
    proof = vr.evidence.rekor_inclusion
    assert isinstance(proof, signing.RekorInclusionProof)
    assert proof.log_index >= 0
    assert proof.tree_size > 0
    assert proof.root_hash
    assert proof.checkpoint
    assert proof.integrated_time > 0
    assert proof.log_id


# ---------------------------------------------------------------------------
# verify() refuses multi-signer bundles (clarification 2)
# ---------------------------------------------------------------------------


# Enforces: per clarification 2, verify() called on a SignatureBundle with
# len(bundles) > 1 raises MultiSignerBundle with the exact message.
def test_verify_raises_multi_signer_bundle_on_two_signers(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_a = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob_b = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="bob@example.com",
        issuer="https://github.com/login/oauth",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_a, blob_b],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    with pytest.raises(signing.MultiSignerBundle) as exc:
        signing.verify(canonical_bytes_simple, sb)
    msg = str(exc.value)
    assert "verify() requires exactly one signer" in msg
    assert "got 2" in msg
    assert "verify_multi()" in msg


# Enforces: MultiSignerBundle is raised regardless of N when N > 1.
@pytest.mark.parametrize("n", [2, 3, 5])
def test_verify_raises_multi_signer_bundle_for_any_n_greater_than_one(
    crypto_factory,
    canonical_bytes_simple,
    monkeypatch,
    n,
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=f"signer{i}@example.com",
            issuer="https://accounts.google.com",
        )
        for i in range(n)
    ]
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    with pytest.raises(signing.MultiSignerBundle) as exc:
        signing.verify(canonical_bytes_simple, sb)
    assert f"got {n}" in str(exc.value)


# Enforces: verify() does no internal dispatch to multi-signer logic
# (clarification 2). Caller must use verify_multi() explicitly.
def test_verify_does_not_silently_dispatch_to_multi(
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
    # MUST raise — not return a VerifyResult, not return MultiVerifyResult.
    with pytest.raises(signing.MultiSignerBundle):
        signing.verify(canonical_bytes_simple, sb)


# ---------------------------------------------------------------------------
# SIGNATURE_MISMATCH — perturbed canonical_bytes
# ---------------------------------------------------------------------------


# Enforces: if the caller verifies against bytes that differ from what was
# signed, status is SIGNATURE_MISMATCH. The load-bearing cryptographic binding:
# "the signature covers exactly these bytes."
def test_verify_signature_mismatch_on_perturbed_bytes(
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
    perturbed = bytearray(canonical_bytes_simple)
    perturbed[0] ^= 0x01
    vr = signing.verify(bytes(perturbed), sb)
    assert vr.status == signing.VerifyStatus.SIGNATURE_MISMATCH


# Enforces: SignatureBundle.canonical_bytes is the literal signed bytes; if the
# caller passes different bytes, verify uses the caller's bytes for the
# signature check. A wrapper that ignored its caller's argument could be
# tricked by a MITM matching the stored field but serving different folio JSON.
def test_verify_uses_caller_canonical_bytes_not_stored(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    different = canonical_bytes_simple + b"_modified"
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=different,  # stored field disagrees with sig
        canon_version="knurl-1.0",
    )
    vr = signing.verify(different, sb)
    assert vr.status == signing.VerifyStatus.SIGNATURE_MISMATCH


# Enforces: canonical_bytes are byte-exact; Unicode normalization drift still
# breaks signature verification.
def test_canonical_bytes_nfc_nfd_drift_returns_signature_mismatch(
    crypto_factory, google_provider, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical_nfc = "josé: résumé".encode("utf-8")
    canonical_nfd = "jose\u0301: re\u0301sume\u0301".encode("utf-8")
    result = signing.sign(canonical_nfc, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_nfc,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_nfd, sb)
    assert vr.status == signing.VerifyStatus.SIGNATURE_MISMATCH


# ---------------------------------------------------------------------------
# CERT_INVALID — Fulcio cert chain breaks
# ---------------------------------------------------------------------------


# Enforces: tampered cert (one bit flipped in cert bytes) returns CERT_INVALID,
# not SIGNATURE_MISMATCH. The two failure modes are distinguishable per spec.
def test_verify_cert_invalid_on_tampered_cert(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    blob_tampered = crypto_factory.tamper_cert(result.bundle_json)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_tampered],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


def test_verify_accepts_chain_length_1(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob_with_chain_length(
        chain_length=1,
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(
        canonical_bytes_simple, _signature_bundle(canonical_bytes_simple, blob)
    )
    assert vr.status == signing.VerifyStatus.VERIFIED


def test_verify_accepts_chain_length_2_with_intermediate(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob_with_chain_length(
        chain_length=2,
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(
        canonical_bytes_simple, _signature_bundle(canonical_bytes_simple, blob)
    )
    assert vr.status == signing.VerifyStatus.VERIFIED


def test_verify_accepts_chain_length_3_with_two_intermediates(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob_with_chain_length(
        chain_length=3,
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(
        canonical_bytes_simple, _signature_bundle(canonical_bytes_simple, blob)
    )
    assert vr.status == signing.VerifyStatus.VERIFIED


def test_verify_rejects_broken_chain_unchained_to_root(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob_with_broken_chain(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(
        canonical_bytes_simple, _signature_bundle(canonical_bytes_simple, blob)
    )
    assert vr.status == signing.VerifyStatus.CERT_INVALID


def test_verify_rejects_chain_length_0_self_signed_leaf(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob_with_chain_length(
        chain_length=0,
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(
        canonical_bytes_simple, _signature_bundle(canonical_bytes_simple, blob)
    )
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: cert chain that does not lead to a known Fulcio CA returns CERT_INVALID.
def test_verify_cert_invalid_on_unknown_ca(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        ca="unknown-ca",
    )
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: future-dated cert (cert notBefore is AFTER the Rekor integrated
# time) returns CERT_INVALID. Cross-check: the wrapper must validate that the
# cert was valid when the entry was integrated.
def test_verify_cert_invalid_on_future_dated_cert(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    blob_future = crypto_factory.future_dated_cert(result.bundle_json)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_future],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: finding-20260512-eaft actionable #1. If Rekor integrated the entry
# after the Fulcio leaf cert's notAfter, the signature was made outside the
# certificate validity window and must fail as CERT_INVALID.
def test_verify_cert_invalid_when_integrated_after_not_after(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_cert_validity(
        blob,
        not_before=CERT_NOT_BEFORE,
        not_after=CERT_NOT_AFTER,
    )
    blob = crypto_factory.set_rekor_integrated_time(blob, CERT_NOT_AFTER + 1)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: finding-20260512-eaft actionable #1 and sbal long-lived Sigstore
# verification. Current verify time may be after notAfter; what matters is that
# the cert was valid when Rekor integrated the entry.
def test_verify_succeeds_when_verify_time_after_not_after_but_integrated_inside_window(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_cert_validity(
        blob,
        not_before=CERT_NOT_BEFORE,
        not_after=CERT_NOT_AFTER,
    )
    blob = crypto_factory.set_rekor_integrated_time(blob, CERT_NOT_BEFORE + 1)
    crypto_factory.set_verify_time(VERIFY_AFTER_CERT_EXPIRY)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Enforces: finding-20260512-eaft actionable #1. X.509 notBefore is inclusive:
# integrated_time == notBefore is valid.
def test_verify_cert_validity_window_boundary_inclusive_at_not_before(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_cert_validity(
        blob,
        not_before=CERT_NOT_BEFORE,
        not_after=CERT_NOT_AFTER,
    )
    blob = crypto_factory.set_rekor_integrated_time(blob, CERT_NOT_BEFORE)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Enforces: finding-20260512-eaft actionable #1. X.509 notAfter is inclusive:
# integrated_time == notAfter is valid.
def test_verify_cert_validity_window_boundary_inclusive_at_not_after(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_cert_validity(
        blob,
        not_before=CERT_NOT_BEFORE,
        not_after=CERT_NOT_AFTER,
    )
    blob = crypto_factory.set_rekor_integrated_time(blob, CERT_NOT_AFTER)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Enforces: finding-20260512-eaft actionable #1. One microsecond before
# notBefore is outside the cert validity window and must fail.
def test_verify_cert_validity_one_microsecond_before_not_before(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_cert_validity(
        blob,
        not_before=CERT_NOT_BEFORE,
        not_after=CERT_NOT_AFTER,
    )
    blob = crypto_factory.set_rekor_integrated_time(blob, CERT_NOT_BEFORE - 1)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: finding-20260512-eaft actionable #1. One microsecond after
# notAfter is outside the cert validity window and must fail.
def test_verify_cert_validity_one_microsecond_after_not_after(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_cert_validity(
        blob,
        not_before=CERT_NOT_BEFORE,
        not_after=CERT_NOT_AFTER,
    )
    blob = crypto_factory.set_rekor_integrated_time(blob, CERT_NOT_AFTER + 1)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# ---------------------------------------------------------------------------
# INCLUSION_FAILED — Rekor proof breaks
# ---------------------------------------------------------------------------


# Enforces: stripped Rekor inclusion proof returns INCLUSION_FAILED, not
# CERT_INVALID.
def test_verify_inclusion_failed_on_stripped_proof(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    blob_stripped = crypto_factory.strip_rekor_proof(result.bundle_json)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_stripped],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.INCLUSION_FAILED


# Enforces: Rekor proof with hashes[] tampered returns INCLUSION_FAILED.
def test_verify_inclusion_failed_on_tampered_merkle_hashes(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    blob_munged = crypto_factory.tamper_merkle_hashes(result.bundle_json)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_munged],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.INCLUSION_FAILED


# Enforces: stripped Rekor checkpoint (but Merkle hashes retained) returns
# INCLUSION_FAILED. The checkpoint is the trust anchor for the proof.
def test_verify_inclusion_failed_on_stripped_checkpoint(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    blob = crypto_factory.strip_checkpoint(result.bundle_json)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.INCLUSION_FAILED


# ---------------------------------------------------------------------------
# IDENTITY_MISMATCH — issuer/subject disambiguation
# ---------------------------------------------------------------------------


# Enforces: verify() returns issuer + subject; the wrapper extracts both from
# the cert (subject from SAN; issuer from OID 1.3.6.1.4.1.57264.1.8 V2,
# falling back to OID 1.3.6.1.4.1.57264.1.1 legacy). Phase 1 sbal C2: returning
# subject alone is impersonatable.
def test_verify_extracts_issuer_from_oid_v2_when_present(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        identity="alice@example.com",
        issuer_oid_variant="v2",  # only OID 1.8 present
    )
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.issuer == "https://accounts.google.com"


# Enforces: legacy fallback — when only OID 1.1 is present (older Fulcio cert),
# verify() still extracts the issuer correctly.
def test_verify_falls_back_to_legacy_issuer_oid(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        identity="alice@example.com",
        issuer_oid_variant="legacy",  # only OID 1.1 present
    )
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.issuer == "https://accounts.google.com"


# Enforces: when both OIDs are present, prefer V2.
def test_verify_prefers_oid_v2_over_legacy(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        identity="alice@example.com",
        issuer_oid_v2="https://accounts.google.com",
        issuer_oid_legacy="https://different-provider.example",
    )
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.issuer == "https://accounts.google.com"


def test_verify_does_not_originate_identity_mismatch(crypto_factory, monkeypatch):
    """Enforces: verify() never returns IDENTITY_MISMATCH directly."""
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"identity-mismatch-negative-pin"
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical,
        identity="any.subject@example.com",
        issuer="https://accounts.google.com",
    )
    bundle = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical, bundle)
    assert vr.status != signing.VerifyStatus.IDENTITY_MISMATCH


# Enforces: non-ASCII identity in the cert SAN round-trips correctly. Catches
# the .decode/.encode bug class.
def test_verify_extracts_non_ascii_subject(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        identity="josé@example.com",
    )
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == "josé@example.com"


# ---------------------------------------------------------------------------
# TRUST_ROOT_STALE — bundle predates verifier's trust root snapshot
# ---------------------------------------------------------------------------


# Enforces: when the bundle's signing time falls outside the verifier's
# trusted_root.json era windows, status is TRUST_ROOT_STALE. Recoverable by
# refreshing the trust root, so distinct from CERT_INVALID (non-recoverable).
def test_verify_trust_root_stale(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        trust_root_predates_bundle=True,
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.TRUST_ROOT_STALE


# Enforces: finding-20260513-w5hq §3(a), finding-20260512-eaft blocker #5,
# and finding-20260512-sr0w actionable #4. A matching trust_root_pin selects
# the era-correct local trust root instead of TUF-current.
def test_verify_uses_era_correct_trust_root_when_pin_matches(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    old_root = crypto_factory.make_era_trust_root(era="2026-01")
    current_root = crypto_factory.make_era_trust_root(era="2026-05")
    pin = crypto_factory.trust_root_pin(old_root)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        trust_roots=[old_root, current_root],
    )
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        trust_root=old_root,
    )
    blob = crypto_factory.set_trust_root_pin(blob, pin)
    sb = _signature_bundle(canonical_bytes_simple, blob, trust_root_pin=pin)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Enforces: finding-20260513-w5hq §3(b). Missing trust_root_pin preserves the
# pre-addendum behavior: verifier falls back to the current TUF root.
def test_verify_falls_back_to_current_tuf_when_pin_missing(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    current_root = crypto_factory.make_era_trust_root(era="2026-05")
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        current_trust_root=current_root,
    )
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        trust_root=current_root,
    )
    sb = _signature_bundle(canonical_bytes_simple, blob, trust_root_pin=None)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Enforces: finding-20260513-w5hq §3(c). A present but unknown trust_root_pin
# fails closed with TRUST_ROOT_STALE and must not silently fall through to
# current TUF.
def test_verify_fails_closed_when_pin_mismatched(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    current_root = crypto_factory.make_era_trust_root(era="2026-05")
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        current_trust_root=current_root,
    )
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        trust_root=current_root,
    )
    sb = _signature_bundle(
        canonical_bytes_simple,
        blob,
        trust_root_pin="sha256:missing-trust-root",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.TRUST_ROOT_STALE


# Enforces: finding-20260513-w5hq §3(c). If the local root file exists but its
# content hash does not match trust_root_pin, verification fails closed.
def test_verify_fails_closed_when_pin_content_mismatch(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    actual_root = crypto_factory.make_era_trust_root(era="2026-01")
    mismatched_pin = "sha256:" + "0" * 64
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        trust_roots=[actual_root],
    )
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        trust_root=actual_root,
    )
    blob = crypto_factory.set_trust_root_pin(blob, mismatched_pin)
    sb = _signature_bundle(
        canonical_bytes_simple,
        blob,
        trust_root_pin=mismatched_pin,
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.TRUST_ROOT_STALE


# Enforces: finding-20260513-w5hq §3(c) intent + Oracle B-review O-A6. When a
# trust_root_pin matches a known era root but that root cannot be materialized
# into a real sigstore TrustedRoot, the pin cannot be honored and verification
# must fail closed (TRUST_ROOT_STALE) — not silently fall back to the current
# TUF root, which would verify against a different root than the bundle pinned.
def test_verify_fails_closed_when_pin_matches_but_root_unmaterializable(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    unmaterializable_root = crypto_factory.make_era_trust_root(
        era="2026-01", unmaterializable=True
    )
    pin = crypto_factory.trust_root_pin(unmaterializable_root)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        trust_roots=[unmaterializable_root],
    )
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        trust_root=unmaterializable_root,
    )
    blob = crypto_factory.set_trust_root_pin(blob, pin)
    sb = _signature_bundle(canonical_bytes_simple, blob, trust_root_pin=pin)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.TRUST_ROOT_STALE


# Enforces: finding-20260513-w5hq §3(c). If a pin is present and the verifier
# has no local trust roots at all, the more precise offline status is
# OFFLINE_NO_TRUSTED_ROOT.
def test_verify_offline_no_trusted_root_when_pin_present_but_no_roots_at_all(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        offline=True,
        trust_roots=[],
    )
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    sb = _signature_bundle(
        canonical_bytes_simple,
        blob,
        trust_root_pin="sha256:missing-trust-root",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT


# ---------------------------------------------------------------------------
# BUNDLE_MALFORMED — parse failures + unknown identity_scheme
# ---------------------------------------------------------------------------


# Enforces: garbage JSON in bundles[0] returns BUNDLE_MALFORMED.
def test_verify_bundle_malformed_on_invalid_json(canonical_bytes_simple, make_bundle):
    sb = make_bundle(
        canonical_bytes=canonical_bytes_simple,
        bundles=["this-is-not-json"],
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: truncated bundle JSON returns BUNDLE_MALFORMED.
def test_verify_bundle_malformed_on_truncated_json(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch, make_bundle
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    blob = result.bundle_json
    truncated = crypto_factory.truncate(blob, len(blob) // 2)
    sb = make_bundle(canonical_bytes=canonical_bytes_simple, bundles=[truncated])
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: unknown identity_scheme is fail-closed BUNDLE_MALFORMED per
# Identity rev 5 Identity Scheme Registry: "Unknown identity_scheme values
# MUST be treated as unverifiable (fail-closed)."
def test_verify_unknown_identity_scheme_is_bundle_malformed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch, make_bundle
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = make_bundle(
        canonical_bytes=canonical_bytes_simple,
        bundles=[result.bundle_json],
        identity_scheme="sigstore-future-vNext",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: future PQC migration scheme (reserved name) is also fail-closed.
def test_verify_reserved_future_scheme_is_fail_closed(
    canonical_bytes_simple, make_bundle
):
    for scheme in ("sigstore-public-v2", "sigstore-private-v1", "did-key-v1"):
        sb = make_bundle(
            canonical_bytes=canonical_bytes_simple,
            bundles=["<placeholder>"],
            identity_scheme=scheme,
        )
        vr = signing.verify(canonical_bytes_simple, sb)
        assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED, scheme


# Enforces: bundle JSON missing required fields returns BUNDLE_MALFORMED.
def test_verify_bundle_malformed_on_missing_required_field(
    canonical_bytes_simple, make_bundle
):
    sb = make_bundle(
        canonical_bytes=canonical_bytes_simple,
        bundles=['{"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}'],
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED


# ---------------------------------------------------------------------------
# OFFLINE_NO_TRUSTED_ROOT — offline verifier has no era-correct root
# ---------------------------------------------------------------------------


# Enforces: offline mode with no usable trust root returns OFFLINE_NO_TRUSTED_ROOT.
# Recoverable by going online; spec distinguishes this from TRUST_ROOT_STALE
# (which means "I have a root but it predates the bundle"). Two different
# operator actions.
def test_verify_offline_no_trusted_root(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, offline=True, trust_root_missing=True
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT


# Enforces: a verify() call must NEVER silently return VERIFIED when offline
# with no usable trust root. Sigstore docs are explicit: "any misbehavior by
# Rekor and Fulcio might go undetected" without independent verification material.
def test_verify_offline_no_root_never_returns_verified(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch, offline=True, trust_root_missing=True
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status != signing.VerifyStatus.VERIFIED


# Enforces: finding-20260512-sr0w blocker #2 and CORPUS.md's
# tsa/bundle.txt.late_timestamp.sigstore gap. The RFC3161 timestamp must be
# inside the Fulcio cert validity window; after notAfter is CERT_INVALID.
def test_verify_cert_invalid_on_tsa_timestamp_after_cert_not_after(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        rekor_version=2,
    )
    blob = crypto_factory.set_cert_validity(
        blob,
        not_before=CERT_NOT_BEFORE,
        not_after=CERT_NOT_AFTER,
    )
    blob = crypto_factory.set_tsa_timestamp(blob, CERT_NOT_AFTER + 1)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: finding-20260512-sr0w blocker #2. Rekor v2 bundles may carry
# multiple RFC3161 timestamps; SKEIN accepts the bundle when all are inside the
# cert validity window.
def test_verify_with_multiple_tsa_timestamps_all_valid(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        rekor_version=2,
    )
    blob = crypto_factory.set_cert_validity(
        blob,
        not_before=CERT_NOT_BEFORE,
        not_after=CERT_NOT_AFTER,
    )
    blob = crypto_factory.set_tsa_timestamp(blob, CERT_NOT_BEFORE)
    blob = crypto_factory.add_tsa_timestamp(blob, CERT_NOT_BEFORE + 1)
    blob = crypto_factory.add_tsa_timestamp(blob, CERT_NOT_AFTER)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Enforces: finding-20260512-sr0w blocker #2. Multiple RFC3161 timestamps use a
# strict all-valid policy; one timestamp outside the cert window rejects the
# bundle.
def test_verify_with_multiple_tsa_timestamps_one_outside_window(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        rekor_version=2,
    )
    blob = crypto_factory.set_cert_validity(
        blob,
        not_before=CERT_NOT_BEFORE,
        not_after=CERT_NOT_AFTER,
    )
    blob = crypto_factory.set_tsa_timestamp(blob, CERT_NOT_BEFORE + 1)
    blob = crypto_factory.add_tsa_timestamp(blob, CERT_NOT_AFTER + 1)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: finding-20260512-sr0w blocker #2. Under
# identity_scheme=sigstore-public-v1, Rekor v2 relies on RFC3161 timestamps;
# absence is malformed rather than silently falling back to wall clock.
def test_verify_no_tsa_timestamp_under_rekor_v2(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        rekor_version=2,
    )
    blob = crypto_factory.strip_tsa(blob)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: finding-20260512-sr0w blocker #2. If Rekor integrated_time and the
# RFC3161 timestamp materially disagree, use CERT_INVALID: both are sign-time
# evidence and disagreement means the cert-validity time cannot be trusted.
def test_verify_when_rekor_integrated_time_and_rfc3161_disagree(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
        rekor_version=2,
    )
    blob = crypto_factory.set_cert_validity(
        blob,
        not_before=CERT_NOT_BEFORE,
        not_after=CERT_NOT_AFTER,
    )
    blob = crypto_factory.set_rekor_integrated_time(blob, CERT_NOT_BEFORE + 1)
    blob = crypto_factory.set_tsa_timestamp(blob, CERT_NOT_AFTER + 1)
    sb = _signature_bundle(canonical_bytes_simple, blob)
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# ---------------------------------------------------------------------------
# verify() does not raise on parse/crypto failure (single-signer only)
# ---------------------------------------------------------------------------


# Enforces: verify() does NOT raise on parse/crypto failure for single-signer
# bundles. The contract is "return a VerifyResult; let the caller switch on
# status." (Exception: MultiSignerBundle and EmptySignatureBundle are caller
# errors and DO raise — those are programming errors, not domain failures.)
@pytest.mark.parametrize(
    "bundle_str",
    [
        "",  # empty
        "\x00\x00\x00\x00",  # binary garbage as string
        "{",  # malformed JSON start
        '{"mediaType":"unknown"}',  # unknown content
    ],
)
def test_verify_never_raises_on_single_malformed_bundle(
    canonical_bytes_simple, make_bundle, bundle_str
):
    sb = make_bundle(
        canonical_bytes=canonical_bytes_simple,
        bundles=[bundle_str],
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert isinstance(vr, signing.VerifyResult)


# Enforces: verify() returns VerifyResult even when the bundle is a real
# sigstore-bundle but the verifier internals raise. The wrapper translates.
def test_verify_returns_result_even_on_verifier_internal_raise(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        raise_in_verifier=True,
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert isinstance(vr, signing.VerifyResult)
    assert vr.status != signing.VerifyStatus.VERIFIED


# Enforces: verify() does NOT raise SigningUnavailable. SigningUnavailable is
# a SIGN-path-only contract.
def test_verify_does_not_raise_signing_unavailable_when_network_down(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch, network="down")
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    try:
        vr = signing.verify(canonical_bytes_simple, sb)
    except signing.SigningUnavailable:
        pytest.fail("verify() raised SigningUnavailable; should return VerifyResult")
    assert isinstance(vr, signing.VerifyResult)


# ---------------------------------------------------------------------------
# canon_version is informational only — verify uses canonical_bytes, not
# re-derived bytes (rev 5 §Canonical bytes, normative)
# ---------------------------------------------------------------------------


# Enforces: verify() does NOT call into canon.py to re-derive canonical_bytes.
# It uses the bytes the caller passed and the bytes stored in the bundle field.
# canon_version is informational only.
def test_verify_does_not_recanonicalize_via_canon_version(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="hypothetical-future-canon-9.99",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Enforces: any canon_version value (including empty, future, garbage) does not
# alter verify outcome. Phase 1 q776/sbal convergent: this is the
# canonicalization-drift bug class the rev-5 amendment closed.
@pytest.mark.parametrize(
    "canon_version_value",
    [
        "knurl-1.0",
        "knurl-99.0",
        "unknown-format",
        "",
        "🐱-1.0",
    ],
)
def test_verify_status_independent_of_canon_version(
    crypto_factory,
    google_provider,
    canonical_bytes_simple,
    monkeypatch,
    canon_version_value,
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version=canon_version_value,
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Enforces: stale bundle re-presentation continues to verify (Sigstore signatures
# are time-bound at sign time but valid forever for the signed content). Freshness,
# if needed, is the caller's policy concern.
def test_verify_stale_bundle_repeats_verified(
    crypto_factory, google_provider, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"stale bundle"
    result = signing.sign(canonical, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    r1 = signing.verify(canonical, sb)
    r100 = signing.verify(canonical, sb)
    assert r1.status == r100.status == signing.VerifyStatus.VERIFIED
