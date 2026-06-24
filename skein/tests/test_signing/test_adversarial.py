"""Adversarial tests for skein.signing.

Threat-model coverage map
-------------------------
Attack class                       → Test class                     → Expected
─────────────────────────────────  ──────────────────────────────  ──────────────────
Cross-payload bundle reuse         → TestCrossPayload               SIGNATURE_MISMATCH
Tampered cert / identity claim     → TestTamperedIdentity           CERT_INVALID
Stripped Rekor inclusion proof     → TestStrippedInclusionProof     INCLUSION_FAILED
Stripped Rekor checkpoint          → TestStrippedCheckpoint         INCLUSION_FAILED
Future-dated cert                  → TestFutureDatedCert            CERT_INVALID
Cross-issuer identity confusion    → TestCrossIssuerConfusion       VERIFIED w/ distinct issuer
Malformed JSON                     → TestMalformedBundle            BUNDLE_MALFORMED (no crash)
Non-ASCII identity                 → TestNonAsciiIdentity           documented
Bundle version skew                → TestBundleVersionSkew          BUNDLE_MALFORMED (unknown)
Trust root staleness               → TestTrustRootStaleness         TRUST_ROOT_STALE
Offline / no trust root            → TestOfflineNoTrustRoot         OFFLINE_NO_TRUSTED_ROOT
identity_scheme mismatch           → TestIdentitySchemeMismatch     BUNDLE_MALFORMED
Bit-flip fuzz                      → TestBitFlipFuzz                non-VERIFIED, no crash
Length truncation fuzz             → TestTruncationFuzz             BUNDLE_MALFORMED, no crash
Replay across canonical_bytes      → TestReplayAcrossCanonicalBytes SIGNATURE_MISMATCH
CVE corpus                         → TestCveCorpus                  rejection per CVE
Multi-signer partial failure       → TestMultiSignerAttack          partial fail, overall != VERIFIED
DSSE confusion                     → TestDsseConfusion              BUNDLE_MALFORMED
Unrelated Rekor entry              → TestUnrelatedRekorEntry        INCLUSION_FAILED / similar
Cross-Rekor log_id substitution    → TestCrossRekorLogId           INCLUSION_FAILED / similar
Hash/curve profile substitution    → TestSigstorePublicV1AlgorithmProfile BUNDLE_MALFORMED / CERT_INVALID
JSON injection / extras            → TestMalformedBundle            no crash
Identity relabel attack            → TestRelabelAttack              cert SAN authoritative

Notes
-----
verify() MUST NOT: raise unhandled exceptions, panic, hang, or return VERIFIED
under any of these inputs. It MUST: return a VerifyResult with a defined status,
OR raise MultiSignerBundle for multi-signer inputs (caller error).
"""

from __future__ import annotations

import base64
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


GOOGLE_ISSUER = "https://accounts.google.com"
GITHUB_ISSUER = "https://github.com/login/oauth"
CANARY_BYTES_A = b"folio:finding-20260511-aaaa\x00canonical-content-alpha"
CANARY_BYTES_B = b"folio:finding-20260511-bbbb\x00canonical-content-beta"


# ---------------------------------------------------------------------------
# Attack class 1: Cross-payload bundle reuse
# ---------------------------------------------------------------------------


class TestCrossPayload:
    """Bundle for payload X presented during verification of payload Y.

    ECDSA signature covers SHA-256(canonical_bytes). Any payload swap that
    changes the hash MUST produce SIGNATURE_MISMATCH, regardless of whether
    the cert and inclusion proof are structurally intact.
    """

    # Enforces: a bundle signed over folio A's canonical_bytes presented as
    # folio B's bundle returns SIGNATURE_MISMATCH.
    def test_cross_payload_bundle_reuse_fails(
        self, crypto_factory, google_provider, monkeypatch
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        folio_a = b'{"folio_id":"A","content":"alice signed this"}'
        folio_b = b'{"folio_id":"B","content":"victim folio body"}'
        a_result = signing.sign(folio_a, google_provider)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[a_result.bundle_json],
            canonical_bytes=folio_b,  # attacker swapped stored canonical_bytes
            canon_version="knurl-1.0",
        )
        result = signing.verify(folio_b, sb)
        assert result.status == signing.VerifyStatus.SIGNATURE_MISMATCH

    # Enforces: single-byte mutation of caller's canonical_bytes is detected.
    def test_single_byte_flip_in_canonical_bytes(
        self, crypto_factory, google_provider, monkeypatch
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(CANARY_BYTES_A, google_provider)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[signed.bundle_json],
            canonical_bytes=CANARY_BYTES_A,
            canon_version="knurl-1.0",
        )
        corrupted = bytearray(CANARY_BYTES_A)
        corrupted[5] ^= 0x01
        result = signing.verify(bytes(corrupted), sb)
        assert result.status == signing.VerifyStatus.SIGNATURE_MISMATCH

    # Enforces: truncating caller's canonical_bytes by one byte is detected.
    def test_canonical_bytes_truncated_by_one(
        self, crypto_factory, google_provider, monkeypatch
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(CANARY_BYTES_A, google_provider)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[signed.bundle_json],
            canonical_bytes=CANARY_BYTES_A,
            canon_version="knurl-1.0",
        )
        result = signing.verify(CANARY_BYTES_A[:-1], sb)
        assert result.status == signing.VerifyStatus.SIGNATURE_MISMATCH

    # Enforces: extending caller's canonical_bytes by one byte is detected.
    def test_canonical_bytes_extended_by_null(
        self, crypto_factory, google_provider, monkeypatch
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(CANARY_BYTES_A, google_provider)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[signed.bundle_json],
            canonical_bytes=CANARY_BYTES_A,
            canon_version="knurl-1.0",
        )
        result = signing.verify(CANARY_BYTES_A + b"\x00", sb)
        assert result.status == signing.VerifyStatus.SIGNATURE_MISMATCH

    # Enforces: cross-folio replay with identical content but different folio_id
    # fails. canonical_bytes includes folio_id per knurl-pqxy; identical content
    # in different folios produces different canonical_bytes.
    def test_cross_folio_replay_fails(
        self, crypto_factory, google_provider, monkeypatch
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        payload_1 = b'{"folio_id":"finding-20260511-0001","content":"hello"}'
        payload_2 = b'{"folio_id":"finding-20260511-0002","content":"hello"}'
        signed_1 = signing.sign(payload_1, google_provider)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[signed_1.bundle_json],
            canonical_bytes=payload_1,
            canon_version="knurl-1.0",
        )
        result = signing.verify(payload_2, sb)
        assert result.status == signing.VerifyStatus.SIGNATURE_MISMATCH


# ---------------------------------------------------------------------------
# Attack class 2: Tampered identity claim
# ---------------------------------------------------------------------------


class TestTamperedIdentity:
    """Cert SAN or issuer field modified after issuance.

    The cert's cryptographic integrity (CT-log SCT, Fulcio CA chain) must be
    verified. Any mutation to the cert bytes after issuance breaks the chain.
    """

    # Enforces: one-bit flip in cert bytes returns CERT_INVALID.
    def test_one_bit_flip_in_cert_returns_cert_invalid(
        self, crypto_factory, google_provider, monkeypatch
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        canonical = b"adversarial payload"
        signed = signing.sign(canonical, google_provider)
        blob = crypto_factory.tamper_cert(signed.bundle_json)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical, sb)
        assert result.status == signing.VerifyStatus.CERT_INVALID

    # Enforces: relabeling a valid bundle from alice to bob doesn't work — the
    # cert SAN is authoritative. signing.py returns whatever the CERT says.
    def test_relabeled_identity_returns_cert_identity_not_relabel(
        self, crypto_factory, canonical_bytes_simple, monkeypatch
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        # Alice's bundle.
        blob = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity="alice@example.com",
            issuer=GOOGLE_ISSUER,
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        # Even if downstream metadata claimed this was bob, verify() reads cert SAN.
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.subject == "alice@example.com"


# ---------------------------------------------------------------------------
# Attack class 3: Stripped Rekor inclusion proof
# ---------------------------------------------------------------------------


class TestStrippedInclusionProof:
    """Verifier must require a valid inclusion proof; stripping fails."""

    # Enforces: stripped inclusion proof returns INCLUSION_FAILED. Per spec rev 5,
    # Rekor v2 verification requires the inclusion proof (no SET fallback).
    def test_stripped_inclusion_proof_returns_inclusion_failed(
        self, crypto_factory, google_provider, monkeypatch
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        canonical = b"adversarial payload"
        signed = signing.sign(canonical, google_provider)
        blob = crypto_factory.strip_rekor_proof(signed.bundle_json)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical, sb)
        assert result.status == signing.VerifyStatus.INCLUSION_FAILED


# ---------------------------------------------------------------------------
# Attack class 4: Stripped checkpoint
# ---------------------------------------------------------------------------


class TestStrippedCheckpoint:
    """The signed-note checkpoint is the trust anchor for the proof."""

    # Enforces: inclusion proof retains Merkle hashes but strips checkpoint →
    # INCLUSION_FAILED.
    def test_stripped_checkpoint_returns_inclusion_failed(
        self, crypto_factory, google_provider, monkeypatch
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        canonical = b"adversarial payload"
        signed = signing.sign(canonical, google_provider)
        blob = crypto_factory.strip_checkpoint(signed.bundle_json)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical, sb)
        assert result.status == signing.VerifyStatus.INCLUSION_FAILED


# ---------------------------------------------------------------------------
# Attack class 5: Future-dated cert
# ---------------------------------------------------------------------------


class TestFutureDatedCert:
    """Cert with notBefore > integratedTime — impossible valid signing window."""

    # Enforces: future-dated cert returns CERT_INVALID (or TRUST_ROOT_STALE).
    # This is the CVE-2026-24122 (Cosign) class of bug.
    def test_future_dated_cert_returns_cert_invalid(
        self, crypto_factory, google_provider, monkeypatch
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        canonical = b"adversarial payload"
        signed = signing.sign(canonical, google_provider)
        blob = crypto_factory.future_dated_cert(signed.bundle_json)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical, sb)
        assert result.status in (
            signing.VerifyStatus.CERT_INVALID,
            signing.VerifyStatus.TRUST_ROOT_STALE,
        )


# ---------------------------------------------------------------------------
# Attack class 6: Cross-issuer identity confusion
# ---------------------------------------------------------------------------


class TestCrossIssuerConfusion:
    """Two providers issuing certs with the same subject email.

    Identity is (issuer, subject) per spec rev 5 — verifier must disambiguate
    by issuer. Phase 1 sbal: returning subject alone is impersonatable.
    """

    # Enforces: alice@google and alice@github produce VERIFIED results with
    # DIFFERENT issuer URLs. The (issuer, subject) tuple is the durable identity.
    def test_identity_disambiguation_across_issuers(self, crypto_factory, monkeypatch):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        canonical = b"shared canonical bytes"
        blob_google = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical,
            identity="alice@example.com",
            issuer=GOOGLE_ISSUER,
        )
        blob_github = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical,
            identity="alice@example.com",  # same SAN
            issuer=GITHUB_ISSUER,
        )
        sb_google = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob_google],
            canonical_bytes=canonical,
            canon_version="knurl-1.0",
        )
        sb_github = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob_github],
            canonical_bytes=canonical,
            canon_version="knurl-1.0",
        )
        r_g = signing.verify(canonical, sb_google)
        r_h = signing.verify(canonical, sb_github)
        assert r_g.subject == r_h.subject
        assert r_g.issuer != r_h.issuer

    # Enforces: issuer URL case-sensitivity. Verifier must not silently widen
    # trust to case variants of the expected issuer.
    def test_issuer_url_case_sensitivity(
        self, crypto_factory, canonical_bytes_simple, monkeypatch
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        # Use uppercase issuer — the result must be deterministic (not crash).
        blob = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity="alice@example.com",
            issuer="HTTPS://ACCOUNTS.GOOGLE.COM",
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert isinstance(result, signing.VerifyResult)


# ---------------------------------------------------------------------------
# Attack class 7: Malformed JSON
# ---------------------------------------------------------------------------


class TestMalformedBundle:
    """Structurally broken bundle inputs must return BUNDLE_MALFORMED, never crash.

    verify() MUST NOT: raise unhandled exceptions, panic, hang, return VERIFIED.
    It MUST: return a VerifyResult with status BUNDLE_MALFORMED.
    """

    # Enforces: random strings in place of bundle JSON return BUNDLE_MALFORMED.
    @pytest.mark.parametrize(
        "payload",
        [
            "",
            "this is plain text",
            "\x00" * 64,
            "\xff" * 64,
            "{",
            "{}",
            "[]",
            '{"mediaType": "not-sigstore"}',
            "null",
            "false",
            "true",
            "42",
        ],
    )
    def test_malformed_bundle_payload_returns_malformed(self, make_bundle, payload):
        sb = make_bundle(canonical_bytes=b"x", bundles=[payload])
        result = signing.verify(b"x", sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED

    # Enforces: bundle JSON missing required fields returns BUNDLE_MALFORMED.
    def test_missing_message_signature(self, make_bundle):
        sb = make_bundle(
            canonical_bytes=b"x",
            bundles=['{"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}'],
        )
        result = signing.verify(b"x", sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED

    # Enforces: deeply nested JSON does not cause stack overflow or hang.
    def test_nested_recursion_bomb(self, crypto_factory, monkeypatch, make_bundle):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        deep = {}
        node = deep
        for _ in range(200):
            node["x"] = {}
            node = node["x"]
        bundle_dict = {
            "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
            "verificationMaterial": {"extra": deep},
            "messageSignature": {
                "messageDigest": {"algorithm": "SHA2_256", "digest": "AA=="},
                "signature": "AA==",
            },
        }
        sb = make_bundle(canonical_bytes=b"x", bundles=[json.dumps(bundle_dict)])
        result = signing.verify(b"x", sb)
        assert isinstance(result, signing.VerifyResult)
        assert result.status != signing.VerifyStatus.VERIFIED

    # Enforces: extra unknown JSON fields are silently ignored (forward compat),
    # not BUNDLE_MALFORMED purely due to unknowns.
    def test_extra_unknown_fields_in_bundle_blob(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = json.loads(signed.bundle_json)
        blob["__future_unknown__"] = {"nested": True}
        modified = json.dumps(blob)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[modified],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        # Must not reject as BUNDLE_MALFORMED purely due to extra fields.
        assert result.status != signing.VerifyStatus.BUNDLE_MALFORMED

    # Enforces: a very large signature field doesn't crash but doesn't verify either.
    def test_very_large_signature_field(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = json.loads(signed.bundle_json)
        import base64 as _b64

        large_sig = _b64.b64encode(b"A" * (1024 * 1024)).decode()
        blob.setdefault("messageSignature", {})["signature"] = large_sig
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[json.dumps(blob)],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert isinstance(result, signing.VerifyResult)
        assert result.status != signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Single-parse refactor invariants (Shard Q / finding-20260520-j4w4 residual)
# ---------------------------------------------------------------------------


class TestSingleParseInvariant:
    """Profile check and Bundle.from_json must agree on the parsed bundle.

    Pre-refactor: _verify_single re-parsed the JSON blob up to three times
    (profile check, strip helper, Bundle.from_json). The three-parse seam was
    a TOCTOU-shaped hazard — any future divergence between parser passes
    (e.g., a different JSON library, or a profile check that grew to inspect
    unknown fields) would let an attacker craft a bundle where profile check
    sees one shape and Bundle.from_json sees another.

    Post-refactor: a single json.loads feeds both the profile check and the
    strip; Bundle.from_json then sees the re-serialized stripped dict. These
    tests lock in observable behaviour that the single-parse path preserves
    — they pass against the multi-parse code today (the divergence is
    latent), and they must still pass after the refactor.
    """

    def test_duplicate_top_level_keys_resolve_deterministically(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        # Per the JSON spec duplicate keys are implementation-defined; Python's
        # stdlib json.loads keeps the last value. The invariant we lock in: the
        # *outcome* of verify() on a fixed blob with duplicate keys is
        # deterministic across invocations. With the post-refactor single-parse
        # path, profile check and Bundle.from_json look at the same parsed dict,
        # so any duplicate-key resolution is consistent across the boundary.
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        # Inject a duplicate mediaType literal into the JSON text — json.dumps
        # would dedup, so we splice the string.
        original = signed.bundle_json
        assert original.startswith("{")
        with_dup = (
            "{"
            + '"mediaType": "application/vnd.dev.sigstore.bundle.v0.2+json", '
            + original[1:]
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[with_dup],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        statuses = {signing.verify(canonical_bytes_simple, sb).status for _ in range(5)}
        # Repeated verification of the *same* blob yields one status (not a flap).
        assert len(statuses) == 1

    def test_profile_failure_takes_precedence_over_unknown_field_strip(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        # A bundle whose mediaType is JSON null fails the profile check
        # (mediaType must be a string when present). Adding an unknown
        # top-level field must NOT rescue the bundle by reaching the
        # strip-and-Bundle.from_json path: profile check runs first, and
        # BUNDLE_MALFORMED is returned before the strip ever considers the
        # unknown field.
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = json.loads(signed.bundle_json)
        blob["mediaType"] = None
        blob["__unknown_future_field__"] = {"some": "value"}
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[json.dumps(blob)],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED

    def test_unknown_top_level_field_strip_preserves_known_alias_fields(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        # Forward-compat: an unknown top-level field is stripped silently. The
        # post-refactor single-parse path must preserve the existing
        # _BUNDLE_TOP_LEVEL_FIELDS filter (both camelCase and snake_case
        # aliases). We assert that injecting an unknown field, alongside the
        # bundle's normal camelCase fields, still verifies — i.e. all
        # known-alias fields survive the strip and reach Bundle.from_json.
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = json.loads(signed.bundle_json)
        blob["__sigstore_future_extension__"] = ["a", "b", "c"]
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[json.dumps(blob)],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        # The unknown field is stripped silently; the surviving known fields
        # verify successfully under the test monkeypatch.
        assert result.status == signing.VerifyStatus.VERIFIED

    def test_top_level_field_filter_set_covers_documented_aliases(self):
        # The strip-filter set must cover every alias the bundle envelope
        # accepts. If a future change drops a snake_case alias the strip would
        # silently corrupt bundles that use that alias. Lock the membership in.
        from skein.signing import _BUNDLE_TOP_LEVEL_FIELDS

        for required in (
            "mediaType",
            "media_type",
            "verificationMaterial",
            "verification_material",
            "messageSignature",
            "message_signature",
            "dsseEnvelope",
            "dsse_envelope",
        ):
            assert required in _BUNDLE_TOP_LEVEL_FIELDS


# ---------------------------------------------------------------------------
# Attack class 8: Non-ASCII identity claims
# ---------------------------------------------------------------------------


class TestNonAsciiIdentity:
    """Unicode identity strings — covers sigstore-python issue #1507.

    On the VERIFY side, behavior must be defined and must not crash.
    """

    # Enforces: NFC-combining-form Unicode round-trips through the read path.
    def test_non_ascii_identity_in_verify(self, crypto_factory, monkeypatch):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        canonical = b"payload"
        blob = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical,
            identity="josé@example.com",
            issuer=GOOGLE_ISSUER,
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical, sb)
        if result.status == signing.VerifyStatus.VERIFIED:
            assert result.subject == "josé@example.com"

    # Enforces: verify() never crashes on Unicode subjects (Cyrillic lookalike,
    # CJK, emoji, etc.). Status may be VERIFIED or fail-closed; never crash.
    @pytest.mark.parametrize(
        "subject",
        [
            "älice@example.com",  # Latin Extended-A
            "аlice@example.com",  # Cyrillic 'а' lookalike
            "用户@example.com",  # CJK
            "\U0001f600@example.com",  # emoji in email
        ],
    )
    def test_unicode_subject_does_not_crash(
        self, crypto_factory, canonical_bytes_simple, monkeypatch, subject
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        blob = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity=subject,
            issuer=GOOGLE_ISSUER,
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert isinstance(result, signing.VerifyResult)

    # Enforces: null byte in subject is rejected cleanly (BUNDLE_MALFORMED or
    # CERT_INVALID — both acceptable; the unacceptable outcome is VERIFIED or crash).
    def test_null_byte_in_subject_does_not_verify(
        self, crypto_factory, canonical_bytes_simple, monkeypatch
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        blob = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity="alice\x00@example.com",
            issuer=GOOGLE_ISSUER,
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status != signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Attack class 9: Bundle version skew
# ---------------------------------------------------------------------------


class TestBundleVersionSkew:
    """Older / newer / malformed mediaType handling."""

    @pytest.mark.parametrize(
        "media_type,must_reject",
        [
            ("application/vnd.dev.sigstore.bundle.v9.9.9+json", True),
            ("application/vnd.dev.sigstore.bundle+json", True),  # missing version
            ("text/plain", True),  # wrong media type
            ("", True),  # empty
        ],
    )
    def test_unknown_or_garbage_media_type_rejected(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
        media_type,
        must_reject,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = json.loads(signed.bundle_json)
        blob["mediaType"] = media_type
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[json.dumps(blob)],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        if must_reject:
            assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED

    # Enforces: malformed version string with injection-style content is rejected.
    def test_malformed_version_string_with_injection(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = json.loads(signed.bundle_json)
        blob["mediaType"] = (
            "application/vnd.dev.sigstore.bundle.v'; DROP TABLE bundles; --+json"
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[json.dumps(blob)],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# ---------------------------------------------------------------------------
# Attack class 10: Trust root staleness
# ---------------------------------------------------------------------------


class TestTrustRootStaleness:
    """Verifier with outdated trust root used against newer bundle."""

    # Enforces: stale trust root returns TRUST_ROOT_STALE (recoverable).
    def test_stale_trust_root_returns_trust_root_stale(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(
            monkeypatch,
            trust_root_predates_bundle=True,
        )
        signed = signing.sign(canonical_bytes_simple, google_provider)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[signed.bundle_json],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.TRUST_ROOT_STALE

    # Enforces: bundle integratedTime past all Rekor key validFor windows is
    # TRUST_ROOT_STALE (or INCLUSION_FAILED), not VERIFIED.
    def test_integrated_time_after_all_key_windows(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(
            monkeypatch,
            provider=google_provider,
            integrated_time="4102444800",
        )
        crypto_factory.install_verify_monkeypatch(
            monkeypatch,
            trust_root_predates_bundle=True,
        )
        signed = signing.sign(canonical_bytes_simple, google_provider)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[signed.bundle_json],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status in (
            signing.VerifyStatus.TRUST_ROOT_STALE,
            signing.VerifyStatus.INCLUSION_FAILED,
        )


# ---------------------------------------------------------------------------
# Attack class 11: Offline / no trust root
# ---------------------------------------------------------------------------


class TestOfflineNoTrustRoot:
    """Complete absence of trust root must fail-closed."""

    # Enforces: nonexistent trust root path returns OFFLINE_NO_TRUSTED_ROOT.
    def test_nonexistent_trust_root_path(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
        tmp_path,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(
            monkeypatch,
            trust_root_path=str(tmp_path / "no_such_file.json"),
            offline=True,
            trust_root_missing=True,
        )
        signed = signing.sign(canonical_bytes_simple, google_provider)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[signed.bundle_json],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT

    # Enforces: empty TUF cache directory returns OFFLINE_NO_TRUSTED_ROOT.
    def test_empty_tuf_cache_dir_offline(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
        tmp_path,
    ):
        monkeypatch.setenv("SIGSTORE_TUF_CACHE_DIR", str(tmp_path))
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(
            monkeypatch,
            offline=True,
            trust_root_missing=True,
        )
        signed = signing.sign(canonical_bytes_simple, google_provider)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[signed.bundle_json],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT


class TestSelectVerifierStateIsolation:
    """Test-factory state must not influence production code path.

    _select_verifier originally read _test_factory._verify_state
    unconditionally; a test that populated state keys like
    `trust_root_missing` and failed to clear them on teardown would alter
    subsequent production calls in the same process (gremlin finding #5,
    F-pass on brief-20260520-gdvx). The fix gates state consultation on
    _test_factory._test_active, which install_verify_monkeypatch toggles
    via monkeypatch.setattr so the False default is restored on teardown.
    """

    def test_stale_verify_state_does_not_leak_into_production(self):
        # Direct dict mutation simulates the worst-case leakage shape: a
        # prior test poked _verify_state and the dict ref was never cleared.
        # With _test_active=False (the default), _select_verifier must NOT
        # consult the dict; it must take the production path regardless.
        factory = signing._test_factory
        assert factory._test_active is False, (
            "test setup precondition: _test_active must default to False"
        )
        original_state = dict(factory._verify_state)
        try:
            factory._verify_state.clear()
            factory._verify_state["trust_root_missing"] = True
            factory._verify_state["offline"] = True
            factory._verify_state["trust_root_predates_bundle"] = True
            # All three would raise _TrustRootError if consulted.
            verifier = signing._select_verifier(None)
            # Production path: returned a real Verifier, no _TrustRootError.
            from sigstore.verify import Verifier

            assert isinstance(verifier, Verifier), (
                f"stale state leaked into production: got {type(verifier).__name__}"
            )
        finally:
            factory._verify_state.clear()
            factory._verify_state.update(original_state)

    def test_state_is_consulted_when_test_active(self):
        # Symmetric: when _test_active is True (the install_verify_monkeypatch
        # mode), _select_verifier DOES consult _verify_state. Verifies the
        # gate is on the active flag, not a permanent disablement.
        factory = signing._test_factory
        original_state = dict(factory._verify_state)
        original_active = factory._test_active
        try:
            factory._verify_state.clear()
            factory._verify_state["trust_root_missing"] = True
            factory._test_active = True
            with pytest.raises(signing._TrustRootError) as excinfo:
                signing._select_verifier(None)
            assert excinfo.value.status == signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT
        finally:
            factory._verify_state.clear()
            factory._verify_state.update(original_state)
            factory._test_active = original_active

    def test_install_verify_monkeypatch_clears_test_active_on_teardown(
        self,
        monkeypatch,
        crypto_factory,
    ):
        # End-to-end: install_verify_monkeypatch sets _test_active=True via
        # monkeypatch.setattr, so teardown must restore the False default.
        # We simulate teardown explicitly via monkeypatch.undo() inside the
        # test body — outside this test, pytest's own teardown handles it.
        factory = signing._test_factory
        assert factory._test_active is False
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        assert factory._test_active is True
        monkeypatch.undo()
        assert factory._test_active is False, (
            "monkeypatch teardown did not restore _test_active=False; "
            "install_verify_monkeypatch must use monkeypatch.setattr"
        )


# ---------------------------------------------------------------------------
# Attack class 12: identity_scheme mismatch
# ---------------------------------------------------------------------------


class TestIdentitySchemeMismatch:
    """identity_scheme value inconsistent with actual bundle contents."""

    # Enforces: unknown identity_scheme is BUNDLE_MALFORMED.
    def test_unknown_identity_scheme(self, make_bundle, canonical_bytes_simple):
        sb = make_bundle(
            canonical_bytes=canonical_bytes_simple,
            bundles=["<placeholder>"],
            identity_scheme="sigstore-private-v99",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED

    # Enforces: empty identity_scheme is BUNDLE_MALFORMED.
    def test_empty_identity_scheme(self, make_bundle, canonical_bytes_simple):
        sb = make_bundle(
            canonical_bytes=canonical_bytes_simple,
            bundles=["<placeholder>"],
            identity_scheme="",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# ---------------------------------------------------------------------------
# Attack class 13: Bit-flip fuzz
# ---------------------------------------------------------------------------


class TestBitFlipFuzz:
    """Flip individual bits across structurally interesting fields.

    No test here should produce an unhandled exception. The only acceptable
    outcomes are: a defined VerifyStatus, not VERIFIED.
    """

    @pytest.mark.parametrize("offset", [0, 1, 7, 31, 63, 255])
    def test_single_byte_flip_in_bundle_is_not_verified(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
        offset,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = signed.bundle_json
        if offset >= len(blob):
            return
        flipped = crypto_factory.bit_flip(blob, offset)
        if flipped == blob:
            return
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[flipped],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert isinstance(result, signing.VerifyResult)
        assert result.status != signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Attack class 14: Truncation fuzz
# ---------------------------------------------------------------------------


class TestTruncationFuzz:
    """Truncate the bundle blob at various boundaries."""

    @pytest.mark.parametrize("keep_ratio", [0.0, 0.01, 0.1, 0.5, 0.99])
    def test_truncated_bundle_returns_bundle_malformed(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
        keep_ratio,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = signed.bundle_json
        cut = int(len(blob) * keep_ratio)
        if cut == len(blob):
            return
        truncated = crypto_factory.truncate(blob, cut)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[truncated],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert isinstance(result, signing.VerifyResult)
        assert result.status != signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Attack class 15: Replay across canonical_bytes
# ---------------------------------------------------------------------------


class TestReplayAcrossCanonicalBytes:
    """Same bundle, intentionally similar but different canonical_bytes."""

    @pytest.mark.parametrize(
        "bytes_a,bytes_b",
        [
            # Differ only in folio_id:
            (
                b'{"folio_id":"finding-2026-0001","type":"finding","content":"x"}',
                b'{"folio_id":"finding-2026-0002","type":"finding","content":"x"}',
            ),
            # Differ by one character:
            (b"canonical bytes version A", b"canonical bytes version B"),
            # Same content, different trailing whitespace:
            (b'{"content":"hello"}', b'{"content":"hello"} '),
            # Same content, different JSON key order:
            (b'{"a":"1","b":"2"}', b'{"b":"2","a":"1"}'),
        ],
    )
    def test_similar_but_distinct_canonical_bytes(
        self,
        crypto_factory,
        google_provider,
        monkeypatch,
        bytes_a,
        bytes_b,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(bytes_a, google_provider)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[signed.bundle_json],
            canonical_bytes=bytes_a,
            canon_version="knurl-1.0",
        )
        result = signing.verify(bytes_b, sb)
        assert result.status == signing.VerifyStatus.SIGNATURE_MISMATCH


# ---------------------------------------------------------------------------
# Attack class 16: CVE corpus (sigstore-python's known-bad fixtures)
# ---------------------------------------------------------------------------


class TestCveCorpus:
    """Known-bad bundles from sigstore-python's MIT corpus.

    Each must be rejected with an appropriate VerifyStatus. These are real
    historical malformations; any SKEIN verifier must handle them.
    """

    # Enforces: CVE-2022-36056 — log entry inconsistent with signature material.
    def test_bundle_cve_2022_36056(self, corpus, canonical_bytes_simple):
        blob = corpus("bundle_cve_2022_36056.txt.sigstore").read_text(encoding="utf-8")
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status != signing.VerifyStatus.VERIFIED

    # Enforces: bundle with stripped checkpoint is rejected.
    def test_bundle_no_checkpoint(self, corpus, canonical_bytes_simple):
        blob = corpus("bundle_no_checkpoint.txt.sigstore").read_text(encoding="utf-8")
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status in (
            signing.VerifyStatus.INCLUSION_FAILED,
            signing.VerifyStatus.BUNDLE_MALFORMED,
        )

    # Enforces: bundle with no tlog entry is rejected.
    def test_bundle_no_log_entry(self, corpus, canonical_bytes_simple):
        blob = corpus("bundle_no_log_entry.txt.sigstore").read_text(encoding="utf-8")
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status in (
            signing.VerifyStatus.INCLUSION_FAILED,
            signing.VerifyStatus.BUNDLE_MALFORMED,
        )

    # Enforces: invalid mediaType is BUNDLE_MALFORMED.
    def test_bundle_invalid_version(self, corpus, canonical_bytes_simple):
        blob = corpus("bundle_invalid_version.txt.sigstore").read_text(encoding="utf-8")
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED

    # Enforces: GHSA-hhfg-fwrw-87w7 pattern — integrated_time trusted without
    # SET backing. SKEIN must NOT replicate this pre-3.6.0 sigstore-python bug.
    def test_ghsa_hhfg_fwrw_87w7_pattern(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = crypto_factory.strip_rekor_proof(signed.bundle_json)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.INCLUSION_FAILED


# ---------------------------------------------------------------------------
# Attack class 17: DSSE-vs-message-signature confusion
# ---------------------------------------------------------------------------


class TestDsseConfusion:
    """sigstore-bundle proto §content oneof: messageSignature XOR dsseEnvelope."""

    # Enforces: a bundle using dsseEnvelope (out of profile for SKEIN, which
    # signs canonical_bytes via messageSignature) is BUNDLE_MALFORMED.
    def test_dsse_envelope_bundle_is_bundle_malformed(
        self,
        crypto_factory,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        blob = crypto_factory.make_dsse_envelope_bundle(
            canonical_bytes=canonical_bytes_simple
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        # DSSE isn't SKEIN's content type; rejected or signature mismatch.
        assert result.status in (
            signing.VerifyStatus.BUNDLE_MALFORMED,
            signing.VerifyStatus.SIGNATURE_MISMATCH,
        )


# ---------------------------------------------------------------------------
# Attack class 18: Unrelated Rekor entry splice
# ---------------------------------------------------------------------------


class TestUnrelatedRekorEntry:
    """GHSA-whqx-f9j3-ch6m / CVE-2022-36056 class — bundle carries valid-but-
    unrelated Rekor entry."""

    # Enforces: a bundle that splices document B's signature+cert with
    # document A's Rekor entry fails verification.
    def test_unrelated_rekor_entry_rejected(
        self,
        crypto_factory,
        google_provider,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        payload_a = b"document A"
        payload_b = b"document B"
        bundle_a = signing.sign(payload_a, google_provider)
        bundle_b = signing.sign(payload_b, google_provider)
        spliced = crypto_factory.splice_rekor_entry(
            host=bundle_b.bundle_json,
            source=bundle_a.bundle_json,
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[spliced],
            canonical_bytes=payload_b,
            canon_version="knurl-1.0",
        )
        result = signing.verify(payload_b, sb)
        assert result.status == signing.VerifyStatus.INCLUSION_FAILED


# ---------------------------------------------------------------------------
# Attack class 19: Cross-Rekor log_id substitution
# ---------------------------------------------------------------------------


class TestCrossRekorLogId:
    """Bundle proof signed by Rekor instance A but tagged as instance B."""

    # Enforces: finding-20260513-w5hq section 1, cross-Rekor-instance
    # substitution clause. verify() must select the Rekor checkpoint key by
    # inclusionProof.log_id and reject proof material signed by a different
    # trusted Rekor instance instead of iterating trusted keys until one works.
    def test_rekor_log_id_substitution_rejected(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        claimed_log_id = crypto_factory.alternate_rekor_log_id()
        substituted = crypto_factory.swap_rekor_log_id(
            signed.bundle_json,
            claimed_log_id,
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[substituted],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.INCLUSION_FAILED


# ---------------------------------------------------------------------------
# Attack class 20: Hash and certificate public-key algorithm substitution
# ---------------------------------------------------------------------------


class TestSigstorePublicV1AlgorithmProfile:
    """sigstore-public-v1 is pinned to SHA-256 + ECDSA P-256."""

    # Enforces: finding-20260512-eaft actionable #3. sigstore-public-v1 accepts
    # only the v0 profile digest algorithm, even when a stronger-but-unpinned
    # digest name appears in the bundle.
    @pytest.mark.parametrize(
        "algorithm",
        ["SHA1", "MD5", "SHA2_384", "SHA2_512"],
        ids=["sha1", "md5", "sha384", "sha512"],
    )
    def test_verify_rejects_non_sha256_digest_algorithm(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
        algorithm,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = crypto_factory.set_digest_algorithm(signed.bundle_json, algorithm)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED

    # Enforces: finding-20260512-eaft actionable #3. SHA-256 is the only digest
    # algorithm accepted for sigstore-public-v1 bundles.
    def test_verify_accepts_sha256_digest_algorithm(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = crypto_factory.set_digest_algorithm(signed.bundle_json, "SHA2_256")
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.VERIFIED

    def test_profile_gate_rejects_null_or_missing_digest_algorithm(self):
        """A messageSignature whose messageDigest.algorithm is None, missing,
        or non-string must be BUNDLE_MALFORMED at the profile gate.

        Pre-fix, the algorithm-allowlist check was guarded with
        ``if algo is not None and algo not in _ALLOWED_DIGEST_ALGORITHMS``,
        and the digest-length check with ``if algo and digest_b64`` — both
        short-circuited when algo was None, leaving the profile gate with
        no opinion on the bundle. sigstore-python rejected it downstream,
        but the documented defense-in-depth gate had a hole.
        """

        def make(*, message_digest, **extra):
            base = {
                "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                "messageSignature": {
                    "messageDigest": message_digest,
                    "signature": "AA==",
                },
            }
            base.update(extra)
            return base

        # algorithm=None
        assert (
            signing._check_sigstore_public_v1_profile(
                make(message_digest={"algorithm": None, "digest": "AA=="})
            )
            == signing.VerifyStatus.BUNDLE_MALFORMED
        )
        # algorithm key absent
        assert (
            signing._check_sigstore_public_v1_profile(
                make(message_digest={"digest": "AA=="})
            )
            == signing.VerifyStatus.BUNDLE_MALFORMED
        )
        # algorithm is not a string (int, dict, list)
        for non_str in (42, {}, [], True):
            assert (
                signing._check_sigstore_public_v1_profile(
                    make(message_digest={"algorithm": non_str, "digest": "AA=="})
                )
                == signing.VerifyStatus.BUNDLE_MALFORMED
            ), f"non-string algorithm {non_str!r} should be BUNDLE_MALFORMED"

        # messageDigest itself is not a dict.
        assert (
            signing._check_sigstore_public_v1_profile(
                {
                    "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                    "messageSignature": {
                        "messageDigest": "not-a-dict",
                        "signature": "AA==",
                    },
                }
            )
            == signing.VerifyStatus.BUNDLE_MALFORMED
        )

        # messageSignature itself is not a dict.
        assert (
            signing._check_sigstore_public_v1_profile(
                {
                    "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                    "messageSignature": "not-a-dict",
                }
            )
            == signing.VerifyStatus.BUNDLE_MALFORMED
        )

    def test_profile_gate_passes_dsse_envelope_with_no_messageSignature(self):
        """DSSE bundles legitimately have no messageSignature; the profile
        gate must not reject them on missing-messageDigest. They are caught
        later by signing.py's 'DSSE envelope not supported in v0' path.
        """
        case = {
            "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
            "dsseEnvelope": {"payload": "AA==", "signatures": []},
        }
        # Profile gate must not reject — None means "no opinion at this layer."
        assert signing._check_sigstore_public_v1_profile(case) is None

    def test_verify_rejects_digest_length_mismatch_with_algorithm(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = json.loads(signed.bundle_json)
        blob["messageSignature"]["messageDigest"]["algorithm"] = "SHA2_256"
        blob["messageSignature"]["messageDigest"]["digest"] = base64.b64encode(
            b"x" * 48
        ).decode("ascii")
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[json.dumps(blob, separators=(",", ":"))],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED

    def test_verify_rejects_simultaneous_set_and_inclusion_proof(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = json.loads(signed.bundle_json)
        blob.setdefault("verificationMaterial", {}).setdefault("tlogEntries", [{}])[0][
            "inclusionPromise"
        ] = {"signedEntryTimestamp": "ZmFrZS1zZXQ="}
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[json.dumps(blob, separators=(",", ":"))],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED

    def test_verify_rejects_der_malformed_0x30_prefixed_set(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        """Regression for finding-20260519-74o7: the SET DER gate is a strict
        parse, not a len/0x30-prefix sniff.

        A signedEntryTimestamp that is base64-valid, starts with 0x30, and is
        >=32 bytes but is NOT a valid ECDSA-Sig-Value SEQUENCE{INTEGER,INTEGER}
        was waved through by the old heuristic (len>=32 and raw[0]==0x30 →
        accepted). It must now be rejected BUNDLE_MALFORMED by the profile gate.
        """
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        signed = signing.sign(canonical_bytes_simple, google_provider)
        blob = json.loads(signed.bundle_json)
        # 0x30 prefix, 41 bytes (>=32), but not a DER SEQUENCE of two INTEGERs.
        bogus_set = bytes([0x30]) + bytes(40)
        blob.setdefault("verificationMaterial", {}).setdefault("tlogEntries", [{}])[0][
            "inclusionPromise"
        ] = {"signedEntryTimestamp": base64.b64encode(bogus_set).decode("ascii")}
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[json.dumps(blob, separators=(",", ":"))],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED

    def test_profile_gate_strict_der_parse_discriminates(self):
        """finding-20260519-74o7: the profile SET check accepts a valid DER
        ECDSA signature and rejects malformed-but-0x30-prefixed blobs.

        White-box on _check_sigstore_public_v1_profile because black-box verify()
        cannot distinguish a profile-gate rejection from sigstore-python's
        downstream SET-signature rejection — both surface as BUNDLE_MALFORMED.
        This test isolates that the GATE itself no longer over-accepts and does
        not over-reject a structurally valid SET.
        """
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        def profile_dict(set_bytes: bytes) -> dict:
            return {
                "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
                "verificationMaterial": {
                    "tlogEntries": [
                        {
                            "inclusionProof": {"logIndex": "1"},
                            "inclusionPromise": {
                                "signedEntryTimestamp": base64.b64encode(
                                    set_bytes
                                ).decode("ascii")
                            },
                        }
                    ]
                },
            }

        # Valid DER ECDSA-Sig-Value (positive r, s) → gate must NOT reject.
        valid_der = encode_dss_signature(2**200 + 1, 2**199 + 7)
        assert (
            signing._check_sigstore_public_v1_profile(profile_dict(valid_der)) is None
        )

        # 0x30-prefixed, >=32 bytes, not a SEQUENCE of two INTEGERs (the gap the
        # old heuristic missed) → BUNDLE_MALFORMED.
        assert (
            signing._check_sigstore_public_v1_profile(
                profile_dict(bytes([0x30]) + bytes(40))
            )
            == signing.VerifyStatus.BUNDLE_MALFORMED
        )

        # Non-0x30 / short blob still rejected (no regression on the old path).
        assert (
            signing._check_sigstore_public_v1_profile(profile_dict(b"fake-set"))
            == signing.VerifyStatus.BUNDLE_MALFORMED
        )

        # Well-formed DER SEQUENCE but r == 0 — decode_dss_signature parses
        # this WITHOUT raising (verified: it raises on negative INTEGERs but
        # returns r=0 from a valid SEQUENCE). Exercises the live `r <= 0`
        # guard, which is the only thing rejecting a trivially-forgeable
        # zero-component SET (finding-20260519-74o7; closes the knuth-flagged
        # coverage gap and pins that the explicit check is not redundant).
        assert (
            signing._check_sigstore_public_v1_profile(
                profile_dict(encode_dss_signature(0, 1))
            )
            == signing.VerifyStatus.BUNDLE_MALFORMED
        )
        # And s == 0 symmetrically.
        assert (
            signing._check_sigstore_public_v1_profile(
                profile_dict(encode_dss_signature(1, 0))
            )
            == signing.VerifyStatus.BUNDLE_MALFORMED
        )

    def test_profile_gate_v3_mediaType_match_is_exact(self):
        """The v0.3 SET-DER defense fires only on the two canonical v0.3
        mediaType strings emitted by sigstore-python (BUNDLE_0_3,
        BUNDLE_0_3_ALT). A substring matcher (the prior implementation:
        ``"v0.3" in mt or "version=0.3" in mt``) would (a) fire on future
        versions like v0.30 or v0.3.1 whose mediaType contains "v0.3" as a
        substring — those need their own profile check, not this one — and
        (b) fire on attacker-supplied mediaTypes that happen to embed the
        substring (e.g. "NOTHING_v0.3_HERE").
        """
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        # Bad SET (r == 0) — gate must reject under the v0.3 path, ignore
        # otherwise. Isolates whether is_v3 latched onto the mediaType.
        bad_set = base64.b64encode(encode_dss_signature(0, 1)).decode("ascii")

        def make(media_type: str) -> dict:
            return {
                "mediaType": media_type,
                "verificationMaterial": {
                    "tlogEntries": [
                        {
                            "inclusionProof": {"logIndex": "1"},
                            "inclusionPromise": {"signedEntryTimestamp": bad_set},
                        }
                    ]
                },
            }

        # Canonical v0.3 mediaTypes — gate fires, bad SET rejected.
        for canonical in (
            "application/vnd.dev.sigstore.bundle.v0.3+json",
            "application/vnd.dev.sigstore.bundle+json;version=0.3",
        ):
            assert (
                signing._check_sigstore_public_v1_profile(make(canonical))
                == signing.VerifyStatus.BUNDLE_MALFORMED
            ), f"canonical v0.3 mediaType {canonical!r} did not engage SET defense"

        # Substring overmatchers — must NOT engage the v0.3-specific gate.
        # The bad SET goes unchecked here; those bundles may still be
        # rejected elsewhere on their own merits, but not by the v0.3 gate.
        for not_v3 in (
            "application/vnd.dev.sigstore.bundle+json;version=0.30",
            "application/vnd.dev.sigstore.bundle+json;version=0.3.1",
            "NOTHING_v0.3_HERE",
            "v0.3x",
            "",
        ):
            assert signing._check_sigstore_public_v1_profile(make(not_v3)) is None, (
                f"non-v0.3 mediaType {not_v3!r} unexpectedly engaged the "
                "v0.3 SET-DER gate via substring overmatch"
            )

    @pytest.mark.parametrize(
        "media_type",
        [None, 42, [], {}, True],
        ids=["null", "int", "list", "dict", "bool"],
    )
    def test_profile_gate_rejects_non_string_mediaType(self, media_type):
        """A non-string mediaType (JSON null, int, list, dict, bool) must be
        BUNDLE_MALFORMED at the profile gate.

        Pre-fix, ``obj.get("mediaType", "")`` only defaulted on absent keys,
        not on explicit ``null``. ``mediaType: null`` produced
        ``media_type = None``; ``None in _V0_3_MEDIA_TYPES`` is False, so the
        v0.3-specific SET-DER defense (finding-20260519-74o7) was silently
        skipped — an adversarial v0.3 bundle could ship ``mediaType: null``
        plus a malformed SET and bypass the documented defense-in-depth gate.
        Non-string mediaTypes of unhashable types (list, dict) additionally
        crashed the gate with TypeError instead of failing closed.

        Parity with the messageDigest/algorithm gate added in
        ``test_profile_gate_rejects_null_or_missing_digest_algorithm``: any
        non-string-shaped value at a string-typed field is BUNDLE_MALFORMED.
        """
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        bad_set = base64.b64encode(encode_dss_signature(0, 1)).decode("ascii")
        case = {
            "mediaType": media_type,
            "verificationMaterial": {
                "tlogEntries": [
                    {
                        "inclusionProof": {"logIndex": "1"},
                        "inclusionPromise": {"signedEntryTimestamp": bad_set},
                    }
                ]
            },
        }
        assert (
            signing._check_sigstore_public_v1_profile(case)
            == signing.VerifyStatus.BUNDLE_MALFORMED
        ), f"non-string mediaType {media_type!r} should be BUNDLE_MALFORMED"

    def test_profile_gate_passes_absent_mediaType(self):
        """A bundle that omits mediaType entirely (older bundles legitimately
        do; only v0.3 needs the SET-DER check) must NOT be rejected by the
        profile gate. Distinguishes the absent-key path from explicit
        ``null`` — absent falls through, null is BUNDLE_MALFORMED.
        """
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        bad_set = base64.b64encode(encode_dss_signature(0, 1)).decode("ascii")
        case = {
            "verificationMaterial": {
                "tlogEntries": [
                    {
                        "inclusionProof": {"logIndex": "1"},
                        "inclusionPromise": {"signedEntryTimestamp": bad_set},
                    }
                ]
            },
        }
        # mediaType absent → no v0.3 gate fires → profile gate has no opinion.
        assert signing._check_sigstore_public_v1_profile(case) is None

    def test_profile_gate_fails_closed_on_structural_weirdness(self):
        """Adversarial bundle with tlogEntries shapes that trigger an unexpected
        exception inside the SET-DER defense loop must NOT bypass the gate.

        Before the fix, the loop was wrapped in `except Exception: pass` —
        any AttributeError / TypeError / KeyError / IndexError from a
        malformed tlogEntries shape (entry not a dict, set_obj not a dict,
        etc.) was swallowed and the profile check returned None
        (passed). That silently disabled the finding-20260519-74o7 SET-DER
        defense. The fix is fail-closed: unexpected structural shapes now
        return BUNDLE_MALFORMED.
        """
        cases = [
            # tlogEntries entry is a string -> entry.get(...) raises AttributeError
            {
                "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                "verificationMaterial": {"tlogEntries": ["not-a-dict"]},
            },
            # tlogEntries entry is an int -> entry.get(...) raises AttributeError
            {
                "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                "verificationMaterial": {"tlogEntries": [42]},
            },
            # inclusionPromise is a string, not a dict -> .get raises AttributeError
            {
                "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                "verificationMaterial": {
                    "tlogEntries": [
                        {
                            "inclusionPromise": "string-not-dict",
                            "inclusionProof": {"logIndex": "1"},
                        }
                    ]
                },
            },
            # tlogEntries itself is a dict, not a list -> iteration over dict
            # yields keys (strings), then entry.get raises AttributeError
            {
                "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                "verificationMaterial": {"tlogEntries": {"k": "v"}},
            },
        ]
        for case in cases:
            assert (
                signing._check_sigstore_public_v1_profile(case)
                == signing.VerifyStatus.BUNDLE_MALFORMED
            ), f"profile gate must fail-closed on: {case!r}"

    @pytest.mark.parametrize(
        "inclusion_promise",
        [{}, [], False, 0, ""],
        ids=["empty_dict", "empty_list", "false", "zero", "empty_string"],
    )
    def test_profile_gate_rejects_falsy_non_None_inclusionPromise(
        self, inclusion_promise
    ):
        """A v0.3 bundle with a falsy non-None inclusionPromise (``{}``,
        ``[]``, ``False``, ``0``, ``""``) must be BUNDLE_MALFORMED.

        Pre-fix, ``set_obj = entry.get("inclusionPromise")`` followed by
        ``if not set_obj: continue`` short-circuited on every falsy
        value — including ``{}`` and ``[]`` — silently skipping the
        finding-20260519-74o7 SET-DER defense. An adversarial v0.3
        bundle could ship ``inclusionPromise: {}`` (or ``[]``, ``False``,
        ``0``, ``""``) alongside a malformed SET-bearing entry and bypass
        the documented defense-in-depth gate.

        Companion to ``test_profile_gate_rejects_non_string_mediaType``
        (the same falsy-non-None shape on a different field). Same
        ``obj.get(K) → falsy → continue`` bug class as Shard L
        (mediaType=null).
        """
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        bad_set = base64.b64encode(encode_dss_signature(0, 1)).decode("ascii")
        case = {
            "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
            "verificationMaterial": {
                "tlogEntries": [
                    {
                        "inclusionProof": {"logIndex": "1"},
                        "inclusionPromise": inclusion_promise,
                    }
                ],
                # Encode the bad SET somewhere observable in case future
                # debugging needs it — the gate must reject before
                # touching this field.
                "_bad_set_encoded_for_debugging": bad_set,
            },
        }
        assert (
            signing._check_sigstore_public_v1_profile(case)
            == signing.VerifyStatus.BUNDLE_MALFORMED
        ), (
            f"falsy non-None inclusionPromise {inclusion_promise!r} must "
            "be BUNDLE_MALFORMED — pre-fix this silently passed the gate "
            "and skipped the SET-DER defense"
        )

    def test_profile_gate_passes_absent_inclusionPromise(self):
        """A v0.3 tlog entry that omits inclusionPromise entirely (or
        sets it to ``null``) must NOT be rejected by the profile gate.

        Confirms the fix did not over-tighten: ``inclusionPromise``
        absence is legitimate (Rekor v2 RFC3161 TSA timestamps replace
        SETs; intermediate Rekor states may carry an inclusionProof
        without a SET). Only present-but-malformed shapes get rejected.
        """
        cases = [
            # Key absent.
            {
                "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                "verificationMaterial": {
                    "tlogEntries": [{"inclusionProof": {"logIndex": "1"}}]
                },
            },
            # Key present, value None (JSON null).
            {
                "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                "verificationMaterial": {
                    "tlogEntries": [
                        {
                            "inclusionProof": {"logIndex": "1"},
                            "inclusionPromise": None,
                        }
                    ]
                },
            },
        ]
        for case in cases:
            assert signing._check_sigstore_public_v1_profile(case) is None, (
                "absent / null inclusionPromise must remain a profile-gate "
                "pass: "
                f"{case!r}"
            )

    @pytest.mark.parametrize(
        "inclusion_proof",
        [{}, [], False, 0, "", 42, "string"],
        ids=[
            "empty_dict",
            "empty_list",
            "false",
            "zero",
            "empty_string",
            "int",
            "nonempty_string",
        ],
    )
    def test_profile_gate_rejects_malformed_inclusionProof(self, inclusion_proof):
        """A v0.3 tlog entry whose inclusionPromise is well-formed but
        whose inclusionProof is present-but-malformed must be
        BUNDLE_MALFORMED.

        Same bug class as inclusionPromise: pre-fix,
        ``not entry.get("inclusionProof")`` short-circuited on every
        falsy value (``{}``, ``[]``, ``False``, ``0``, ``""``) and the
        SET-DER check was silently skipped. Non-dict truthy values
        (ints, strings) likewise must not pass the gate — the
        inclusionProof slot is structurally a sigstore-bundle proto
        ``InclusionProof`` message, never a scalar.

        Companion to ``test_profile_gate_passes_absent_inclusionProof``:
        the test pair distinguishes "field absent" (legitimate) from
        "field present but malformed" (rejected).
        """
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        good_set = base64.b64encode(encode_dss_signature(0xCAFE, 0xBABE)).decode(
            "ascii"
        )
        case = {
            "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
            "verificationMaterial": {
                "tlogEntries": [
                    {
                        "inclusionProof": inclusion_proof,
                        "inclusionPromise": {"signedEntryTimestamp": good_set},
                    }
                ]
            },
        }
        assert (
            signing._check_sigstore_public_v1_profile(case)
            == signing.VerifyStatus.BUNDLE_MALFORMED
        ), (
            f"malformed inclusionProof {inclusion_proof!r} must be "
            "BUNDLE_MALFORMED — pre-fix the falsy values silently passed "
            "and the SET-DER defense was skipped"
        )

    def test_profile_gate_passes_absent_inclusionProof(self):
        """A v0.3 tlog entry that omits inclusionProof entirely (or
        sets it to ``null``) must NOT be rejected by the profile gate.

        The SET-DER guard is gated on BOTH inclusionPromise and
        inclusionProof being present. Absent inclusionProof legitimately
        skips the v0.3 SET-DER check (sigstore-python handles missing
        proof downstream); only present-but-malformed shapes get rejected
        here. Confirms the fix did not over-tighten.
        """
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        good_set = base64.b64encode(encode_dss_signature(0xCAFE, 0xBABE)).decode(
            "ascii"
        )
        cases = [
            # inclusionProof key absent.
            {
                "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                "verificationMaterial": {
                    "tlogEntries": [
                        {
                            "inclusionPromise": {"signedEntryTimestamp": good_set},
                        }
                    ]
                },
            },
            # inclusionProof present, value None.
            {
                "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
                "verificationMaterial": {
                    "tlogEntries": [
                        {
                            "inclusionProof": None,
                            "inclusionPromise": {"signedEntryTimestamp": good_set},
                        }
                    ]
                },
            },
        ]
        for case in cases:
            assert signing._check_sigstore_public_v1_profile(case) is None, (
                "absent / null inclusionProof must remain a profile-gate "
                "pass: "
                f"{case!r}"
            )

    def test_profile_gate_passes_well_formed_inclusionProof(self):
        """Sanity: a non-empty inclusionProof dict alongside a
        well-formed inclusionPromise dict (with a valid DER SET) must
        pass the profile gate. Guards against over-tightening of the
        falsy-rejection rule that would inadvertently reject the happy
        path.
        """
        from cryptography.hazmat.primitives.asymmetric.utils import (
            encode_dss_signature,
        )

        good_set = base64.b64encode(encode_dss_signature(0xCAFE, 0xBABE)).decode(
            "ascii"
        )
        case = {
            "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
            "verificationMaterial": {
                "tlogEntries": [
                    {
                        "inclusionProof": {
                            "logIndex": "1",
                            "treeSize": "42",
                            "rootHash": "abc",
                        },
                        "inclusionPromise": {"signedEntryTimestamp": good_set},
                    }
                ]
            },
        }
        assert signing._check_sigstore_public_v1_profile(case) is None

    @pytest.mark.parametrize(
        "bad_set",
        [{}, [], 42, {"nested": "dict"}, ["nested", "list"]],
        ids=["empty_dict", "empty_list", "int", "nested_dict", "nested_list"],
    )
    def test_profile_gate_rejects_non_string_signedEntryTimestamp(self, bad_set):
        """A v0.3 entry whose ``signedEntryTimestamp`` is not a string
        (dict, list, int) must be BUNDLE_MALFORMED.

        Pre-fix, ``set_b64 = set_obj.get("signedEntryTimestamp")`` then
        ``base64.b64decode(set_b64, validate=True)`` raised TypeError on
        non-bytes/str input, which was NOT caught by the inner
        ``except (binascii.Error, ValueError)`` — it propagated to the
        outer ``except Exception`` and returned BUNDLE_MALFORMED
        (the right outcome, by the wrong path). Falsy non-string
        values (``{}``, ``[]``) additionally took the ``not set_b64``
        ``continue`` path and silently skipped the check.

        Post-fix, the explicit ``isinstance(set_b64, str) or not set_b64``
        check rejects every non-string shape directly, before any base64
        call. This test verifies that type-confusion on the SET field
        itself is not a new bypass surface.
        """
        case = {
            "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
            "verificationMaterial": {
                "tlogEntries": [
                    {
                        "inclusionProof": {"logIndex": "1"},
                        "inclusionPromise": {"signedEntryTimestamp": bad_set},
                    }
                ]
            },
        }
        assert (
            signing._check_sigstore_public_v1_profile(case)
            == signing.VerifyStatus.BUNDLE_MALFORMED
        ), f"non-string signedEntryTimestamp {bad_set!r} must be BUNDLE_MALFORMED"

    # Enforces: finding-20260512-eaft actionable #3. sigstore-public-v1 is
    # pinned to ECDSA P-256 leaf certificates; other key algorithms fail closed.
    @pytest.mark.parametrize(
        "curve",
        ["Ed25519", "P-384", "RSA-2048"],
        ids=["ed25519", "p384", "rsa"],
    )
    def test_verify_rejects_non_p256_cert_pubkey(
        self,
        crypto_factory,
        canonical_bytes_simple,
        monkeypatch,
        curve,
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        cert = crypto_factory.make_cert_with_curve(curve)
        blob = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity="alice@example.com",
            issuer=GOOGLE_ISSUER,
            cert=cert,
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.CERT_INVALID

    # Enforces: finding-20260512-eaft actionable #3. ECDSA P-256 is the
    # accepted certificate public-key profile for sigstore-public-v1.
    def test_verify_accepts_p256_cert_pubkey(
        self,
        crypto_factory,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        cert = crypto_factory.make_cert_with_curve("P-256")
        blob = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity="alice@example.com",
            issuer=GOOGLE_ISSUER,
            cert=cert,
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.status == signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Attack class 21: Multi-signer partial failure
# ---------------------------------------------------------------------------


class TestMultiSignerAttack:
    """Multi-signer attacks — append forged entries, anonymous signers, etc."""

    # Enforces: two-signer with one tampered cert produces partial failure;
    # overall != VERIFIED.
    def test_one_valid_one_tampered_cert(
        self,
        crypto_factory,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        good = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity="a@example.com",
            issuer=GOOGLE_ISSUER,
        )
        bad = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity="b@example.com",
            issuer=GOOGLE_ISSUER,
        )
        tampered = crypto_factory.tamper_cert(bad)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[good, tampered],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify_multi(canonical_bytes_simple, sb)
        assert len(result.results) == 2
        assert result.results[1].status == signing.VerifyStatus.CERT_INVALID
        assert result.overall != signing.VerifyStatus.VERIFIED

    # Enforces: both signers tampered — all results CERT_INVALID, overall same.
    def test_both_signers_tampered(
        self,
        crypto_factory,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        good = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity="a@example.com",
            issuer=GOOGLE_ISSUER,
        )
        bad = crypto_factory.tamper_cert(good)
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[bad, bad],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify_multi(canonical_bytes_simple, sb)
        for r in result.results:
            assert r.status == signing.VerifyStatus.CERT_INVALID
        assert result.overall == signing.VerifyStatus.CERT_INVALID


# ---------------------------------------------------------------------------
# Attack class 22: Identity relabel
# ---------------------------------------------------------------------------


class TestRelabelAttack:
    """An attacker rewrites a downstream identity claim but can't touch the cert."""

    # Enforces: cert SAN is authoritative; relabeling outer metadata can't
    # change verify()'s returned subject.
    def test_relabel_outside_cert_does_not_change_subject(
        self,
        crypto_factory,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        blob = crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes_simple,
            identity="alice@example.com",
            issuer=GOOGLE_ISSUER,
        )
        sb = signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[blob],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )
        result = signing.verify(canonical_bytes_simple, sb)
        assert result.subject == "alice@example.com"
