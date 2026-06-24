"""Conformance corpus tests against sigstore-python's MIT-licensed test/assets/
bundle_* fixtures.

Goal: cross-implementation portability. A bundle that sigstore-python verifies
clean must verify clean through SKEIN.signing. A bundle sigstore-python rejects
must be rejected by SKEIN.signing with the appropriate status.

Corpus expected at tests/conformance/corpus/ — vendored from sigstore-python's
test/assets/ (MIT-licensed). Tests skip cleanly when a specific corpus file is
absent so phase 3 implementer can iterate.

Note on staging vs production: corpus bundles are signed against staging
Sigstore. The phase-3 implementer must support a staging trust root override
for these tests (env var SIGSTORE_TUF_CACHE_DIR or a Verifier.staging path).

See tests/conformance/CORPUS.md for the corpus-to-test mapping.
"""

from __future__ import annotations

import json

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


# Tests that need the staging TUF root. Phase-3 must support staging mode.
#
# Footgun: this file applies @conformance_staging at FUNCTION level (no class
# wrapper), unlike tests/conformance/test_signing_conformance.py which marks at
# CLASS level. An undecorated function that drives a staging-signed corpus
# through signing.verify() silently runs the production verifier and fails the
# staging cert chain → CERT_INVALID. Behind a loose pin (`!= VERIFIED` or a
# disjunction) that masks the real assertion. Any new staging-corpus test here
# MUST carry @conformance_staging. (finding-20260519-xsrk / -syos defect class.)
conformance_staging = pytest.mark.conformance_staging


def _make_unpatched_staging_verifier_or_skip(crypto_factory):
    make_verifier = getattr(crypto_factory, "make_staging_verifier", None)
    if make_verifier is None:
        pytest.skip(
            "crypto_factory.make_staging_verifier() not present; Phase 3 must "
            "provide an unpatched sigstore-python verifier with the test trust root."
        )
    return make_verifier()


# ---------------------------------------------------------------------------
# Section 1: Known-good v0.3 bundles must verify
# ---------------------------------------------------------------------------


# Enforces: sigstore-python's canonical good-bundle fixture verifies clean.
@conformance_staging
def test_conformance_bundle_v3_verifies(corpus):
    artifact = corpus("bundle_v3.txt").read_bytes()
    blob = corpus("bundle_v3.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.VERIFIED


# Enforces: VERIFIED result carries non-None issuer + subject extracted from
# Fulcio cert. Per spec rev 5: issuer is the OIDC URL (not a shortname).
@conformance_staging
def test_conformance_bundle_v3_identity_fields(corpus):
    artifact = corpus("bundle_v3.txt").read_bytes()
    blob = corpus("bundle_v3.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.VERIFIED
    assert result.issuer is not None
    assert result.subject is not None
    # Per spec rev 5: account_binding stores issuer URL, not shortname.
    assert result.issuer.startswith("https://"), (
        f"Issuer must be an OIDC URL, got: {result.issuer!r}"
    )


# Enforces: an alternate good-bundle fixture verifies cleanly — multiple known
# bundles, not just one hard-coded case.
@conformance_staging
def test_conformance_bundle_v3_alt_verifies(corpus):
    artifact = corpus("bundle_v3_alt.txt").read_bytes()
    blob = corpus("bundle_v3_alt.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.VERIFIED


# Enforces: RFC3161 TSA timestamp bundle (Rekor v2 default path per spec rev 5).
@conformance_staging
def test_conformance_bundle_tsa_verifies(corpus):
    artifact = corpus("tsa/bundle.txt").read_bytes()
    blob = corpus("tsa/bundle.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.VERIFIED


# Enforces: bundle_v3_no_signed_time — v0.3 bundle without inclusionPromise (SET).
# v0.3 spec arguably allows omitting SET when an inclusion proof is present.
#
# Disposition (finding-20260519-syos, Option E1, mechanism refined per the syos
# fell cycle): sigstore-python 4.2.0 Bundle._verify structurally requires SET OR
# timestampVerificationData on v0.3; this corpus has neither. Accepted as a
# documented v0 library constraint, NOT gated on an upgrade. xfail(strict=True)
# rather than skip: the body runs and asserts the spec-intended VERIFIED; under
# the locked library it fails (→ xfail), but a future library that accepts
# inclusion-proof-only v0.3 XPASSes → strict=True fails loudly. A permanent skip
# would silently absorb that upgrade signal — the exact flaw Option A rejects.
#
# @conformance_staging is REQUIRED here (not optional as it was when this was a
# skip): the corpus is staging-signed, so for the strict=True tripwire to ever
# XPASS on a future library, VERIFIED must be reachable — which needs the
# staging verifier. Without it signing.verify() uses Verifier.production(),
# the staging cert fails the production trust root → CERT_INVALID, the body
# never reaches VERIFIED, and strict=True is inert (degrades to permanent-skip,
# the exact flaw this disposition rejects). See the file-header footgun note.
@conformance_staging
@pytest.mark.xfail(
    strict=True,
    reason=(
        "sigstore-python 4.2.0 Bundle._verify requires inclusion_promise (SET) "
        "OR timestampVerificationData on v0.3 bundles; this corpus has neither. "
        "Documented v0 library constraint per finding-20260519-syos (Option "
        "E1). strict=True so a future library accepting inclusion-proof-only "
        "v0.3 fails loudly."
    ),
)
def test_conformance_bundle_v3_no_signed_time_verifies(corpus):
    artifact = corpus("bundle_v3_no_signed_time.txt").read_bytes()
    blob = corpus("bundle_v3_no_signed_time.txt.sigstore.json").read_text(
        encoding="utf-8"
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Section 2: Known-bad bundles MUST be rejected
# ---------------------------------------------------------------------------


# Enforces: CVE-2022-36056 bundle (tlog entry inconsistent with signature
# material) is rejected. Patched in sigstore-python >=3.5.3. 4.2.0 surfaces the
# splice as a generic VerificationError → d3u6 §5 catch-all → BUNDLE_MALFORMED.
# Pin tightened from `!= VERIFIED` to exact per finding-20260519-syos (Option A;
# parity with conformance/test_signing_conformance.py::test_cve_2022_36056).
#
# Needs @conformance_staging (see file-header footgun note): without the
# staging redirect this staging-signed corpus hits Verifier.production() →
# CERT_INVALID, masking the real tlog-inconsistency rejection. This file was
# outside xsrk's fix; the Option A tightening surfaced this instance. The
# second instance on this corpus (test_skein_and_sigstore_python_agree_on_cve_
# bundle) was surfaced by the oracle rerun and fixed in the same fell cycle.
@conformance_staging
def test_conformance_cve_2022_36056_rejected(corpus):
    artifact = corpus("bundle_cve_2022_36056.txt").read_bytes()
    blob = corpus("bundle_cve_2022_36056.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: invalid mediaType is BUNDLE_MALFORMED.
def test_conformance_invalid_version_rejected(corpus):
    artifact = corpus("bundle_invalid_version.txt").read_bytes()
    blob = corpus("bundle_invalid_version.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: v0.2 bundle with SET only (no checkpoint) is Rekor v1 — rejected by
# sigstore-public-v1 scheme. 4.2.0 rejects this shape structurally at
# Bundle.from_json (InvalidBundle) → d3u6 §5 → BUNDLE_MALFORMED. Pin tightened
# from the disjunction to exact per finding-20260519-syos (Option A).
def test_conformance_no_checkpoint_rejected(corpus):
    artifact = corpus("bundle_no_checkpoint.txt").read_bytes()
    blob = corpus("bundle_no_checkpoint.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: v0.1 bundle with no tlog entry is rejected. 4.2.0 rejects this
# shape structurally at Bundle.from_json (InvalidBundle: "expected exactly one
# log entry in bundle") → d3u6 §5 → BUNDLE_MALFORMED. Pin tightened from the
# disjunction to exact per finding-20260519-syos (Option A).
def test_conformance_no_log_entry_rejected(corpus):
    artifact = corpus("bundle_no_log_entry.txt").read_bytes()
    blob = corpus("bundle_no_log_entry.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: v0.1 bundle without leaf certificate (uses x509CertificateChain) is
# rejected by sigstore-public-v1 (requires cert-in-bundle leaf shape).
#
# Pin tightened from `in (CERT_INVALID, BUNDLE_MALFORMED)` to exact per
# finding-20260519-syos (Option A discipline). sigstore-python 4.2.0 rejects
# this shape at Bundle.from_json (InvalidBundle: "expected non-empty
# certificate chain in bundle") → d3u6 §5 → BUNDLE_MALFORMED, deterministically
# and marker-independent (production == staging). The CERT_INVALID branch is
# dead under the locked library — the exact dead-disjunction anti-pattern
# Option A rejects. Surfaced by the knuth rerun (syos-delegated).
def test_conformance_no_cert_v1_rejected(corpus):
    artifact = corpus("bundle_no_cert_v1.txt").read_bytes()
    blob = corpus("bundle_no_cert_v1.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# ---------------------------------------------------------------------------
# Section 3: Cross-implementation portability — sigstore-python agreement
# ---------------------------------------------------------------------------


# Enforces: a bundle SKEIN verifies must also verify via sigstore-python's
# Verifier directly. Portability gate: if SKEIN says VERIFIED but sigstore-python
# raises VerificationError, SKEIN has accepted something the ecosystem rejects.
@conformance_staging
def test_skein_and_sigstore_python_agree_on_good_bundle(corpus):
    from sigstore.errors import VerificationError
    from sigstore.models import Bundle
    from sigstore.verify import policy as sigstore_policy
    from sigstore.verify.verifier import Verifier

    artifact = corpus("bundle_v3.txt").read_bytes()
    blob_str = corpus("bundle_v3.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_str],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    # Path 1: SKEIN verify
    skein_result = signing.verify(artifact, sb)
    assert skein_result.status == signing.VerifyStatus.VERIFIED

    # Path 2: sigstore-python direct verify (same blob, same artifact)
    verifier = Verifier.staging(offline=True)
    bundle = Bundle.from_json(blob_str)
    try:
        verifier.verify_artifact(artifact, bundle, sigstore_policy.UnsafeNoOp())
        sigstore_verified = True
    except VerificationError:
        sigstore_verified = False
    assert sigstore_verified, (
        "SKEIN returned VERIFIED but sigstore-python raised VerificationError "
        "on the same bundle blob. This is a portability regression."
    )


# Enforces: CVE bundle is rejected by both SKEIN and sigstore-python.
#
# Needs @conformance_staging for the same reason as
# test_conformance_cve_2022_36056_rejected: the CVE corpus is staging-signed
# and parses cleanly, so the SKEIN path (Path 1) reaches verifier selection.
# Without the staging redirect Path 1 hits Verifier.production() and fails the
# staging cert chain → CERT_INVALID, which is `!= VERIFIED` and so passed the
# old loose pin vacuously — the CVE tlog-splice rejection was never exercised
# on the SKEIN path. Same Group 1 marker-wiring class as
# finding-20260519-xsrk; this was the fourth instance, surfaced by the
# oracle rerun (finding-20260519-syos fell cycle) and fixed here.
@conformance_staging
def test_skein_and_sigstore_python_agree_on_cve_bundle(corpus):
    from sigstore.errors import VerificationError
    from sigstore.models import Bundle
    from sigstore.verify import policy as sigstore_policy
    from sigstore.verify.verifier import Verifier

    artifact = corpus("bundle_cve_2022_36056.txt").read_bytes()
    blob_str = corpus("bundle_cve_2022_36056.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_str],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    skein_result = signing.verify(artifact, sb)
    # Exact pin (was `!= VERIFIED`): under the staging redirect the splice is
    # reached and surfaces as a generic VerificationError → d3u6 §5 catch-all →
    # BUNDLE_MALFORMED. Parity with test_conformance_cve_2022_36056_rejected
    # and Option A's "every pin exact" discipline (finding-20260519-syos).
    assert skein_result.status == signing.VerifyStatus.BUNDLE_MALFORMED, (
        "SKEIN must reject the CVE-2022-36056 bundle as BUNDLE_MALFORMED "
        f"(security regression); got {skein_result.status}."
    )

    verifier = Verifier.staging(offline=True)
    bundle = Bundle.from_json(blob_str)
    with pytest.raises(VerificationError):
        verifier.verify_artifact(artifact, bundle, sigstore_policy.UnsafeNoOp())


# Enforces: bundle stored then reloaded (JSON parse → serialize → reparse)
# verifies identically. SKEIN's storage path must not depend on byte identity.
@conformance_staging
def test_conformance_bundle_json_round_trip_does_not_corrupt(corpus):
    from sigstore.models import Bundle

    artifact = corpus("bundle_v3.txt").read_bytes()
    blob_str = corpus("bundle_v3.txt.sigstore").read_text(encoding="utf-8")
    bundle_obj = Bundle.from_json(blob_str)
    reserialized = bundle_obj.to_json()

    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[reserialized],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.VERIFIED


# Enforces: end-to-end SKEIN sign → sigstore-python verify. Requires a real
# OIDC token; gated by SKEIN_RUN_SIGSTORE_LIVE=1.
@pytest.mark.skip(
    reason="requires OIDC: set SKEIN_RUN_SIGSTORE_LIVE=1 and provide token"
)
def test_skein_sign_bundle_is_portable_to_sigstore_verify():
    from sigstore.errors import VerificationError
    from sigstore.models import Bundle
    from sigstore.verify import policy as sigstore_policy
    from sigstore.verify.verifier import Verifier

    canonical_bytes = b"portable-to-sigstore"
    # In practice, requires a real OIDCProviderConfig with a valid token.
    cfg = signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token="oidc-token-acquired-out-of-band",
        provider_id="google",
    )
    sign_result = signing.sign(canonical_bytes, cfg)
    bundle = Bundle.from_json(sign_result.bundle_json)
    verifier = Verifier.staging(offline=True)
    try:
        verifier.verify_artifact(canonical_bytes, bundle, sigstore_policy.UnsafeNoOp())
    except VerificationError as e:
        pytest.fail(f"sigstore-python rejected a bundle produced by SKEIN.sign(): {e}")


# Enforces: finding-20260512-eaft blocker #3. The CI-safe stand-in for the true
# OIDC round-trip still drives SKEIN.sign() and then asks sigstore-python's
# Bundle parser + Verifier to accept the produced bundle JSON.
@conformance_staging
def test_skein_sign_output_verifies_in_sigstore_python(
    crypto_factory,
    google_provider,
    monkeypatch,
):
    from sigstore.models import Bundle
    from sigstore.verify import policy as sigstore_policy

    canonical_bytes = b"SKEIN sign output verifies in sigstore-python"
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)

    sign_result = signing.sign(canonical_bytes, google_provider)
    bundle = Bundle.from_json(sign_result.bundle_json)
    verifier = _make_unpatched_staging_verifier_or_skip(crypto_factory)
    verifier.verify_artifact(canonical_bytes, bundle, sigstore_policy.UnsafeNoOp())


# Enforces: finding-20260512-eaft blocker #3. SKEIN.sign() must emit
# sigstore-bundle v0.3 ProtoJSON that sigstore-python parses without repair.
@conformance_staging
def test_skein_sign_output_format_matches_sigstore_bundle_v03(
    crypto_factory,
    google_provider,
    monkeypatch,
):
    from sigstore.models import Bundle

    canonical_bytes = b"SKEIN sign output format is sigstore bundle v0.3"
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)

    sign_result = signing.sign(canonical_bytes, google_provider)
    Bundle.from_json(sign_result.bundle_json)
    payload = json.loads(sign_result.bundle_json)
    assert payload["mediaType"].endswith("v0.3+json")


# Enforces: finding-20260512-eaft blocker #3. The same SKEIN.sign() output must
# verify through both SKEIN.verify() and sigstore-python's Verifier.
@conformance_staging
def test_canonical_bytes_round_trip_through_both_verifiers(
    crypto_factory,
    google_provider,
    monkeypatch,
):
    from sigstore.models import Bundle
    from sigstore.verify import policy as sigstore_policy

    canonical_bytes = b"canonical bytes round-trip through both verifiers"
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)

    sign_result = signing.sign(canonical_bytes, google_provider)
    bundle = Bundle.from_json(sign_result.bundle_json)
    verifier = _make_unpatched_staging_verifier_or_skip(crypto_factory)
    verifier.verify_artifact(canonical_bytes, bundle, sigstore_policy.UnsafeNoOp())

    crypto_factory.install_verify_monkeypatch(monkeypatch)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[sign_result.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    skein_result = signing.verify(canonical_bytes, sb)
    assert skein_result.status == signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Section 4: JSON shape conformance
# ---------------------------------------------------------------------------


# Enforces: corpus bundles parse as v0.3 ProtoJSON, which is what
# SKEIN.signing must accept.
def test_corpus_bundles_are_v03(corpus):
    blob_str = corpus("bundle_v3.txt.sigstore").read_text(encoding="utf-8")
    payload = json.loads(blob_str)
    assert payload["mediaType"].endswith("v0.3+json")


# Enforces: SKEIN tolerates extra unknown JSON fields on the bundle envelope
# (forward-compat).
@conformance_staging
def test_conformance_extra_unknown_fields_do_not_break_verify(corpus):

    artifact = corpus("bundle_v3.txt").read_bytes()
    blob_str = corpus("bundle_v3.txt.sigstore").read_text(encoding="utf-8")
    blob = json.loads(blob_str)
    blob["__future_unknown_field__"] = {"nested": True}
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[json.dumps(blob)],
        canonical_bytes=artifact,
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status != signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: tampering with stored canonical_bytes — i.e. caller passes original,
# but bundle's stored canonical_bytes field was rewritten — is rejected.
@conformance_staging
def test_conformance_tampered_canonical_bytes_rejected(corpus):
    artifact = corpus("bundle_v3.txt").read_bytes()
    blob_str = corpus("bundle_v3.txt.sigstore").read_text(encoding="utf-8")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob_str],
        canonical_bytes=b"tampered content (not what was signed)",
        canon_version="knurl-1.0",
    )
    result = signing.verify(artifact, sb)
    assert result.status == signing.VerifyStatus.SIGNATURE_MISMATCH
