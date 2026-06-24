"""Phase 2 test contract: signing.py — cross-implementation conformance (Team A.4)

This module tests SKEIN's signing.py verifier against the sigstore-python corpus
and against sigstore-python's own verifier directly. Every test here exists to
enforce cross-implementation compatibility: if SKEIN and sigstore-python disagree
on a bundle, that's a portability bug.

Test framing:
  - Corpus tests use bundles from sigstore-python test/assets/ (MIT-licensed).
  - All corpus bundles are staging bundles (signed against staging.sigstore.dev).
  - Tests that call verify() against real Sigstore infrastructure are marked
    @pytest.mark.conformance_staging; the rest run offline against fixture data.
  - The round-trip test verifies portability: a bundle SKEIN accepts must also
    be accepted by sigstore-python's Verifier (same data, independent verify call).

Corpus mapping: see CORPUS.md in this directory.

References:
  brief-20260511-nbz4 (Identity and Signing rev 5)
  finding-20260511-kn5j (RSP surface)
  finding-20260511-2ysh (sigstore-python library deep dive)
  finding-20260511-qejb (bundle ecosystem audit)
  brief-20260511-0o2s (phase 2 brief)
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest

# SKEIN's signing module is not yet implemented; these imports define the contract.
# Tests fail to import until skein/signing.py is implemented.
try:
    from skein.signing import (  # type: ignore[import]
        EmptySignatureBundle,
        Evidence,
        MultiVerifyResult,
        SignatureBundle,
        SigningUnavailable,
        VerifyResult,
        VerifyStatus,
        verify,
        verify_multi,
    )

    _SIGNING_AVAILABLE = True
except ImportError:
    _SIGNING_AVAILABLE = False
    # Placeholders so the file is collectable before the module exists.
    EmptySignatureBundle = None  # type: ignore[assignment,misc]
    Evidence = None  # type: ignore[assignment,misc]
    MultiVerifyResult = None  # type: ignore[assignment,misc]
    SignatureBundle = None  # type: ignore[assignment,misc]
    SigningUnavailable = None  # type: ignore[assignment,misc]
    VerifyResult = None  # type: ignore[assignment,misc]
    VerifyStatus = None  # type: ignore[assignment,misc]
    verify = None  # type: ignore[assignment]
    verify_multi = None  # type: ignore[assignment]

pytestmark = pytest.mark.skipif(
    not _SIGNING_AVAILABLE,
    reason="skein.signing not yet implemented (Phase 3)",
)

CORPUS_DIR = Path(__file__).parent / "corpus"


# ===========================================================================
# Markers
# ===========================================================================

# Tests that call SKEIN's verify() against staging Sigstore TUF root.
# These need the staging-tuf assets baked into sigstore-python to be available.
# Run with: pytest -m conformance_staging
conformance_staging = pytest.mark.conformance_staging

# Tests that require network access to Sigstore staging infrastructure.
# Skip by default; enable with SKEIN_TEST_ONLINE=1 env var.
conformance_online = pytest.mark.conformance_online


# ===========================================================================
# Section 1: Known-good bundles — SKEIN must return VERIFIED
#
# Each test: wrap a corpus bundle in SKEIN's signature_bundle format, call
# SKEIN's verify(), assert VerifyStatus.VERIFIED.
#
# These tests use Verifier.staging() internally because the corpus bundles
# were signed against staging Sigstore. SKEIN's verify() must accept a
# staging_trust_root override for test environments; the exact mechanism
# is an implementation choice (e.g. env var SIGSTORE_TUF_CACHE_DIR or
# a parameter on verify()). See implementation note below each test.
# ===========================================================================


@conformance_staging
class TestKnownGoodBundles:
    """Corpus bundles that SKEIN must accept with status VERIFIED."""

    def test_bundle_v3_verifies(self, bundle_v3):
        """bundle_v3.txt / bundle_v3.txt.sigstore: canonical v0.3 staging bundle.

        Sigstore-python verifies this with Verifier.staging(offline=True).
        SKEIN must return VERIFIED when the canonical_bytes match the stored artifact.

        Corpus file: test/assets/bundle_v3.txt.sigstore
        Identity: william@yossarian.net via https://github.com/login/oauth
        """
        artifact, skein_bundle = bundle_v3
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.VERIFIED
        assert result.issuer is not None
        assert result.subject is not None

    def test_bundle_v3_identity_fields(self, bundle_v3):
        """Verify that (issuer, subject) are extracted from the Fulcio cert SAN.

        The cert SAN carries the identity; issuer is from OID 1.3.6.1.4.1.57264.1.8
        (Issuer V2). SKEIN must extract both — subject alone is insufficient
        (two issuers can issue the same email).

        Corpus file: test/assets/bundle_v3.txt.sigstore
        """
        artifact, skein_bundle = bundle_v3
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.VERIFIED
        # staging GitHub issuer (personal OAuth, not Actions)
        assert result.issuer == "https://github.com/login/oauth"
        assert result.subject == "william@yossarian.net"

    def test_bundle_v3_alt_verifies(self, bundle_v3_alt):
        """bundle_v3_alt.txt / bundle_v3_alt.txt.sigstore: alternate v0.3 signer.

        Ensures the verifier handles a second independent signer correctly
        (different ephemeral key, different Rekor log index, same format).

        Corpus file: test/assets/bundle_v3_alt.txt.sigstore
        """
        artifact, skein_bundle = bundle_v3_alt
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.VERIFIED

    def test_bundle_tsa_verifies(self, bundle_tsa):
        """tsa/bundle.txt / tsa/bundle.txt.sigstore: bundle with RFC3161 TSA timestamps.

        This exercises the Rekor v2 timestamp verification path: bundle carries
        timestampVerificationData (RFC3161), not just integratedTime + SET.
        This is the SKEIN v0 target path per rev 5 spec (Rekor v2, RFC3161 TSA).

        Corpus file: test/assets/tsa/bundle.txt.sigstore
        """
        artifact, skein_bundle = bundle_tsa
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.VERIFIED

    # Disposition (finding-20260519-syos, Option E1, mechanism refined per the
    # syos fell cycle): the v0.3 spec arguably permits inclusion-proof-only
    # bundles, but sigstore-python 4.2.0 Bundle._verify structurally requires
    # inclusion_promise (SET) OR timestampVerificationData; this corpus has
    # neither. Accepted as a documented v0 library constraint, NOT gated on an
    # upgrade. xfail(strict=True) rather than skip: the body runs and asserts
    # the spec-intended VERIFIED; under the locked library it fails (→ xfail),
    # but if a future sigstore-python accepts inclusion-proof-only v0.3 it
    # XPASSes and strict=True fails LOUDLY — the same loud-on-upgrade tripwire
    # Option A's own rationale demands (a permanent skip would silently absorb
    # that signal, the exact flaw Option A rejects in loose pins).
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "sigstore-python 4.2.0 Bundle._verify requires inclusion_promise "
            "(SET) OR timestampVerificationData on v0.3 bundles; this corpus "
            "has neither. Documented v0 library constraint per "
            "finding-20260519-syos (Option E1). strict=True so a future "
            "library that accepts inclusion-proof-only v0.3 fails loudly."
        ),
    )
    def test_bundle_v3_no_signed_time_verifies(self, bundle_v3_no_signed_time):
        """bundle_v3_no_signed_time: v0.3 bundle with no inclusionPromise (no SET).

        The sigstore-bundle v0.3 spec makes inclusionPromise optional when an
        inclusion proof is present. SKEIN must NOT require the SET field for v0.3.
        This was a regression vector in earlier Sigstore verifier versions.

        Corpus file: test/assets/bundle_v3_no_signed_time.txt.sigstore.json
        """
        artifact, skein_bundle = bundle_v3_no_signed_time
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.VERIFIED


# ===========================================================================
# Section 2: Known-bad bundles — SKEIN must return a specific non-VERIFIED status
#
# These tests run without network access: the rejection reason is cryptographic
# or structural, not infrastructure-dependent. The CVE bundle fails regardless
# of whether the Sigstore log is reachable.
# ===========================================================================


# Routes corpus tests reaching verify_artifact through the staging trust root
# (the bundles are staging-signed; without this the production verifier fails
# the staging TSA chain → spurious CERT_INVALID). Harmless on tests that
# reject before verifier selection (profile / Bundle.from_json).
@conformance_staging
class TestKnownBadBundles:
    """Corpus bundles that SKEIN must reject. Each should produce a specific VerifyStatus."""

    def test_cve_2022_36056_rejected(self, bundle_cve_2022_36056):
        """bundle_cve_2022_36056: log entry inconsistent with bundle material.

        CVE-2022-36056 exploited a bundle manipulation where the transparency log
        entry could be swapped for one that doesn't match the signature/cert tuple.
        A correctly-patched verifier (sigstore-python >=3.5.3, >=4.0) raises
        'transparency log entry is inconsistent with other materials'. The
        splice IS rejected (fail-closed) — the security property holds.

        Expected: BUNDLE_MALFORMED. sigstore-python 4.2.0 surfaces the splice
        as a generic VerificationError (NOT the InvalidRekorEntry subclass that
        maps to INCLUSION_FAILED), so it falls to the d3u6 §5 catch-all →
        BUNDLE_MALFORMED. Reaching INCLUSION_FAILED would require string-matching
        the library message — the brittleness the d3u6 2026-05-14 amendment
        removed. Status pin amended from INCLUSION_FAILED per
        finding-20260519-syos (Option A); supersedes finding-20260512-eaft
        line 213 for this input under the locked library.

        Corpus file: test/assets/bundle_cve_2022_36056.txt.sigstore
        Sigstore-python test: test_verifier_inconsistent_log_entry (test/unit/verify/test_verifier.py)
        CVE: https://github.com/sigstore/cosign/security/advisories/GHSA-8gw7-4j42-w388
        """
        artifact, skein_bundle = bundle_cve_2022_36056
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_invalid_media_type_rejected(self, bundle_invalid_version):
        """bundle_invalid_version: mediaType is 'this is completely wrong'.

        A bundle with an unrecognizable mediaType must be rejected at parse time.
        SKEIN maps this to BUNDLE_MALFORMED (structure/parse failure, non-recoverable).

        Corpus file: test/assets/bundle_invalid_version.txt.sigstore
        """
        artifact, skein_bundle = bundle_invalid_version
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_no_checkpoint_rejected(self, bundle_no_checkpoint):
        """bundle_no_checkpoint: v0.2 bundle with SET only, no inclusionProof.checkpoint.

        The sigstore-public-v1 identity_scheme requires Rekor v2 (checkpoint-backed
        inclusion proofs). A bundle that only has a signedEntryTimestamp (SET, Rekor v1)
        and no checkpoint does not satisfy the v2 verification requirement.

        Per Identity rev 5 registry rules: 'sigstore-public-v1 must accept Rekor v2
        bundles and reject Rekor v1 bundles.' This is the Rekor v1 rejection case.

        Expected: BUNDLE_MALFORMED. sigstore-python 4.2.0 rejects this Rekor v1
        shape structurally at Bundle.from_json (InvalidBundle: "entry must
        contain inclusion proof, with checkpoint"), before any inclusion-proof
        stage. The honest exception mapping (d3u6 §5: InvalidBundle →
        BUNDLE_MALFORMED) yields BUNDLE_MALFORMED. The Rekor v1 rejection still
        holds; only the status code is structural rather than inclusion-stage.
        Status pin amended from INCLUSION_FAILED per finding-20260519-syos
        (Option A) — makes this pin consistent with the locked d3u6 §5 table.

        Corpus file: test/assets/bundle_no_checkpoint.txt.sigstore
        """
        artifact, skein_bundle = bundle_no_checkpoint
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_no_log_entry_rejected(self, bundle_no_log_entry):
        """bundle_no_log_entry: v0.1 bundle with empty tlogEntries array.

        Without a transparency log entry, there is no inclusion proof. SKEIN
        cannot verify the Rekor commitment and must reject the bundle.

        Expected: BUNDLE_MALFORMED. sigstore-python 4.2.0 rejects this v0.1
        shape structurally at Bundle.from_json (InvalidBundle: "expected
        exactly one log entry in bundle"), before any inclusion-proof stage.
        The honest exception mapping (d3u6 §5: InvalidBundle → BUNDLE_MALFORMED)
        yields BUNDLE_MALFORMED. Status pin amended from INCLUSION_FAILED per
        finding-20260519-syos (Option A) — consistent with the locked d3u6 §5.

        Corpus file: test/assets/bundle_no_log_entry.txt.sigstore
        """
        artifact, skein_bundle = bundle_no_log_entry
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_wrong_canonical_bytes_rejected(self, bundle_v3):
        """Passing canonical_bytes that don't match the stored bundle canonical_bytes.

        The bundle contains canonical_bytes = base64(original_artifact).
        If the caller passes different content, the ECDSA signature over
        original_artifact will not verify against the submitted bytes.
        Must return SIGNATURE_MISMATCH.

        This is not a corpus file mismatch — it simulates a folio whose content
        was modified after signing.
        """
        _artifact, skein_bundle = bundle_v3
        tampered_bytes = b"this content was not what was signed"
        result = verify(tampered_bytes, skein_bundle)
        assert result.status == VerifyStatus.SIGNATURE_MISMATCH

    def test_tampered_canonical_bytes_in_bundle_rejected(self, bundle_v3):
        """Bundle's stored canonical_bytes field modified to point to different content.

        An attacker who modifies signature_bundle.canonical_bytes to a different
        base64 value is trying to make the verifier check the signature against
        content it didn't sign. SKEIN must reject this.

        Expected: SIGNATURE_MISMATCH (the ECDSA signature won't verify against
        the attacker-substituted canonical_bytes).
        """
        artifact, skein_bundle = bundle_v3
        tampered = copy.deepcopy(skein_bundle)
        tampered["canonical_bytes"] = base64.b64encode(b"tampered content").decode()
        result = verify(artifact, tampered)
        assert result.status == VerifyStatus.SIGNATURE_MISMATCH


# ===========================================================================
# Section 3: Identity scheme registry conformance
#
# Per Identity rev 5: the identity_scheme field is the dispatch key.
# Unknown values MUST fail-closed (BUNDLE_MALFORMED, non-recoverable).
# sigstore-public-v1 is the only v0 active scheme.
# ===========================================================================


@conformance_staging
class TestIdentitySchemeRegistry:
    """Conformance against the identity_scheme registry rules from Identity rev 5."""

    def test_unknown_scheme_rejected(self, bundle_v3):
        """An unknown identity_scheme ('sigstore-public-v2') must return BUNDLE_MALFORMED.

        Per registry rules: unknown values are treated as unverifiable (fail-closed).
        Applies to future/hypothetical schemes — if the verifier doesn't know the scheme,
        it cannot execute the correct verification procedure. Fail open here would be
        catastrophic (attacker-chosen scheme pointing at attacker-controlled infrastructure).

        Reserved future entry: sigstore-public-v2 (post-PQC migration).
        """
        artifact, skein_bundle = bundle_v3
        unknown = copy.deepcopy(skein_bundle)
        unknown["identity_scheme"] = "sigstore-public-v2"
        result = verify(artifact, unknown)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_missing_scheme_rejected(self, bundle_v3):
        """A signature_bundle with no identity_scheme field must return BUNDLE_MALFORMED.

        The identity_scheme is the dispatch key; without it the verifier cannot
        select the correct verification procedure.
        """
        artifact, skein_bundle = bundle_v3
        no_scheme = copy.deepcopy(skein_bundle)
        del no_scheme["identity_scheme"]
        result = verify(artifact, no_scheme)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_empty_scheme_rejected(self, bundle_v3):
        """identity_scheme='' (empty string) must return BUNDLE_MALFORMED."""
        artifact, skein_bundle = bundle_v3
        empty = copy.deepcopy(skein_bundle)
        empty["identity_scheme"] = ""
        result = verify(artifact, empty)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_legacy_unversioned_scheme_rejected(self, bundle_v3):
        """identity_scheme='sigstore-public' (unversioned, pre-rev5) must return BUNDLE_MALFORMED.

        Rev 5 replaced 'sigstore-public' with 'sigstore-public-v1'. The unversioned
        name is no longer valid; the registry has no entry for it. Fail closed.
        """
        artifact, skein_bundle = bundle_v3
        legacy = copy.deepcopy(skein_bundle)
        legacy["identity_scheme"] = "sigstore-public"
        result = verify(artifact, legacy)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    @conformance_staging
    def test_sigstore_public_v1_accepts_v03_bundle(self, bundle_v3):
        """sigstore-public-v1 scheme correctly dispatches to the v0.3 verification path.

        This is the positive registry path: the active identity_scheme accepts
        the current bundle format (v0.3, Rekor v2).
        """
        artifact, skein_bundle = bundle_v3
        assert skein_bundle["identity_scheme"] == "sigstore-public-v1"
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.VERIFIED

    def test_sigstore_public_v1_rejects_rekor_v1_bundle(self, bundle_no_checkpoint):
        """sigstore-public-v1 rejects a Rekor v1 bundle (SET-only, no checkpoint).

        Registry rule from Identity rev 5: 'sigstore-public-v1 must accept Rekor v2
        bundles and reject Rekor v1 bundles.' The bundle_no_checkpoint fixture is a
        canonical Rekor v1 bundle (has SET, no inclusion proof checkpoint).

        This test is the explicit registry enforcement check. See also
        TestKnownBadBundles.test_no_checkpoint_rejected for the corpus-driven version.

        Expected: BUNDLE_MALFORMED. The Rekor v1 bundle IS rejected — the
        registry rule holds — but sigstore-python 4.2.0 rejects it structurally
        at Bundle.from_json (InvalidBundle), before the inclusion stage. Status
        pin amended from INCLUSION_FAILED per finding-20260519-syos (Option A);
        consistent with test_no_checkpoint_rejected and the locked d3u6 §5.
        """
        artifact, skein_bundle = bundle_no_checkpoint
        assert skein_bundle["identity_scheme"] == "sigstore-public-v1"
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED


# ===========================================================================
# Section 4: Sigstore-bundle ProtoJSON shape conformance
#
# Tests that exercise SKEIN's parse behavior on bundles with structural
# problems: missing fields, extra fields, version mismatches.
# Per qejb finding: ProtoJSON is well-defined for field names but not
# for unknown fields or missing required fields.
# ===========================================================================


class TestProtoJSONShapeConformance:
    """Parse and structure conformance for sigstore-bundle v0.3 ProtoJSON."""

    def test_empty_bundles_array_rejected_at_construction(self, bundle_v3):
        """An empty bundles array raises EmptySignatureBundle at construction time.

        Per clarification 2 (finding-20260511-d3u6): SignatureBundle's field
        validator enforces len(bundles) >= 1. Construction with bundles=[]
        raises EmptySignatureBundle with message
        "SignatureBundle must have at least one signer; got 0." before any
        verify-time logic can run. This is a caller programming error, not a
        domain failure — verify() never observes the empty case.

        Pydantic validators run for both kwarg construction and dict parsing
        (model_validate), so the same exception is raised regardless of whether
        the empty bundles came from a literal or a deserialized payload.
        """
        _artifact, skein_bundle = bundle_v3
        with pytest.raises(EmptySignatureBundle) as exc_info:
            SignatureBundle(
                identity_scheme=skein_bundle["identity_scheme"],
                bundles=[],
                canonical_bytes=base64.b64decode(skein_bundle["canonical_bytes"]),
                canon_version=skein_bundle.get("canon_version", "knurl-1.0"),
            )
        assert str(exc_info.value) == (
            "SignatureBundle must have at least one signer; got 0."
        )

    def test_missing_bundles_field_rejected(self, bundle_v3):
        """A signature_bundle with no 'bundles' key must return BUNDLE_MALFORMED."""
        artifact, skein_bundle = bundle_v3
        no_bundles = copy.deepcopy(skein_bundle)
        del no_bundles["bundles"]
        result = verify(artifact, no_bundles)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_missing_canonical_bytes_rejected(self, bundle_v3):
        """A signature_bundle with no 'canonical_bytes' key must return BUNDLE_MALFORMED.

        canonical_bytes is the source of truth for what was signed (rev 5).
        Without it the verifier cannot perform the ECDSA signature check.
        """
        artifact, skein_bundle = bundle_v3
        no_cb = copy.deepcopy(skein_bundle)
        del no_cb["canonical_bytes"]
        result = verify(artifact, no_cb)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_non_base64_canonical_bytes_rejected(self, bundle_v3):
        """canonical_bytes that is not valid base64 must return BUNDLE_MALFORMED."""
        artifact, skein_bundle = bundle_v3
        bad = copy.deepcopy(skein_bundle)
        bad["canonical_bytes"] = "this is not base64!!!"
        result = verify(artifact, bad)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_malformed_blob_json_rejected(self, bundle_v3):
        """A bundles[] entry that is not parseable JSON must return BUNDLE_MALFORMED.

        Bundle.from_json() will raise; SKEIN must catch and map to BUNDLE_MALFORMED.
        """
        artifact, skein_bundle = bundle_v3
        bad_blob = copy.deepcopy(skein_bundle)
        bad_blob["bundles"] = ["{ this is not json {{"]
        result = verify(artifact, bad_blob)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_extra_top_level_fields_do_not_break_verify(self, bundle_v3):
        """Extra unknown fields on the signature_bundle envelope must be silently ignored.

        Forward-compatibility: a future writer may add fields (e.g. 'witness_cosignature').
        An older verifier must not reject the bundle; it should continue verifying
        the fields it knows about.

        The verification status depends on whether the known-good fields verify:
        for this fixture, the result should be VERIFIED (staging) or at worst
        something other than BUNDLE_MALFORMED.
        """
        artifact, skein_bundle = bundle_v3
        with_extras = copy.deepcopy(skein_bundle)
        with_extras["unknown_future_field"] = "some value"
        with_extras["another_unknown"] = {"nested": True}
        result = verify(artifact, with_extras)
        # Must not reject as BUNDLE_MALFORMED purely due to extra fields.
        # The actual status depends on cryptographic verification (staging TUF root).
        assert result.status != VerifyStatus.BUNDLE_MALFORMED

    def test_extra_blob_fields_do_not_break_verify(self, bundle_v3):
        """Extra JSON fields inside a canonical sigstore-bundle JSON string must not break parsing.

        sigstore-bundle ProtoJSON unknown fields are preserved by proto3 (unknown fields
        are forwarded). SKEIN's call to Bundle.from_json() should handle unknown fields
        per sigstore-python's own behaviour (forward-compatible).

        We add a fake top-level key to the bundle JSON and expect verify to continue.
        The result must not be BUNDLE_MALFORMED due to the extra field alone.
        """
        artifact, skein_bundle = bundle_v3
        blob_with_extra = json.loads(skein_bundle["bundles"][0])
        blob_with_extra["_skein_test_extra"] = "should be ignored"
        modified = copy.deepcopy(skein_bundle)
        modified["bundles"] = [json.dumps(blob_with_extra)]
        result = verify(artifact, modified)
        assert result.status != VerifyStatus.BUNDLE_MALFORMED

    def test_completely_empty_blob_rejected(self, bundle_v3):
        """A bundles[] entry that is valid JSON but structurally empty ({}) must be rejected.

        Bundle.from_json('{}') will either raise or return a bundle missing all required
        fields. SKEIN must map this to BUNDLE_MALFORMED.
        """
        artifact, skein_bundle = bundle_v3
        bad = copy.deepcopy(skein_bundle)
        bad["bundles"] = ["{}"]
        result = verify(artifact, bad)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_v02_bundle_shape_without_checkpoint(self, bundle_no_checkpoint):
        """v0.2 bundle using x509CertificateChain and no inclusionProof.checkpoint.

        This tests the version-dispatch path: SKEIN parses the mediaType and
        determines the bundle is v0.2 / Rekor v1. Per sigstore-public-v1 registry
        rules, this must be rejected (Rekor v1 not accepted under v1 scheme).

        Expected: BUNDLE_MALFORMED. Same corpus and same deterministic path as
        TestKnownBadBundles.test_no_checkpoint_rejected — sigstore-python 4.2.0
        rejects at Bundle.from_json (InvalidBundle) → d3u6 §5 → BUNDLE_MALFORMED,
        marker-independent. Pin tightened from the (INCLUSION_FAILED,
        BUNDLE_MALFORMED) disjunction to exact per finding-20260519-syos: the
        INCLUSION_FAILED branch is dead under the locked library; leaving it
        disjunctive while Option A tightened the other no_checkpoint consumers
        is internally inconsistent. Surfaced by the knuth rerun.

        Corpus file: test/assets/bundle_no_checkpoint.txt.sigstore
        """
        artifact, skein_bundle = bundle_no_checkpoint
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED


# ===========================================================================
# Section 5: Multi-signer bundle conformance
#
# Tests for verify_multi() with bundles[] length > 1.
# The multi-signer case is used by declare-continuity (two signers, same canonical_bytes).
# ===========================================================================


@conformance_staging
class TestMultiSignerConformance:
    """verify_multi() conformance: multiple bundles for the same canonical_bytes."""

    def test_multi_verify_single_bundle_returns_one_result(self, bundle_v3):
        """verify_multi() with a single-element bundles array returns one result.

        This is the common case: single-signer folio with bundles=[one_blob].
        MultiVerifyResult.results must have length 1 and overall == VERIFIED.
        """
        artifact, skein_bundle = bundle_v3
        result = verify_multi(artifact, skein_bundle)
        assert len(result.results) == 1
        assert result.results[0].status == VerifyStatus.VERIFIED
        assert result.overall == VerifyStatus.VERIFIED

    def test_multi_verify_two_good_bundles_both_verified(
        self, bundle_v3, bundle_v3_alt
    ):
        """Two distinct signers of the same canonical_bytes — both must verify.

        This is the declare-continuity case: two separate sign() calls produce two
        canonical sigstore-bundle JSON strings, both signing the same canonical_bytes.
        SKEIN stores them in bundles[0] and bundles[1]. verify_multi() verifies each
        independently.

        Implementation note: bundle_v3 and bundle_v3_alt are signed by the same
        person (william@yossarian.net) against different Rekor log indices. For this
        test we treat them as two distinct signers signing the same content (they
        sign the same artifact bytes, so canonical_bytes match both).
        """
        artifact_v3, skein_bundle_v3 = bundle_v3
        _artifact_alt, skein_bundle_alt = bundle_v3_alt
        # Both fixtures sign the same canonical content (bundle_v3 and bundle_v3_alt
        # sign different artifacts in the corpus, but here we reuse bundle_v3's artifact
        # for both to simulate two signers on the same canonical_bytes).
        # Only the blob differs (different Rekor entry, different ephemeral key).
        two_signer_bundle = {
            "identity_scheme": "sigstore-public-v1",
            "bundles": [
                skein_bundle_v3["bundles"][0],
                skein_bundle_alt["bundles"][0],
            ],
            "canonical_bytes": skein_bundle_v3["canonical_bytes"],
            "canon_version": "knurl-1.0",
        }
        result = verify_multi(artifact_v3, two_signer_bundle)
        assert len(result.results) == 2
        # bundle_v3 verifies (correct canonical_bytes + correct blob)
        assert result.results[0].status == VerifyStatus.VERIFIED
        # bundle_v3_alt has a different artifact → SIGNATURE_MISMATCH for the alt signer
        # (the blob was signed against different content; not a valid co-signer here)
        assert result.results[1].status == VerifyStatus.SIGNATURE_MISMATCH
        # overall: not VERIFIED because one signer failed
        assert result.overall != VerifyStatus.VERIFIED

    def test_multi_verify_partial_failure_sets_overall_not_verified(self, bundle_v3):
        """If one bundle in a multi-signer array fails, overall must not be VERIFIED.

        Federation and authorization checks depend on overall == VERIFIED meaning
        ALL signers passed. A partial pass is not VERIFIED at the overall level.
        """
        artifact, skein_bundle = bundle_v3
        bad_blob = json.loads(skein_bundle["bundles"][0])
        bad_blob["messageSignature"]["signature"] = "AAAA"  # corrupt signature
        two_signer = copy.deepcopy(skein_bundle)
        two_signer["bundles"] = [
            skein_bundle["bundles"][0],
            json.dumps(bad_blob),
        ]
        result = verify_multi(artifact, two_signer)
        assert len(result.results) == 2
        assert result.results[0].status == VerifyStatus.VERIFIED
        assert result.results[1].status != VerifyStatus.VERIFIED
        assert result.overall != VerifyStatus.VERIFIED

    def test_multi_verify_all_fail_overall_not_verified(self, bundle_no_log_entry):
        """All-fail multi-bundle: overall must not be VERIFIED."""
        artifact, skein_bundle = bundle_no_log_entry
        two_fail_bundle = {
            "identity_scheme": "sigstore-public-v1",
            "bundles": [
                skein_bundle["bundles"][0],
                skein_bundle["bundles"][0],
            ],
            "canonical_bytes": skein_bundle["canonical_bytes"],
            "canon_version": "knurl-1.0",
        }
        result = verify_multi(artifact, two_fail_bundle)
        assert all(r.status != VerifyStatus.VERIFIED for r in result.results)
        assert result.overall != VerifyStatus.VERIFIED


# ===========================================================================
# Section 6: Portability round-trip
#
# This is the cross-implementation portability sanity check.
# Goal: a bundle that SKEIN's verify() accepts must also be accepted by
# sigstore-python's own Verifier directly — same data, independent call.
# If these disagree, SKEIN has a portability bug.
# ===========================================================================


@conformance_staging
class TestPortabilityRoundTrip:
    """Portability sanity check: SKEIN verify and sigstore-python verify agree."""

    def test_skein_and_sigstore_python_agree_on_good_bundle_via_corpus(self, bundle_v3):
        """A bundle SKEIN verifies must also verify via sigstore-python's Verifier directly.

        This is the portability gate. If SKEIN returns VERIFIED but sigstore-python
        raises VerificationError on the same blob, SKEIN has accepted something the
        ecosystem would reject — a portability bug.

        We verify the same data via two independent paths:
          1. SKEIN's verify(canonical_bytes, skein_bundle)
          2. sigstore-python's Verifier.staging(offline=True).verify_artifact(...)
        Both must succeed.

        Mock strategy: No mock needed. The corpus bundle is a real staging bundle.
        sigstore-python ships its staging TUF root; offline=True avoids network calls.
        """
        from sigstore.errors import VerificationError
        from sigstore.models import Bundle
        from sigstore.verify import policy as sigstore_policy
        from sigstore.verify.verifier import Verifier

        artifact, skein_bundle = bundle_v3
        blob = skein_bundle["bundles"][0]

        # Path 1: SKEIN verify
        skein_result = verify(artifact, skein_bundle)
        assert skein_result.status == VerifyStatus.VERIFIED

        # Path 2: sigstore-python direct verify (same blob, same artifact)
        verifier = Verifier.staging(offline=True)
        bundle = Bundle.from_json(blob)
        try:
            verifier.verify_artifact(artifact, bundle, sigstore_policy.UnsafeNoOp())
            sigstore_verified = True
        except VerificationError:
            sigstore_verified = False

        assert sigstore_verified, (
            "SKEIN returned VERIFIED but sigstore-python raised VerificationError "
            "on the same bundle blob. This is a portability regression."
        )

    def test_skein_and_sigstore_python_agree_on_cve_bundle_via_corpus(
        self, bundle_cve_2022_36056
    ):
        """CVE bundle: SKEIN and sigstore-python must both reject it.

        If SKEIN returns INCLUSION_FAILED but sigstore-python would accept the same
        bundle, SKEIN is being MORE strict than the ecosystem — still correct but
        potentially surprising. More critically: if SKEIN accepts but sigstore-python
        rejects, that's the dangerous asymmetry.

        This test checks that both reject.
        """
        from sigstore.errors import VerificationError
        from sigstore.models import Bundle
        from sigstore.verify import policy as sigstore_policy
        from sigstore.verify.verifier import Verifier

        artifact, skein_bundle = bundle_cve_2022_36056
        blob = skein_bundle["bundles"][0]

        # Path 1: SKEIN verify — must not be VERIFIED
        skein_result = verify(artifact, skein_bundle)
        assert skein_result.status != VerifyStatus.VERIFIED, (
            "SKEIN accepted the CVE-2022-36056 bundle — this is a security regression."
        )

        # Path 2: sigstore-python must also reject
        verifier = Verifier.staging(offline=True)
        bundle = Bundle.from_json(blob)
        with pytest.raises(VerificationError):
            verifier.verify_artifact(artifact, bundle, sigstore_policy.UnsafeNoOp())

    def test_bundle_json_round_trip_does_not_corrupt(self, bundle_v3):
        """Bundle stored then reloaded (JSON parse → serialize → reparse) verifies identically.

        sigstore-bundle ProtoJSON is NOT byte-canonical (protobuf.dev). Re-serializing
        may produce different whitespace/key ordering. SKEIN's verify must work on
        the re-serialized form. This tests that SKEIN's storage path (load blob from DB,
        pass to Bundle.from_json, pass to verifier) does not depend on byte identity.

        Critical: canonicalizedBody inside tlogEntries MUST NOT be re-encoded.
        This test validates SKEIN's handling, not byte equivalence.
        """
        from sigstore.models import Bundle

        artifact, skein_bundle = bundle_v3
        blob = skein_bundle["bundles"][0]

        # Round-trip the blob: parse → re-serialize
        bundle_obj = Bundle.from_json(blob)
        reserialized_blob = bundle_obj.to_json()

        # Wrap the re-serialized blob in a new SKEIN bundle
        rt_skein_bundle = copy.deepcopy(skein_bundle)
        rt_skein_bundle["bundles"] = [reserialized_blob]

        # SKEIN must verify the re-serialized bundle
        result = verify(artifact, rt_skein_bundle)
        assert result.status == VerifyStatus.VERIFIED, (
            "SKEIN failed to verify a bundle that was round-tripped through "
            "Bundle.from_json → to_json. The stored blob may be byte-compared "
            "incorrectly instead of being treated as an opaque JSON string for storage."
        )

    @pytest.mark.skip(reason="requires OIDC: set SKEIN_TEST_OIDC=1 and provide token")
    def test_skein_sign_produces_sigstore_verifiable_bundle(self):
        """SKEIN.sign() produces a bundle JSON that sigstore-python's Verifier accepts.

        This is the true end-to-end round-trip: sign via SKEIN → verify via
        sigstore-python. Requires a real OIDC token (ambient GHA credential,
        or one acquired out-of-band by the caller).

        Per clarification 1 + 3 (finding-20260511-d3u6):
          - sign(canonical_bytes, OIDCProviderConfig) -> SignResult
          - SignResult.bundle_json carries the canonical sigstore-bundle
            ProtoJSON string for one signer.

        Mock strategy for CI: provide a pre-acquired OIDC JWT via env var
        SKEIN_TEST_OIDC_TOKEN. When present, skip the browser OIDC flow.

        Enable: SKEIN_TEST_OIDC=1 pytest tests/conformance/ -m conformance_staging -k round_trip
        """
        import os
        from sigstore.errors import VerificationError
        from sigstore.models import Bundle
        from sigstore.verify import policy as sigstore_policy
        from sigstore.verify.verifier import Verifier
        from skein.signing import (  # type: ignore[import]
            OIDCProviderConfig,
            SignResult,
            sign,
        )

        raw_token = os.environ.get("SKEIN_TEST_OIDC_TOKEN")
        if not raw_token:
            pytest.skip("SKEIN_TEST_OIDC_TOKEN not set")

        canonical_bytes = b"SKEIN round-trip conformance test artifact"

        # Per clarification 3: OIDCProviderConfig is a 4-field Pydantic model.
        # issuer + token required; provider_id + expires_at optional.
        provider = OIDCProviderConfig(
            issuer="https://github.com/login/oauth",
            token=raw_token,
            provider_id="github",
        )

        # SKEIN sign returns a SignResult; the wire bundle JSON is in .bundle_json.
        result = sign(canonical_bytes, provider)
        assert isinstance(result, SignResult), (
            "sign() must return a SignResult per clarification 1"
        )
        assert isinstance(result.bundle_json, str), (
            "SignResult.bundle_json must be a canonical sigstore-bundle JSON string"
        )

        # sigstore-python should accept the bundle JSON SKEIN produced
        verifier = Verifier.staging(offline=True)
        bundle = Bundle.from_json(result.bundle_json)
        try:
            verifier.verify_artifact(
                canonical_bytes, bundle, sigstore_policy.UnsafeNoOp()
            )
        except VerificationError as e:
            pytest.fail(
                f"sigstore-python rejected a bundle produced by SKEIN.sign(): {e}\n"
                "This is a portability bug: SKEIN is producing bundles the ecosystem rejects."
            )


# ===========================================================================
# Section 7: VerifyResult structure conformance
#
# These tests verify the shape of VerifyResult returned by verify().
# The return type is a Pydantic model with specific fields per rev 5 spec.
# ===========================================================================


@conformance_staging
class TestVerifyResultStructure:
    """VerifyResult Pydantic model shape conformance."""

    def test_verified_result_has_issuer_and_subject(self, bundle_v3):
        """A VERIFIED result must carry non-None issuer and subject.

        Per rev 5: verify() must extract (issuer, subject) from the Fulcio cert.
        The issuer is the OIDC issuer URL (not a shortname like 'github').
        The subject is the cert SAN (email or URI form).
        """
        artifact, skein_bundle = bundle_v3
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.VERIFIED
        assert result.issuer is not None
        assert result.subject is not None
        # Issuer must be a URL, not a shortname
        assert result.issuer.startswith("https://"), (
            f"Issuer should be an OIDC URL, got: {result.issuer!r}. "
            "account_binding stores issuer URL, not shortname (rev 5 requirement)."
        )

    def test_failed_result_has_no_identity(self, bundle_cve_2022_36056):
        """A failed result (non-VERIFIED) must have None issuer and subject.

        When verification fails, no identity has been cryptographically confirmed.
        Returning an identity from a failed verification would be misleading.
        """
        artifact, skein_bundle = bundle_cve_2022_36056
        result = verify(artifact, skein_bundle)
        assert result.status != VerifyStatus.VERIFIED
        assert result.issuer is None
        assert result.subject is None

    def test_verified_result_has_evidence(self, bundle_v3):
        """A VERIFIED result should carry Evidence with at minimum a rekor_inclusion field.

        Evidence allows the caller to inspect what was verified without re-running verify.
        """
        artifact, skein_bundle = bundle_v3
        result = verify(artifact, skein_bundle)
        assert result.status == VerifyStatus.VERIFIED
        assert result.evidence is not None

    def test_result_is_pydantic_model(self, bundle_v3):
        """VerifyResult must be a Pydantic BaseModel instance.

        Callers can use .model_dump(), .model_json_schema(), etc. for serialization.
        This enforces the contract from rev 5's spec.
        """
        artifact, skein_bundle = bundle_v3
        result = verify(artifact, skein_bundle)
        # Should have Pydantic model_dump method
        assert hasattr(result, "model_dump"), (
            "VerifyResult must be a Pydantic BaseModel (has model_dump)"
        )
        dumped = result.model_dump()
        assert "status" in dumped
        assert "issuer" in dumped
        assert "subject" in dumped


# ===========================================================================
# Section 8: Signature verification edge cases
# ===========================================================================


@conformance_staging
class TestSignatureEdgeCases:
    """Signature mismatch and structural edge cases not in the main corpus."""

    def test_corrupted_signature_bytes_rejected(self, bundle_v3):
        """A canonical sigstore-bundle JSON string with corrupted messageSignature.signature bytes.

        Replace the signature value with valid base64 of random bytes — correct
        encoding, wrong content. The ECDSA verification must fail.
        Expected: SIGNATURE_MISMATCH.
        """
        artifact, skein_bundle = bundle_v3
        bad_blob = json.loads(skein_bundle["bundles"][0])
        # Replace signature with 64 random bytes (valid base64, wrong ECDSA sig)
        bad_blob["messageSignature"]["signature"] = base64.b64encode(
            b"\x00" * 64
        ).decode()
        corrupt = copy.deepcopy(skein_bundle)
        corrupt["bundles"] = [json.dumps(bad_blob)]
        result = verify(artifact, corrupt)
        assert result.status == VerifyStatus.SIGNATURE_MISMATCH

    def test_multiple_tlog_entries_handled(self, bundle_v3):
        """A bundle with two tlogEntries (duplicate) must be handled without crash.

        Expected: BUNDLE_MALFORMED, deterministically. sigstore-python 4.2.0
        Bundle.from_json requires exactly one log entry and raises InvalidBundle
        ("expected exactly one log entry in bundle") on a multi-entry bundle,
        before any verifier → d3u6 §5 → BUNDLE_MALFORMED, marker-independent.
        Pin tightened from the (VERIFIED, BUNDLE_MALFORMED) robustness
        disjunction to exact per finding-20260519-74o7: the VERIFIED branch is
        dead under the locked library (verified by direct probe). The intent
        (must not crash; well-defined status) is still enforced — by an exact
        pin rather than an either-or.
        """
        artifact, skein_bundle = bundle_v3
        blob = json.loads(skein_bundle["bundles"][0])
        entries = blob["verificationMaterial"]["tlogEntries"]
        blob["verificationMaterial"]["tlogEntries"] = entries + entries  # duplicate
        dup = copy.deepcopy(skein_bundle)
        dup["bundles"] = [json.dumps(blob)]
        result = verify(artifact, dup)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED

    def test_dsse_envelope_bundle_rejected_as_bundle_malformed(self, bundle_v3):
        """A bundle using dsseEnvelope instead of messageSignature must be rejected.

        SKEIN's sign() produces messageSignature bundles (signing over canonical_bytes).
        DSSE envelopes are for in-toto statements (sign_dsse path in sigstore-python).
        SKEIN has no DSSE consumer; a DSSE bundle presented to verify() is malformed
        for SKEIN's verification model.

        Replace the messageSignature field with a fake dsseEnvelope to simulate.
        """
        artifact, skein_bundle = bundle_v3
        blob = json.loads(skein_bundle["bundles"][0])
        msg_sig = blob.pop("messageSignature")
        blob["dsseEnvelope"] = {
            "payload": base64.b64encode(b"fake payload").decode(),
            "payloadType": "application/vnd.in-toto+json",
            "signatures": [{"sig": msg_sig.get("signature", ""), "keyid": ""}],
        }
        dsse = copy.deepcopy(skein_bundle)
        dsse["bundles"] = [json.dumps(blob)]
        result = verify(artifact, dsse)
        assert result.status == VerifyStatus.BUNDLE_MALFORMED
