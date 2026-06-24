"""Test contract fixtures for skein.signing (Phase 2 unified).

What this file owns
-------------------
- The import gate: every test_signing/test_*.py imports `signing` from this
  conftest. If skein.signing functions are not present (pre-phase-3), tests
  that need them either xfail or skip via pytest.importorskip.
- Provider configs used in sign tests (real provider URLs, no live OIDC).
- Canonical-bytes fixtures covering size and Unicode edges.
- make_signature_bundle factory for shape-only tests.
- crypto_factory fixture pointing at signing._test_factory; Phase 3 implementer
  ships the factory so tests using real crypto behave deterministically without
  network.

Mock strategy (rationale)
-------------------------
Mock at the **sigstore-python boundary**, not at the HTTP boundary. signing.py
is a thin wrapper around sigstore-python; its contract is "we use sigstore-python
correctly and surface its outcomes as VerifyResult / SigningUnavailable."

- For sign() tests, monkeypatch sigstore.sign.SigningContext so signer.sign_artifact
  returns a fabricated Bundle produced by our fixture helpers.
- For verify() tests, either (a) use the conformance corpus (cross-implementation
  source of truth), or (b) monkeypatch sigstore.verify.Verifier.verify_artifact
  to raise a specific VerificationError subclass, asserting the wrapper maps it
  to the right VerifyStatus.
- Live Sigstore staging integration is opt-in via SKEIN_TEST_SIGSTORE_LIVE=1
  and marked pytest.mark.staging. Default test run never hits the network.

HTTP-level mocking (responses/vcrpy) is sigstore-python's internal concern and
churns across the 4.x → 5.x line. Mocking at the SigningContext / Verifier
boundary is stable across library upgrades.
"""

from __future__ import annotations

import importlib
import base64
import json
import os
from pathlib import Path
from typing import Any

import pytest


try:
    signing = importlib.import_module("skein.signing")
except ImportError:
    signing = None


# True when phase-3 functions (sign, verify, verify_multi) are present.
# Files that exercise those functions set their pytestmark to skip when False.
HAS_FUNCTIONS = (
    signing is not None
    and hasattr(signing, "sign")
    and hasattr(signing, "verify")
    and hasattr(signing, "verify_multi")
)


CORPUS_DIR = Path(__file__).resolve().parents[1] / "conformance" / "corpus"


# ---------------------------------------------------------------------------
# Provider configs (4-field OIDCProviderConfig per clarification 3)
# ---------------------------------------------------------------------------


@pytest.fixture
def google_provider():
    """A google OIDCProviderConfig — issuer + token (required), provider_id (optional)."""
    return signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token=make_jwt_token(aud="sigstore", issuer="https://accounts.google.com"),
        provider_id="google",
    )


@pytest.fixture
def github_provider():
    """A github OIDCProviderConfig.

    Issuer URL pulled from public Fulcio config at implementation time; the
    spec only commits "personal OAuth, not Actions OIDC" (Identity rev 5
    §"v0 OIDC providers (locked)").
    """
    return signing.OIDCProviderConfig(
        issuer="https://github.com/login/oauth",
        token=make_jwt_token(aud="sigstore", issuer="https://github.com/login/oauth"),
        provider_id="github",
    )


@pytest.fixture
def expired_google_provider():
    """A google OIDCProviderConfig whose token is already past expires_at.

    sign() must raise SigningUnavailable with a clear "OIDC token expired"
    reason before talking to Fulcio (clarification 3, last paragraph).
    """
    return signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token=make_jwt_token(aud="sigstore", issuer="https://accounts.google.com"),
        provider_id="google",
        expires_at=1_500_000_000_000_000,  # 2017-07-14T02:40:00Z
    )


# ---------------------------------------------------------------------------
# Canonical-bytes fixtures (edge cases)
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_bytes_simple() -> bytes:
    return b'{"folio_id":"finding-20260511-test","title":"hello"}'


@pytest.fixture
def canonical_bytes_empty() -> bytes:
    return b""


@pytest.fixture
def canonical_bytes_one_byte() -> bytes:
    return b"a"


@pytest.fixture
def canonical_bytes_one_mb() -> bytes:
    # 1 MiB payload — tests that signing.py imposes no smaller size cap.
    return b"x" * (1 << 20)


@pytest.fixture
def canonical_bytes_unicode_heavy() -> bytes:
    # NFC-normalized Unicode mix: emoji, RTL, CJK, combining marks, ZWJ.
    s = (
        "\U0001f525"  # FIRE
        "אבג"  # Hebrew alef bet gimel (RTL)
        "你好"  # CJK
        "é"  # NFC precomposed
        "\U0001f468‍\U0001f4bb"  # man + ZWJ + laptop
    )
    return s.encode("utf-8")


# ---------------------------------------------------------------------------
# SignatureBundle factory — shape-only tests
# ---------------------------------------------------------------------------


def make_signature_bundle(
    *,
    canonical_bytes: bytes,
    bundles: list[str] | None = None,
    identity_scheme: str = "sigstore-public-v1",
    canon_version: str = "knurl-1.0",
    trust_root_pin: str | None = None,
):
    """Build a SignatureBundle for tests that only need shape.

    bundles is a list of str (canonical sigstore-bundle ProtoJSON) per
    clarification 1. Each placeholder string is structurally a JSON token;
    cryptographic factories produce real verifiable blobs.
    """
    if bundles is None:
        bundles = ["<placeholder-sigstore-bundle-protojson>"]
    return signing.SignatureBundle(
        identity_scheme=identity_scheme,
        bundles=bundles,
        canonical_bytes=canonical_bytes,
        canon_version=canon_version,
        trust_root_pin=trust_root_pin,
    )


@pytest.fixture
def make_bundle():
    """Returns the factory; preferred over importing the bare function."""
    return make_signature_bundle


def make_jwt_token(
    *, aud: Any = "sigstore", include_aud: bool = True, issuer: str | None = None
) -> str:
    """Return an unsigned JWT-shaped token with caller-controlled claims.

    sign() only needs to parse the payload locally to enforce the v0 aud
    contract; Fulcio remains responsible for real JWT signature verification.
    """
    header = {"alg": "none", "typ": "JWT"}
    payload = {"sub": "alice@example.com"}
    if issuer is not None:
        payload["iss"] = issuer
    if include_aud:
        payload["aud"] = aud

    def b64url(data: dict[str, Any]) -> str:
        raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{b64url(header)}.{b64url(payload)}."


@pytest.fixture
def make_oidc_token():
    """Returns a JWT-shaped token factory for sign-side OIDC parsing tests."""
    return make_jwt_token


@pytest.fixture
def make_oidc_provider(make_oidc_token):
    """Build an OIDCProviderConfig with a controllable unsigned JWT payload."""

    def factory(
        *,
        aud: Any = "sigstore",
        include_aud: bool = True,
        issuer: str = "https://accounts.google.com",
        provider_id: str | None = "google",
        expires_at: int | None = None,
    ):
        return signing.OIDCProviderConfig(
            issuer=issuer,
            token=make_oidc_token(aud=aud, include_aud=include_aud, issuer=issuer),
            provider_id=provider_id,
            expires_at=expires_at,
        )

    return factory


# ---------------------------------------------------------------------------
# Cryptographic factories
# ---------------------------------------------------------------------------
#
# These produce a real `sigstore.models.Bundle` over user-controlled canonical
# bytes, signed by a test-only ECDSA P-256 key wrapped in a self-issued cert
# chain. They are usable as the strings inside SignatureBundle.bundles[i] in
# tests where we monkeypatch sigstore-python's Verifier to accept the test
# trust root.


@pytest.fixture
def crypto_factory():
    """Returns a factory object with helpers to mint test bundles and trust roots.

    The factory is implementation-defined in phase 3. The contract:

        factory.install_sign_monkeypatch(monkeypatch, *, provider=..., **opts)
            Patches the sigstore-python sign boundary so sign() can be driven
            offline. Recognised options control failure modes — see test bodies.
            Returns an optional spy object. If returned, the spy exposes
            fulcio_call_count so aud preflight tests can assert wrong-audience
            tokens are rejected before Fulcio is contacted. If the factory does
            not return a spy, it exposes the same count via
            factory.fulcio_call_count. Batch 6 adds named partial-failure modes
            for oracle finding-20260512-sr0w actionable #6:
            tuf_unavailable, rekor_after_fulcio_503,
            tsa_unavailable_rekor_v2, and oidc_token_expired_mid_flow. The
            optional spy may expose leaked_cert=False for the post-Fulcio Rekor
            failure assertion.

        factory.install_verify_monkeypatch(monkeypatch, **opts)
            Patches the sigstore-python verify boundary. Default is success;
            options drive specific VerifyStatus outcomes for testing. Batch 6
            names verify failure modes rekor_unreachable, rekor_timeout,
            tuf_unavailable_no_cached_root, and bundle_parse_error. The
            raise_sigstore_exception option takes a class-name string: real
            sigstore.errors classes are raised verbatim. Names not in the
            sigstore-python exception hierarchy (e.g. "UnrecognizedException")
            are synthesized as ad-hoc subclasses of Exception (NOT of
            sigstore.errors.VerificationError) carrying the supplied name,
            so tests can exercise the d3u6 §5 catch-all branch in
            _map_sigstore_exception under the broader "unrecognized exception
            class entirely" rule, not just the "VerificationError subclass we
            don't know" rule. Phase 3's catch-all must catch Exception (or
            equivalent), not only sigstore.errors.VerificationError.

        factory.make_staging_verifier() -> sigstore.verify.verifier.Verifier
            Returns an unpatched sigstore-python staging verifier configured
            with the factory's test trust root. Cross-implementation tests use
            this helper after install_sign_monkeypatch() so SKEIN.sign() can
            run offline while sigstore-python's verify_artifact() remains real.

        factory.make_bundle_blob(canonical_bytes, identity, issuer) -> str
            Returns sigstore-bundle ProtoJSON string for a bundle that will
            verify under the test trust root for the given (identity, issuer).
            Tests may pass cert=factory.make_cert_with_curve(...) to request a
            specific leaf certificate public-key algorithm.

        factory.make_bundle_blob_with_san(
            *, san_type: str, value: str, canonical_bytes, identity, issuer
        ) -> str
            Returns a bundle with a leaf cert SAN extension containing one
            caller-selected SAN variant. Supported san_type values:
            "rfc822Name", "uniformResourceIdentifier",
            "otherName_oid_57264_1_24".

        factory.make_bundle_blob_with_multiple_sans(
            *, sans: list[tuple[str, str]], canonical_bytes, identity, issuer
        ) -> str
            Returns a bundle with multiple SAN entries in one cert.

        factory.make_bundle_blob_with_missing_san(
            *, canonical_bytes, identity, issuer
        ) -> str
            Returns a bundle whose leaf cert has no SAN extension.

        factory.make_bundle_blob_with_malformed_ia5string_san(
            *, canonical_bytes, identity, issuer
        ) -> str
            Returns a bundle whose SAN IA5String bytes are malformed.

        factory.make_dsse_envelope_bundle(canonical_bytes) -> str
            Returns a bundle that uses dsseEnvelope (out-of-profile for SKEIN).

        factory.make_trust_root() -> TestTrustRoot
            Returns a TestTrustRoot object that, when monkeypatched in, makes
            the synthetic bundles verifiable.

        factory.make_era_trust_root(era: str, **opts) -> TestTrustRoot
            Returns a trust root for a named signing era. Options can select
            CA/Rekor/TSA key material and validity windows so tests can prove
            trust_root_pin selects the historical root instead of TUF-current.

        factory.trust_root_pin(root: TestTrustRoot) -> str
            Returns the content-addressable pin string for a trust root.

        factory.set_trust_root_pin(blob: str, pin_hash: str) -> str
            If a bundle format embeds a trust-root reference, sets it. SKEIN's
            folio-level SignatureBundle.trust_root_pin remains authoritative;
            tests pass both when useful so implementations cannot ignore either
            fixture seam by accident.

        factory.make_bundle_blob_with_rekor_inclusion(
            canonical_bytes, *, log_index, tree_size, leaf_hash, root_hash,
            hashes, checkpoint=None, log_id=None, identity=..., issuer=...
        ) -> str
            Returns a bundle whose Rekor inclusion proof is exactly the supplied
            Merkle witness. If checkpoint is None, the factory signs a matching
            C2SP signed-note checkpoint with the active test Rekor key.

        factory.make_bundle_blob_with_chain_length(
            *, chain_length: int, canonical_bytes, identity, issuer
        ) -> str
            Returns a bundle with a leaf chain of caller-selected length:
            0=self-signed leaf, 1=leaf+root, 2+=leaf+intermediates+root.

        factory.make_bundle_blob_with_broken_chain(
            *, canonical_bytes, identity, issuer
        ) -> str
            Returns a bundle whose leaf signs under an intermediate that does
            not chain to a trusted root.

        factory.make_checkpoint(*, tree_size, root_hash, log_id=None) -> str
            Returns a C2SP signed-note checkpoint binding tree_size and
            root_hash to the active test Rekor key.

        factory.parse_checkpoint_signed_note(checkpoint: str) -> ParsedCheckpoint
            Parses and verifies the signed-note checkpoint, returning at least
            tree_size and root_hash. Tests use this instead of string containment.

        factory.tamper_signature(blob: str) -> str
            ECDSA signature bytes replaced by all-zero bytes of same length.

        factory.set_digest_algorithm(blob: str, algo: str) -> str
            Rewrites messageSignature.messageDigest.algorithm without changing
            the digest bytes. Tests use this to enforce the sigstore-public-v1
            SHA-256 profile and reject SHA1/MD5/SHA384/SHA512 declarations.

        factory.tamper_cert(blob: str) -> str
            One byte of the Fulcio cert flipped.

        factory.make_cert_with_curve(curve: str) -> TestCertificate
            Returns a synthetic Fulcio leaf certificate whose public key uses
            the requested algorithm/curve. Recognized curve labels include
            "P-256", "P-384", "Ed25519", and "RSA-2048".

        factory.set_leaf_cert(blob: str, cert: TestCertificate) -> str
            Replaces the bundle's leaf certificate with cert and updates any
            internally linked synthetic fixture material needed for verification.
            The signature bytes remain those from the original bundle unless
            the factory's test-root implementation requires a matching rewrite.

        factory.tamper_merkle_hashes(blob: str) -> str
            One sibling hash in inclusionProof.hashes flipped.

        factory.strip_rekor_proof(blob: str) -> str
            Removes the inclusion proof.

        factory.strip_checkpoint(blob: str) -> str
            Removes the signed-note checkpoint, keeps Merkle hashes.

        factory.set_rekor_hashes(blob: str, hashes: list[str]) -> str
            Replaces inclusionProof.hashes with the supplied list.

        factory.set_rekor_root_hash(blob: str, root_hash: str) -> str
            Replaces inclusionProof.root_hash without rewriting the checkpoint.

        factory.set_rekor_tree_size(blob: str, tree_size: int) -> str
            Replaces inclusionProof.tree_size.

        factory.alternate_rekor_log_id() -> str
            Returns the key identifier for a second trusted Rekor instance in
            the test trusted_root.json. The alternate log_id is distinct from
            the active Rekor instance used by default bundle/checkpoint helpers.

        factory.swap_rekor_log_id(blob: str, new_log_id: str) -> str
            Replaces only inclusionProof.log_id with new_log_id, preserving the
            original Merkle proof and checkpoint signature material.

        factory.tamper_checkpoint_signature(blob: str) -> str
            Flips one bit in the C2SP checkpoint signature while preserving the
            signed tree_size/root_hash text.

        factory.checkpoint_with_different_root(blob: str) -> str
            Replaces the checkpoint with a valid signed note whose root_hash
            differs from inclusionProof.root_hash.

        factory.checkpoint_with_different_tree_size(blob: str) -> str
            Replaces the checkpoint with a valid signed note whose tree_size
            differs from inclusionProof.tree_size.

        factory.checkpoint_signed_by_old_rekor_key(blob: str) -> str
            Replaces the checkpoint with a signed note from a rotated-out Rekor
            key that is not active for the bundle's log_id/signing era.

        factory.malformed_checkpoint(blob: str, *, kind: str) -> str
            Replaces the checkpoint with syntactically malformed signed-note
            text, e.g. extra newlines or a missing origin line.

        factory.future_dated_cert(blob: str) -> str
            Sets cert notBefore later than integratedTime.

        factory.set_cert_validity(blob: str, *, not_before: int, not_after: int) -> str
            Rewrites the Fulcio leaf certificate validity window and keeps the
            synthetic bundle internally consistent unless the test deliberately
            moves the verification time outside the window.

        factory.set_rekor_integrated_time(blob: str, ts: int) -> str
            Rewrites the Rekor integratedTime / inclusion proof time.

        factory.set_verify_time(ts: int) -> None
            Sets the verifier's wall-clock time for tests that distinguish
            sign-time validity from current-time expiration.

        factory.set_tsa_timestamp(blob: str, ts: int) -> str
            Replaces the RFC3161 timestamp list with one timestamp at ts.

        factory.add_tsa_timestamp(blob: str, ts: int) -> str
            Appends an RFC3161 timestamp at ts, preserving existing timestamps.

        factory.strip_tsa(blob: str) -> str
            Removes all RFC3161 timestamp material from the bundle while
            keeping the Rekor v2 inclusion material otherwise intact.

        factory.strip_sct(blob: str) -> str
            Removes the Fulcio SCT extension
            (OID 1.3.6.1.4.1.11129.2.4.2) from the leaf certificate.

        factory.tamper_sct(blob: str) -> str
            Flips one bit in the embedded SCT signature while keeping the SCT
            extension present and parseable.

        factory.cross_sct(blob: str) -> str
            Replaces the embedded SCT with one issued for a different leaf
            certificate, preserving shape but breaking the certificate binding.

        factory.untrusted_ct_log_sct(blob: str) -> str
            Replaces the embedded SCT with one signed by a CT log key absent
            from trusted_root.json's ctlogs.

        factory.truncate(blob: str, n: int) -> str
            Returns blob[:n].

        factory.bit_flip(blob: str, offset: int, bit: int = 0) -> str
            Flip one bit in the blob at the given byte offset.

        factory.splice_rekor_entry(*, host: str, source: str) -> str
            Builds a bundle with host's signature+cert and source's Rekor entry.
            (GHSA-whqx-f9j3-ch6m / CVE-2022-36056 class.)

    Tests using this fixture skip if signing._test_factory is absent so the
    phase-3 implementer can ship it incrementally.
    """
    factory = getattr(signing, "_test_factory", None)
    if factory is None:
        pytest.skip(
            "skein.signing._test_factory not present; phase-3 implementer "
            "must ship test crypto factory or this test is skipped."
        )
    return factory


# ---------------------------------------------------------------------------
# Conformance corpus
# ---------------------------------------------------------------------------
#
# Corpus lives at tests/conformance/corpus/ (vendored from sigstore-python's
# test/assets/, MIT-licensed). Tests skip if a specific file is absent.


def corpus_file(name: str) -> Path:
    """Return path to a corpus file, or skip if absent."""
    p = CORPUS_DIR / name
    if not p.exists():
        pytest.skip(
            f"conformance corpus file {name!r} not present at {p}; "
            f"populate from sigstore-python test/assets/ to run."
        )
    return p


@pytest.fixture
def corpus():
    return corpus_file


# ---------------------------------------------------------------------------
# Pytest config
# ---------------------------------------------------------------------------


def pytest_configure(config):
    # --run-interactive is registered at rootdir-level in tests/conftest.py so
    # the option is recognized regardless of the invocation path.
    config.addinivalue_line(
        "markers",
        "staging: integration test that hits live Sigstore staging "
        "(skipped unless SKEIN_TEST_SIGSTORE_LIVE=1)",
    )
    config.addinivalue_line(
        "markers",
        "interactive: requires a human-in-the-loop browser-driven OAuth2 "
        "handshake (localhost-redirect by default; out-of-band copy/paste "
        "with SKEIN_TEST_SIGSTORE_OIDC_OOB=1). Skipped unless "
        "--run-interactive is passed. Intended for one-time launch-readiness "
        "empirical runs, not CI.",
    )


def pytest_collection_modifyitems(config, items):
    skip_staging = pytest.mark.skip(
        reason="SKEIN_TEST_SIGSTORE_LIVE not set; live Sigstore staging tests skipped."
    )
    skip_interactive = pytest.mark.skip(
        reason="--run-interactive not passed; human-in-the-loop OIDC handshake required."
    )
    live = os.environ.get("SKEIN_TEST_SIGSTORE_LIVE") == "1"
    run_interactive = config.getoption("--run-interactive")
    for item in items:
        if "staging" in item.keywords and not live:
            item.add_marker(skip_staging)
        if "interactive" in item.keywords and not run_interactive:
            item.add_marker(skip_interactive)


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_signing_test_factory():
    """Reset signing._test_factory module-level state around every test.

    The factory's _sign_state / _verify_state are module-level and survive
    pytest-monkeypatch teardown. Without this, a test that exercises the
    production code path without calling install_*_monkeypatch reads stale
    state from the prior test — a latent test-order dependency. Reset on both
    entry and exit so neither the current nor the next test inherits state.
    """
    if signing is not None and hasattr(signing, "_test_factory"):
        signing._test_factory._reset()
    yield
    if signing is not None and hasattr(signing, "_test_factory"):
        signing._test_factory._reset()


@pytest.fixture(autouse=True)
def _conformance_staging_verifier(request, monkeypatch):
    """Route @conformance_staging tests through the staging trust root.

    Corpus bundles are signed against staging Sigstore (see CORPUS.md). The
    production verify() path builds Verifier.production(); on environments
    where production TUF resolves, that verifier rejects the staging-rooted
    leaf with CertValidationError → CERT_INVALID. Per CORPUS.md these tests
    are meant to verify against Verifier.staging(offline=True).

    This patches only the boundary helper, only for tests bearing the
    conformance_staging mark — it does NOT swap the production verifier for
    all callers. Tests that subsequently call install_verify_monkeypatch
    re-patch the same target (later setattr wins), so the synthetic-verifier
    paths are unaffected.
    """
    if request.node.get_closest_marker("conformance_staging") is None:
        return
    if signing is None or not HAS_FUNCTIONS:
        return
    from sigstore.verify import Verifier

    monkeypatch.setattr(
        "skein.signing._build_production_verifier",
        lambda *, offline=False: Verifier.staging(offline=True),
    )
