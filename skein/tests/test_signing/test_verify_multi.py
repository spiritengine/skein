"""verify_multi() contract — array semantics, overall aggregation, order
preservation, N=1/2/5, declare-continuity scenario.

verify_multi() is the canonical read API for multi-signer bundles. Per
clarification 2, calling verify_multi() on a length-1 bundle is legal and
produces a MultiVerifyResult with a single-element results array.

The overall-status aggregation is the load-bearing rule for authorization
decisions (rev 5 §verify return shape: "For authorization decisions, all
bundles in the array MUST return VERIFIED").
"""

from __future__ import annotations

import itertools
import random

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
# N=1 — single signer (the common case; legal per clarification 2)
# ---------------------------------------------------------------------------


# Enforces: verify_multi() on a length-1 bundles array produces a results list
# of length 1 and overall=VERIFIED when the single bundle verifies. Per
# clarification 2: "Calling verify_multi() on a length-1 bundle is legal."
def test_verify_multi_n1_verified(
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
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert len(multi.results) == 1
    assert multi.results[0].status == signing.VerifyStatus.VERIFIED
    assert multi.overall == signing.VerifyStatus.VERIFIED


# Enforces: verify_multi() and verify() agree on the N=1 case. Pinning
# equivalence avoids drift between the two APIs.
def test_verify_multi_n1_agrees_with_verify(
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
    one = signing.verify(canonical_bytes_simple, sb)
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert multi.results[0].status == one.status
    assert multi.results[0].issuer == one.issuer
    assert multi.results[0].subject == one.subject


# ---------------------------------------------------------------------------
# N=2 — declare-continuity scenario
# ---------------------------------------------------------------------------


# Enforces: verify_multi() on a length-2 bundles array (declare-continuity)
# returns 2 results, both VERIFIED, overall=VERIFIED. Each signer signs the
# same canonical_bytes per Identity rev 5 §"declare-continuity flow".
def test_verify_multi_n2_continuity_both_verified(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_google = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob_github = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice-gh",
        issuer="https://github.com/login/oauth",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_google, blob_github],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert len(multi.results) == 2
    assert all(r.status == signing.VerifyStatus.VERIFIED for r in multi.results)
    assert multi.overall == signing.VerifyStatus.VERIFIED


# Enforces: verify_multi() preserves order. results[i] corresponds to bundles[i].
# Continuity depends on knowing which signer is the original vs continuity
# identity — order is the only marker.
def test_verify_multi_preserves_bundle_order(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_a = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="first@example.com",
        issuer="https://accounts.google.com",
    )
    blob_b = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="second@example.com",
        issuer="https://github.com/login/oauth",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_a, blob_b],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert multi.results[0].subject == "first@example.com"
    assert multi.results[1].subject == "second@example.com"


# Enforces: when one of two signers fails, overall != VERIFIED. The bad signer
# surfaces its specific status; the other still returns its result. Federation/UX
# renders partial-trust signals from the per-bundle results.
def test_verify_multi_partial_failure_not_overall_verified(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_ok = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob_bad = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice-gh",
        issuer="https://github.com/login/oauth",
    )
    blob_bad_tampered = crypto_factory.tamper_cert(blob_bad)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_ok, blob_bad_tampered],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert multi.results[0].status == signing.VerifyStatus.VERIFIED
    assert multi.results[1].status == signing.VerifyStatus.CERT_INVALID
    assert multi.overall != signing.VerifyStatus.VERIFIED


# Enforces: overall is non-VERIFIED iff ANY result is non-VERIFIED. Spec rev 5
# says "VERIFIED iff all results are VERIFIED" — the inverse is what we pin.
# The exact non-VERIFIED status (worst-status, first-failure, dedicated
# AGGREGATE_FAILED) is implementation-defined.
def test_verify_multi_overall_is_non_verified_iff_any_fails(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    good = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    bad = crypto_factory.tamper_cert(good)
    for variant in ([bad], [good, bad], [bad, good], [good, good, bad]):
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=variant,
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        multi = signing.verify_multi(canonical_bytes_simple, sb)
        assert multi.overall != signing.VerifyStatus.VERIFIED, variant


# Enforces: each bundle is verified independently — no shared state across signers.
def test_verify_multi_each_result_independent_status(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_a = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="a@example.com",
        issuer="https://accounts.google.com",
    )
    blob_b = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="b@example.com",
        issuer="https://accounts.google.com",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_a, blob_b],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    for r in multi.results:
        assert isinstance(r.status, signing.VerifyStatus)


# ---------------------------------------------------------------------------
# N=5 — pushing past obvious test sizes
# ---------------------------------------------------------------------------


# Enforces: verify_multi() scales to N=5 signers. No magic number caps multi-signer
# at 2 or 3. Future multi-author folios may be N=5+.
def test_verify_multi_n5_all_verified(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=f"signer{i}@example.com",
            issuer="https://accounts.google.com",
        )
        for i in range(5)
    ]
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert len(multi.results) == 5
    assert all(r.status == signing.VerifyStatus.VERIFIED for r in multi.results)
    assert multi.overall == signing.VerifyStatus.VERIFIED


# Enforces: at N=5, per-signer subjects come out distinct and in order. Catches
# the bug where the verifier reuses a single result object for all entries.
def test_verify_multi_n5_subjects_distinct_in_order(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=f"signer{i}@example.com",
            issuer="https://accounts.google.com",
        )
        for i in range(5)
    ]
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    subjects = [r.subject for r in multi.results]
    assert subjects == [f"signer{i}@example.com" for i in range(5)]


# Enforces: results[i] count matches bundles array length for arbitrary N.
@pytest.mark.parametrize("n", [1, 2, 3, 5])
def test_verify_multi_results_count_matches_bundles(
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
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert len(multi.results) == n


# ---------------------------------------------------------------------------
# All signers sign the same canonical_bytes (cross-payload prevention)
# ---------------------------------------------------------------------------


# Enforces: when one signer in the array signed DIFFERENT canonical_bytes (an
# attacker assembled a multi-bundle by reusing a sigstore-bundle from a
# different folio), the per-signer result for THAT signer must NOT be VERIFIED.
# Caller-supplied canonical_bytes governs all signature checks.
def test_verify_multi_cross_payload_signer_fails(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    legitimate = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    other_bytes = canonical_bytes_simple + b"_other_folio"
    smuggled = crypto_factory.make_bundle_blob(
        canonical_bytes=other_bytes,
        identity="bob@example.com",
        issuer="https://github.com/login/oauth",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[legitimate, smuggled],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert multi.results[0].status == signing.VerifyStatus.VERIFIED
    assert multi.results[1].status == signing.VerifyStatus.SIGNATURE_MISMATCH
    # Tightened by finding-20260514-caqj (worst-status aggregation): a
    # legitimately-VERIFIED first signer does not mask the forgery signal on
    # the second. overall surfaces the worst per-bundle status, not just
    # "something failed."
    assert multi.overall == signing.VerifyStatus.SIGNATURE_MISMATCH


# ---------------------------------------------------------------------------
# Order independence at the overall-status level
# ---------------------------------------------------------------------------


# Enforces: shuffling the bundles array does not change overall MultiVerifyResult
# status. Per spec: "each bundle is verified independently" implies overall is
# order-independent.
def test_verify_multi_shuffle_same_overall(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=f"signer{i}@example.com",
            issuer=f"https://provider-{i}.example.com",
        )
        for i in range(3)
    ]
    sb_ordered = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    shuffled = blobs[:]
    random.seed(0)
    random.shuffle(shuffled)
    sb_shuffled = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=shuffled,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    r_ordered = signing.verify_multi(canonical_bytes_simple, sb_ordered)
    r_shuffled = signing.verify_multi(canonical_bytes_simple, sb_shuffled)
    assert r_ordered.overall == r_shuffled.overall


# ---------------------------------------------------------------------------
# Unknown identity_scheme propagates to every result
# ---------------------------------------------------------------------------


# Enforces: unknown identity_scheme is BUNDLE_MALFORMED for every signer
# (scheme is global, not per-signer). overall == BUNDLE_MALFORMED.
def test_verify_multi_unknown_scheme_fail_closed_globally(
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
        identity_scheme="sigstore-future-vNext",
        bundles=blobs,
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert all(r.status == signing.VerifyStatus.BUNDLE_MALFORMED for r in multi.results)
    assert multi.overall == signing.VerifyStatus.BUNDLE_MALFORMED


# ---------------------------------------------------------------------------
# Multi-signer attack — partial structural failure
# ---------------------------------------------------------------------------


# Enforces: two-signer bundle where signer[1]'s inclusion proof is stripped
# returns INCLUSION_FAILED for signer[1] alone; signer[0] still VERIFIED;
# overall != VERIFIED. Catches an attacker appending a forged entry hoping
# the verifier only checks the first signer.
def test_verify_multi_one_valid_one_stripped_inclusion(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob_ok = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="a@example.com",
        issuer="https://accounts.google.com",
    )
    blob_bad = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="b@example.com",
        issuer="https://accounts.google.com",
    )
    blob_stripped = crypto_factory.strip_rekor_proof(blob_bad)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_ok, blob_stripped],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    assert multi.results[0].status == signing.VerifyStatus.VERIFIED
    assert multi.results[1].status == signing.VerifyStatus.INCLUSION_FAILED
    assert multi.overall != signing.VerifyStatus.VERIFIED


# Enforces: when ALL signers fail with the same status, overall == that status.
def test_verify_multi_all_fail_same_status_overall_matches(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    good = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="a@example.com",
        issuer="https://accounts.google.com",
    )
    bad = crypto_factory.tamper_cert(good)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[bad, bad],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical_bytes_simple, sb)
    for r in multi.results:
        assert r.status == signing.VerifyStatus.CERT_INVALID
    assert multi.overall == signing.VerifyStatus.CERT_INVALID


# ---------------------------------------------------------------------------
# Empty bundles arrays cannot reach verify_multi (clarification 2)
# ---------------------------------------------------------------------------


# Enforces: SignatureBundle with empty bundles raises EmptySignatureBundle at
# construction (clarification 2). So verify_multi never sees a length-0 array.
def test_empty_bundles_raises_at_construction_not_in_verify_multi(
    canonical_bytes_simple,
):
    with pytest.raises(signing.EmptySignatureBundle):
        signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )


# ---------------------------------------------------------------------------
# Worst-status aggregation with severity ordering (finding-20260514-caqj,
# closes brief-20260514-k7s1)
# ---------------------------------------------------------------------------
#
# Severity order on VerifyStatus (worst → mildest):
#
#   SIGNATURE_MISMATCH > CERT_INVALID > INCLUSION_FAILED > BUNDLE_MALFORMED >
#       TRUST_ROOT_STALE > OFFLINE_NO_TRUSTED_ROOT > IDENTITY_MISMATCH > VERIFIED
#
# overall = max(results[].status, key=severity-rank). All-VERIFIED → VERIFIED.
# Any failure → the worst failure status present (never VERIFIED).
#
# Tested severity coverage in heterogeneous mixtures: SIG_MISMATCH > CERT_INVALID
# > INCLUSION_FAILED > BUNDLE_MALFORMED. The remaining edges (TRUST_ROOT_STALE,
# OFFLINE_NO_TRUSTED_ROOT, IDENTITY_MISMATCH) are verifier-state / authorization
# concerns that apply uniformly across a SignatureBundle, not per-signer; the
# all-same-status case is already pinned by
# test_verify_multi_all_fail_same_status_overall_matches.


def _good_blob(crypto_factory, canonical_bytes, *, identity, issuer):
    return crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes,
        identity=identity,
        issuer=issuer,
    )


def _sb(canonical_bytes, blobs):
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )


# Enforces: SIGNATURE_MISMATCH outranks CERT_INVALID. A cross-payload signer
# (forgery class) wins over an untrusted-cert signer. The strongest adversarial
# signal — proof that bytes do not match a signature — surfaces as overall.
def test_verify_multi_worst_status_signature_mismatch_over_cert_invalid(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    cert_bad_seed = _good_blob(
        crypto_factory,
        canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    cert_bad = crypto_factory.tamper_cert(cert_bad_seed)
    sig_mismatch = _good_blob(
        crypto_factory,
        canonical_bytes_simple + b"_other_folio",
        identity="bob@example.com",
        issuer="https://github.com/login/oauth",
    )
    multi = signing.verify_multi(
        canonical_bytes_simple, _sb(canonical_bytes_simple, [cert_bad, sig_mismatch])
    )
    assert multi.results[0].status == signing.VerifyStatus.CERT_INVALID
    assert multi.results[1].status == signing.VerifyStatus.SIGNATURE_MISMATCH
    assert multi.overall == signing.VerifyStatus.SIGNATURE_MISMATCH


# Enforces: SIGNATURE_MISMATCH outranks INCLUSION_FAILED. Bytes-forgery beats
# stripped Rekor proof.
def test_verify_multi_worst_status_signature_mismatch_over_inclusion_failed(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    incl_seed = _good_blob(
        crypto_factory,
        canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    incl_failed = crypto_factory.strip_rekor_proof(incl_seed)
    sig_mismatch = _good_blob(
        crypto_factory,
        canonical_bytes_simple + b"_other_folio",
        identity="bob@example.com",
        issuer="https://github.com/login/oauth",
    )
    multi = signing.verify_multi(
        canonical_bytes_simple, _sb(canonical_bytes_simple, [incl_failed, sig_mismatch])
    )
    assert multi.results[0].status == signing.VerifyStatus.INCLUSION_FAILED
    assert multi.results[1].status == signing.VerifyStatus.SIGNATURE_MISMATCH
    assert multi.overall == signing.VerifyStatus.SIGNATURE_MISMATCH


# Enforces: CERT_INVALID outranks INCLUSION_FAILED. Untrusted issuer beats
# missing transparency-log evidence.
def test_verify_multi_worst_status_cert_invalid_over_inclusion_failed(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    cert_seed = _good_blob(
        crypto_factory,
        canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    cert_bad = crypto_factory.tamper_cert(cert_seed)
    incl_seed = _good_blob(
        crypto_factory,
        canonical_bytes_simple,
        identity="bob@example.com",
        issuer="https://github.com/login/oauth",
    )
    incl_failed = crypto_factory.strip_rekor_proof(incl_seed)
    multi = signing.verify_multi(
        canonical_bytes_simple, _sb(canonical_bytes_simple, [cert_bad, incl_failed])
    )
    assert multi.results[0].status == signing.VerifyStatus.CERT_INVALID
    assert multi.results[1].status == signing.VerifyStatus.INCLUSION_FAILED
    assert multi.overall == signing.VerifyStatus.CERT_INVALID


# Enforces: INCLUSION_FAILED outranks BUNDLE_MALFORMED. A signer with a
# stripped Rekor proof outranks a signer whose bundle is just unparseable.
def test_verify_multi_worst_status_inclusion_failed_over_bundle_malformed(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    incl_seed = _good_blob(
        crypto_factory,
        canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    incl_failed = crypto_factory.strip_rekor_proof(incl_seed)
    malformed_seed = _good_blob(
        crypto_factory,
        canonical_bytes_simple,
        identity="bob@example.com",
        issuer="https://github.com/login/oauth",
    )
    malformed = crypto_factory.truncate(
        malformed_seed, max(1, len(malformed_seed) // 2)
    )
    multi = signing.verify_multi(
        canonical_bytes_simple, _sb(canonical_bytes_simple, [malformed, incl_failed])
    )
    assert multi.results[0].status == signing.VerifyStatus.BUNDLE_MALFORMED
    assert multi.results[1].status == signing.VerifyStatus.INCLUSION_FAILED
    assert multi.overall == signing.VerifyStatus.INCLUSION_FAILED


# Enforces: a three-way heterogeneous mixture (CERT_INVALID, INCLUSION_FAILED,
# SIGNATURE_MISMATCH) surfaces SIGNATURE_MISMATCH as overall. The full
# top-of-order rule under realistic adversarial mix.
def test_verify_multi_worst_status_three_way_heterogeneous_picks_signature_mismatch(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    cert_bad = crypto_factory.tamper_cert(
        _good_blob(
            crypto_factory,
            canonical_bytes_simple,
            identity="alice@example.com",
            issuer="https://accounts.google.com",
        )
    )
    incl_failed = crypto_factory.strip_rekor_proof(
        _good_blob(
            crypto_factory,
            canonical_bytes_simple,
            identity="bob@example.com",
            issuer="https://github.com/login/oauth",
        )
    )
    sig_mismatch = _good_blob(
        crypto_factory,
        canonical_bytes_simple + b"_other_folio",
        identity="carol@example.com",
        issuer="https://accounts.google.com",
    )
    multi = signing.verify_multi(
        canonical_bytes_simple,
        _sb(canonical_bytes_simple, [cert_bad, incl_failed, sig_mismatch]),
    )
    statuses = [r.status for r in multi.results]
    assert signing.VerifyStatus.CERT_INVALID in statuses
    assert signing.VerifyStatus.INCLUSION_FAILED in statuses
    assert signing.VerifyStatus.SIGNATURE_MISMATCH in statuses
    assert multi.overall == signing.VerifyStatus.SIGNATURE_MISMATCH


# Enforces: shuffle-invariance over a heterogeneous failure mixture. The
# worst-status rule is permutation-invariant by construction — verify it
# directly against an adversary that reorders bundles to try to change the
# overall classification.
def test_verify_multi_worst_status_invariant_under_shuffle_with_heterogeneous_failures(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    cert_bad = crypto_factory.tamper_cert(
        _good_blob(
            crypto_factory,
            canonical_bytes_simple,
            identity="alice@example.com",
            issuer="https://accounts.google.com",
        )
    )
    incl_failed = crypto_factory.strip_rekor_proof(
        _good_blob(
            crypto_factory,
            canonical_bytes_simple,
            identity="bob@example.com",
            issuer="https://github.com/login/oauth",
        )
    )
    malformed = crypto_factory.truncate(
        _good_blob(
            crypto_factory,
            canonical_bytes_simple,
            identity="carol@example.com",
            issuer="https://accounts.google.com",
        ),
        16,
    )
    overalls = set()
    permutations = list(itertools.permutations([cert_bad, incl_failed, malformed]))
    for perm in permutations:
        multi = signing.verify_multi(
            canonical_bytes_simple, _sb(canonical_bytes_simple, perm)
        )
        overalls.add(multi.overall)
    assert overalls == {signing.VerifyStatus.CERT_INVALID}, (
        f"overall must be invariant under shuffle of the same failure multiset; got {overalls}"
    )


# Enforces: a single VERIFIED result among failures does not save overall.
# overall surfaces the worst failure, NOT a soft-pass on "at least someone
# verified."
def test_verify_multi_worst_status_one_verified_does_not_save_overall(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    good = _good_blob(
        crypto_factory,
        canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    cert_bad = crypto_factory.tamper_cert(
        _good_blob(
            crypto_factory,
            canonical_bytes_simple,
            identity="bob@example.com",
            issuer="https://github.com/login/oauth",
        )
    )
    multi = signing.verify_multi(
        canonical_bytes_simple, _sb(canonical_bytes_simple, [good, cert_bad])
    )
    assert multi.results[0].status == signing.VerifyStatus.VERIFIED
    assert multi.results[1].status == signing.VerifyStatus.CERT_INVALID
    assert multi.overall == signing.VerifyStatus.CERT_INVALID
    assert multi.overall != signing.VerifyStatus.VERIFIED
