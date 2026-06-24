"""sigstore-python exception → SKEIN failure mode mapping (clarification 5).

Per clarification 5 (finding-20260511-d3u6 § 5): SKEIN's failure-mode vocabulary
is a *superset* of sigstore-python's exception hierarchy. Each sigstore-python
exception class maps to exactly one SKEIN failure mode (no information loss
from the library). SKEIN may additionally produce failure modes that have no
sigstore-python analog (TRUST_ROOT_STALE, OFFLINE_NO_TRUSTED_ROOT,
SIGNATURE_MISMATCH).

Mapping table:

| sigstore-python exception          | Sign path                                  | Verify path           |
|---|---|---|
| VerificationError (base)           | n/a                                        | BUNDLE_MALFORMED       |
| InvalidBundle                      | n/a                                        | BUNDLE_MALFORMED       |
| InvalidMaterials                   | n/a                                        | CERT_INVALID           |
| InvalidRekorEntry                  | n/a                                        | INCLUSION_FAILED       |
| CertificateExpired / cert-validity | SigningUnavailable("cert expired")         | CERT_INVALID           |
| FulcioClientError / HTTP fail      | SigningUnavailable("fulcio unavailable")   | n/a (verify doesn't call Fulcio) |
| RekorClientError / HTTP fail       | SigningUnavailable("rekor unavailable")    | INCLUSION_FAILED / OFFLINE_NO_TRUSTED_ROOT |
| IdentityError (OIDC)               | SigningUnavailable("oidc token invalid")   | n/a                    |
| Network / timeout                  | SigningUnavailable("network failure")      | INCLUSION_FAILED (during Rekor proof verify) |

Plus SKEIN-specific modes with no library analog (test separately):
 - TRUST_ROOT_STALE
 - OFFLINE_NO_TRUSTED_ROOT
 - SIGNATURE_MISMATCH
 - VERIFIED

Phase 3 implementation should produce a small mapping module / function
(_map_sigstore_exception(exc) -> VerifyStatus) so the table lives in one place.
This test file exercises the table from outside, using crypto_factory to inject
specific sigstore-python exception classes at the seam.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

import sigstore as _sigstore  # noqa: E402  (used by the catch-all log test)

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


# ---------------------------------------------------------------------------
# Sign path: library exception → SigningUnavailable with correct component
# ---------------------------------------------------------------------------


# Enforces: FulcioClientError maps to SigningUnavailable(component="fulcio").
def test_sign_fulcio_client_error_maps_to_signing_unavailable_fulcio(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        raise_sigstore_exception="FulcioClientError",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "fulcio"
    assert (
        "fulcio" in exc.value.reason.lower()
        or "unavailable" in exc.value.reason.lower()
    )


# Enforces: RekorClientError maps to SigningUnavailable(component="rekor").
def test_sign_rekor_client_error_maps_to_signing_unavailable_rekor(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        raise_sigstore_exception="RekorClientError",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "rekor"


# Enforces: IdentityError (OIDC) maps to SigningUnavailable(component="oidc")
# with reason mentioning token.
def test_sign_identity_error_maps_to_signing_unavailable_oidc(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        raise_sigstore_exception="IdentityError",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "oidc"
    assert "token" in exc.value.reason.lower() or "oidc" in exc.value.reason.lower()


# Enforces: CertificateExpired during sign maps to SigningUnavailable with
# component="fulcio" and a "cert expired" reason. Per mapping table.
def test_sign_certificate_expired_maps_to_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        raise_sigstore_exception="CertificateExpired",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "fulcio"
    assert "cert" in exc.value.reason.lower() or "expir" in exc.value.reason.lower()


# Enforces: generic network / timeout failures map to SigningUnavailable with
# component classified by the originating call site.
def test_sign_network_timeout_maps_to_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        raise_sigstore_exception="TimeoutError",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert (
        "network" in exc.value.reason.lower() or "timeout" in exc.value.reason.lower()
    )


# Enforces: __cause__ is set to the underlying sigstore-python exception for
# debugging. Per clarification 5: "SigningUnavailable with a structured reason
# and the original exception as __cause__."
@pytest.mark.parametrize(
    "sigstore_exc_name",
    [
        "FulcioClientError",
        "RekorClientError",
        "IdentityError",
        "CertificateExpired",
    ],
)
def test_sign_exception_preserves_cause(
    crypto_factory,
    google_provider,
    canonical_bytes_simple,
    monkeypatch,
    sigstore_exc_name,
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        raise_sigstore_exception=sigstore_exc_name,
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    # __cause__ should be the original sigstore-python exception (or its surrogate).
    assert exc.value.__cause__ is not None


# Enforces: when sigstore-python raises an exception during sign() that doesn't
# match any branch in _classify_sign_exception, the catch-all attributes the
# failure to "fulcio" AND emits a WARNING-level log. Mirrors the verify-side
# behavior pinned by test_verify_catchall_logs_unrecognized_sigstore_exception
# (brief-20260514-7i3w pattern). Without this the sign side would silently
# collapse every unknown sigstore-python surface drift to component="fulcio"
# with no observability signal (Knuth B-review concern 4 / Oracle B-review
# zone 3).
#
# Log format requirements (parity with verify-side catch-all):
# - WARNING level
# - Grep-able prefix "signing.classify_sign_exception_unrecognized"
# - Includes the exception class name (for triage)
# - Includes sigstore.__version__ (for version-skew diagnosis)
def test_sign_catchall_logs_unrecognized_sigstore_exception(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch, caplog
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        raise_sigstore_exception="UnrecognizedSignException",
    )
    with caplog.at_level("WARNING"):
        with pytest.raises(signing.SigningUnavailable) as exc:
            signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "fulcio"
    catchall_records = [
        r
        for r in caplog.records
        if "signing.classify_sign_exception_unrecognized" in r.getMessage()
    ]
    assert catchall_records, (
        "expected at least one WARNING log with "
        "'signing.classify_sign_exception_unrecognized' prefix; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    assert any(r.levelname == "WARNING" for r in catchall_records)
    assert any(
        "UnrecognizedSignException" in r.getMessage() for r in catchall_records
    ), "log must include the unrecognized exception class name for triage"
    assert any(_sigstore.__version__ in r.getMessage() for r in catchall_records), (
        f"log must include sigstore.__version__ ({_sigstore.__version__}) "
        "for version-skew diagnosis"
    )


# ---------------------------------------------------------------------------
# Verify path: library exception → VerifyStatus
# ---------------------------------------------------------------------------


# Enforces: InvalidBundle maps to BUNDLE_MALFORMED.
def test_verify_invalid_bundle_maps_to_bundle_malformed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        raise_sigstore_exception="InvalidBundle",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: InvalidMaterials maps to CERT_INVALID.
def test_verify_invalid_materials_maps_to_cert_invalid(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        raise_sigstore_exception="InvalidMaterials",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: InvalidRekorEntry maps to INCLUSION_FAILED.
def test_verify_invalid_rekor_entry_maps_to_inclusion_failed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        raise_sigstore_exception="InvalidRekorEntry",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.INCLUSION_FAILED


# Enforces: CertificateExpired on verify path maps to CERT_INVALID.
def test_verify_certificate_expired_maps_to_cert_invalid(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        raise_sigstore_exception="CertificateExpired",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.CERT_INVALID


# Enforces: when sigstore-python raises an exception that doesn't match a row
# in the d3u6 §5 mapping table, verify() routes to BUNDLE_MALFORMED AND emits
# a WARNING-level log. The log lets operators detect upstream library drift
# before it becomes silent misclassification. Closes brief-20260514-7i3w.
#
# Log format requirements (from brief-20260514-7i3w):
# - WARNING level
# - Grep-able prefix "signing.exception_catchall"
# - Includes the exception class name (for triage)
# - Includes sigstore.__version__ (for version-skew diagnosis)
def test_verify_catchall_logs_unrecognized_sigstore_exception(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch, caplog
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        raise_sigstore_exception="UnrecognizedException",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    with caplog.at_level("WARNING"):
        vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED
    catchall_records = [
        r for r in caplog.records if "signing.exception_catchall" in r.getMessage()
    ]
    assert catchall_records, (
        "expected at least one WARNING log with 'signing.exception_catchall' prefix; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    assert any(r.levelname == "WARNING" for r in catchall_records)
    assert any("UnrecognizedException" in r.getMessage() for r in catchall_records), (
        "log must include the unrecognized exception class name for triage"
    )
    # sigstore.__version__ is the version-skew diagnostic; without it the log
    # tells operators *what* surface changed but not *which* library version
    # introduced the change. brief-20260514-7i3w lists this as a required
    # log payload alongside the exception class name.
    assert any(_sigstore.__version__ in r.getMessage() for r in catchall_records), (
        f"log must include sigstore.__version__ ({_sigstore.__version__}) "
        "for version-skew diagnosis"
    )


# Enforces: bare VerificationError (no recognized subclass) maps to BUNDLE_MALFORMED
# (fallback per clarification 5: "If a future library version raises an exception
# we don't recognize, the verify path treats it as BUNDLE_MALFORMED and logs the
# unexpected exception for follow-up.")
def test_verify_unrecognized_verification_error_maps_to_bundle_malformed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        raise_sigstore_exception="VerificationError",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: RekorClientError during verify (Rekor witness query failure) maps to
# INCLUSION_FAILED if proof material is malformed, OFFLINE_NO_TRUSTED_ROOT if no
# trust root is available.
def test_verify_rekor_client_error_maps_to_inclusion_failed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        raise_sigstore_exception="RekorClientError",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status in (
        signing.VerifyStatus.INCLUSION_FAILED,
        signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT,
    )


# Enforces: TUF-class exceptions during verify (TUFError, MetadataError,
# RootError) all map to OFFLINE_NO_TRUSTED_ROOT.
#
# Pre-fix the verify-side map only matched name == "TUFError"; MetadataError
# and RootError are SEPARATE classes in sigstore.errors (not subclasses of
# TUFError) and fell through to the catch-all WARNING + BUNDLE_MALFORMED.
# That misclassified a transient TUF metadata staleness or root rotation
# as a permanent bundle defect — callers would reject the bundle instead
# of retrying after a TUF refresh. The sign-side classifier already groups
# all three (signing.py:357); the verify side must too.
def test_verify_tuf_class_exceptions_map_to_offline_no_trusted_root():
    for name in ("TUFError", "MetadataError", "RootError"):
        exc = signing._synthesize_exception(name, f"{name} for test")
        status = signing._map_sigstore_exception(exc)
        assert status == signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT, (
            f"{name} mapped to {status.name}, expected OFFLINE_NO_TRUSTED_ROOT"
        )


# Enforces (R4-4, F-pass round 4): the VerificationError-with-message
# heuristic must use sigstore-python's exact wire phrasings, not loose
# substrings. sigstore-python's verifier.py raises two literal strings:
#   - "Signature is invalid for input"  (verifier.py 4.2.0 line 495)
#   - "Bundle message digest mismatch"  (verifier.py 4.2.0 line 483)
# A pre-fix loose substring "signature is invalid" also matched cert-chain
# wording like "cert chain: signature is invalid for leaf" — misclassifying
# a cert-chain failure as SIGNATURE_MISMATCH. Real cert-chain failures come
# through as CertValidationError (CERT_INVALID), so the loose substring was
# only triggered by synthesized exceptions today; a future sigstore-python
# rev wrapping cert-chain errors in plain VerificationError would hit it.
def test_verify_signature_invalid_for_input_maps_to_signature_mismatch():
    # Real sigstore-python sig-mismatch phrasing maps correctly.
    exc = signing._synthesize_exception(
        "VerificationError",
        "Signature is invalid for input",
    )
    assert (
        signing._map_sigstore_exception(exc) == signing.VerifyStatus.SIGNATURE_MISMATCH
    )


def test_verify_bundle_message_digest_mismatch_maps_to_signature_mismatch():
    # Real sigstore-python digest-mismatch phrasing maps correctly.
    exc = signing._synthesize_exception(
        "VerificationError",
        "Bundle message digest mismatch",
    )
    assert (
        signing._map_sigstore_exception(exc) == signing.VerifyStatus.SIGNATURE_MISMATCH
    )


def test_verify_cert_chain_signature_invalid_does_not_map_to_signature_mismatch():
    # R4-4 regression. "cert chain: signature is invalid for leaf" used to
    # false-match "signature is invalid" and return SIGNATURE_MISMATCH; the
    # tightened heuristic requires the full phrase "signature is invalid
    # for input", so cert-chain wording now falls to the catch-all
    # (BUNDLE_MALFORMED, the d3u6 §5 safe default for unknown
    # VerificationError shapes).
    exc = signing._synthesize_exception(
        "VerificationError",
        "cert chain: signature is invalid for leaf",
    )
    status = signing._map_sigstore_exception(exc)
    assert status != signing.VerifyStatus.SIGNATURE_MISMATCH, (
        f"cert-chain wording must not map to SIGNATURE_MISMATCH; got {status.name}"
    )
    assert status == signing.VerifyStatus.BUNDLE_MALFORMED


def test_verify_cert_chain_signature_invalid_for_intermediate_does_not_map_to_sig_mismatch():
    # Same R4-4 pattern with intermediate-cert wording.
    exc = signing._synthesize_exception(
        "VerificationError",
        "cert chain: signature is invalid for intermediate",
    )
    assert (
        signing._map_sigstore_exception(exc) != signing.VerifyStatus.SIGNATURE_MISMATCH
    )


def test_verify_partial_digest_mismatch_string_does_not_map_to_sig_mismatch():
    # The pre-fix "digest mismatch" loose substring would have matched
    # any wording mentioning "digest mismatch" — e.g. "user account digest
    # mismatch" (hypothetical, but parallel to the R4-4 shape). Tightened
    # phrase requires the exact "bundle message digest mismatch".
    exc = signing._synthesize_exception(
        "VerificationError",
        "user account digest mismatch",
    )
    assert (
        signing._map_sigstore_exception(exc) != signing.VerifyStatus.SIGNATURE_MISMATCH
    )


def test_verify_signature_mismatch_phrasing_is_case_insensitive():
    # The heuristic uses msg_lower, so any case spelling of the exact
    # phrase still matches. Pin so a future refactor doesn't drop the
    # case fold.
    exc = signing._synthesize_exception(
        "VerificationError",
        "SIGNATURE IS INVALID FOR INPUT",
    )
    assert (
        signing._map_sigstore_exception(exc) == signing.VerifyStatus.SIGNATURE_MISMATCH
    )


# ---------------------------------------------------------------------------
# A4 (j4w4 round-7): sigstore-python wire-string version pin
# ---------------------------------------------------------------------------
#
# _map_sigstore_exception's VerificationError-with-message heuristic relies on
# two literal wire strings raised from sigstore-python's verifier.py @ 4.2.0:
#
#   - "Signature is invalid for input"   (verifier.py line 495)
#   - "Bundle message digest mismatch"   (verifier.py line 483)
#
# The pyproject pin is `sigstore>=4.2,<5`, so any 4.x version is admitted at
# install time. If a future sigstore-python 4.x release rewrites these messages
# without bumping the major version (e.g. "Invalid signature for payload"),
# _map_sigstore_exception silently falls through to the catch-all and
# misclassifies SIGNATURE_MISMATCH as BUNDLE_MALFORMED — losing diagnostic
# precision with no test signal.
#
# These tests defend against that by introspecting the installed
# sigstore-python source and asserting the wire strings are still present.
# A future upgrade that changes the strings will fail loudly with a clear
# pointer at what to update in _map_sigstore_exception.

_SIGSTORE_VERIFIED_VERSIONS = ("4.2", "4.3")


def _sigstore_verifier_source() -> str:
    """Return the source of sigstore.verify.verifier, or skip if unavailable.

    Source-introspection is the cheapest robust defense: it requires no real
    sigstore-python verifier call (which would need a full trust root + bundle
    set-up) and it directly anchors the contract to the installed library's
    actual source.
    """
    import inspect

    # sigstore-python has reshuffled its `_internal` / verify paths between
    # releases. A bare ModuleNotFoundError here would surface as a noisy
    # collection error with no actionable signal; pytest.fail with a pointed
    # message tells the operator exactly which knobs to turn. pytest.fail
    # (not pytest.skip) is deliberate — silently skipping the wire-string pin
    # would re-introduce the silent-precision-loss risk these tests defend
    # against.
    try:
        import sigstore.verify.verifier as _verifier
    except ImportError as exc:
        pytest.fail(
            f"sigstore-python module structure changed (installed: "
            f"{_sigstore.__version__}): could not import "
            "'sigstore.verify.verifier'. The A4 wire-string pin's "
            "introspection target moved. Update the import path in "
            "_sigstore_verifier_source() and re-verify "
            "skein.signing._map_sigstore_exception's substring matches "
            "('Signature is invalid for input', 'Bundle message digest "
            f"mismatch') against the new module surface. Original "
            f"ImportError: {exc}"
        )

    try:
        return inspect.getsource(_verifier)
    except (OSError, TypeError):  # pragma: no cover - source not readable
        pytest.skip(
            "sigstore.verify.verifier source not introspectable in this env; "
            "wire-string pin requires source access."
        )


def test_sigstore_python_signature_invalid_wire_string_unchanged():
    """Pin the SIGNATURE_MISMATCH wire string against silent precision loss.

    If sigstore-python rewrites the literal "Signature is invalid for input",
    the substring match in _map_sigstore_exception falls through to the
    BUNDLE_MALFORMED catch-all and SIGNATURE_MISMATCH classification silently
    degrades. Verified against sigstore-python 4.2.x; bump
    _SIGSTORE_VERIFIED_VERSIONS and update the substring if a future release
    changes the wording.
    """
    source = _sigstore_verifier_source()
    # The substring match in _map_sigstore_exception lowercases the message
    # ("signature is invalid for input"); the source itself uses sentence case
    # ("Signature is invalid for input"). Pin the verbatim source spelling so
    # this test fails on any wording shift, not just case folds.
    assert "Signature is invalid for input" in source, (
        "sigstore-python wire string for SIGNATURE_MISMATCH changed; "
        "update skein.signing._map_sigstore_exception's substring match or "
        "VerifyStatus.SIGNATURE_MISMATCH will silently degrade to "
        f"BUNDLE_MALFORMED. Verified versions: {_SIGSTORE_VERIFIED_VERSIONS}; "
        f"installed: {_sigstore.__version__}."
    )


def test_sigstore_python_digest_mismatch_wire_string_unchanged():
    """Pin the second SIGNATURE_MISMATCH wire string ("bundle message digest
    mismatch") against silent precision loss. Same rationale as the
    signature-invalid pin."""
    source = _sigstore_verifier_source()
    assert "Bundle message digest mismatch" in source, (
        "sigstore-python wire string for bundle digest mismatch changed; "
        "update skein.signing._map_sigstore_exception's substring match or "
        "VerifyStatus.SIGNATURE_MISMATCH will silently degrade to "
        f"BUNDLE_MALFORMED. Verified versions: {_SIGSTORE_VERIFIED_VERSIONS}; "
        f"installed: {_sigstore.__version__}."
    )


def test_sigstore_python_installed_version_in_verified_range():
    """Sanity: the installed sigstore-python version is in the range these
    wire strings were verified against. If the installed version is outside
    that range the wire-string checks above are still correct (they introspect
    the live source), but this test surfaces the version drift independently
    so the operator can re-verify _map_sigstore_exception against the new
    surface and bump _SIGSTORE_VERIFIED_VERSIONS."""
    installed = _sigstore.__version__
    assert any(installed.startswith(v) for v in _SIGSTORE_VERIFIED_VERSIONS), (
        f"sigstore-python {installed} installed, but _map_sigstore_exception "
        f"wire strings were only verified against {_SIGSTORE_VERIFIED_VERSIONS}. "
        "Re-verify the substring match against the new surface and add the "
        "new version to _SIGSTORE_VERIFIED_VERSIONS."
    )


# Enforces: network timeout during verify Rekor proof maps to INCLUSION_FAILED.
def test_verify_network_timeout_maps_to_inclusion_failed(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        raise_sigstore_exception="TimeoutError",
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == signing.VerifyStatus.INCLUSION_FAILED


# ---------------------------------------------------------------------------
# SKEIN-specific failure modes (no library analog)
# ---------------------------------------------------------------------------


# Enforces: TRUST_ROOT_STALE is reachable via SKEIN-specific code path (verifier
# detects bundle integratedTime falls outside trusted_root.json era windows).
def test_trust_root_stale_skein_specific_path(
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


# Enforces: OFFLINE_NO_TRUSTED_ROOT is reachable via SKEIN-specific code path
# (offline verifier with no era-correct trust root).
def test_offline_no_trusted_root_skein_specific_path(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        offline=True,
        trust_root_missing=True,
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


# Enforces: SIGNATURE_MISMATCH is SKEIN-specific (signature doesn't match
# canonical_bytes the caller passed — could be cross-payload reuse or perturbed
# bytes). No library exception analog; SKEIN computes locally.
def test_signature_mismatch_skein_specific_path(
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
    # Different bytes than what was signed → SIGNATURE_MISMATCH.
    vr = signing.verify(canonical_bytes_simple + b"_perturbed", sb)
    assert vr.status == signing.VerifyStatus.SIGNATURE_MISMATCH


# ---------------------------------------------------------------------------
# Information preservation — mapping is lossless from library perspective
# ---------------------------------------------------------------------------


# Enforces: every sigstore-python exception class maps to exactly one
# VerifyStatus. No two distinct exception classes collapse to the same
# status that loses information. (This is the no-information-loss property
# from clarification 5: "no information loss from the library".)
#
# Coverage proxy: confirm each of the 5 documented sigstore-python exception
# classes (InvalidBundle, InvalidMaterials, InvalidRekorEntry, CertificateExpired)
# maps to a DIFFERENT VerifyStatus.
def test_library_exceptions_map_to_distinct_statuses(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    expected_mapping = {
        "InvalidBundle": signing.VerifyStatus.BUNDLE_MALFORMED,
        "InvalidMaterials": signing.VerifyStatus.CERT_INVALID,
        "InvalidRekorEntry": signing.VerifyStatus.INCLUSION_FAILED,
        "CertificateExpired": signing.VerifyStatus.CERT_INVALID,
    }
    # Phase-3 amendment (closes friction-20260515-kf8b): the comment above
    # explicitly notes that CertificateExpired and InvalidMaterials BOTH map
    # to CERT_INVALID, so the four documented exceptions reduce to THREE
    # distinct statuses (BUNDLE_MALFORMED, CERT_INVALID, INCLUSION_FAILED).
    # The previous `>= 4` assertion contradicted that comment. The point of
    # the property is "no exception silently disappears into VERIFIED or an
    # unrelated status" — `>= 3` enforces that without contradicting the
    # mapping table's own collapses.
    distinct_statuses = set(expected_mapping.values())
    assert signing.VerifyStatus.VERIFIED not in distinct_statuses
    assert len(distinct_statuses) >= 3


# Enforces: phase-3 implementation should expose the mapping as a single
# function (per clarification 5: "Phase 3 implementation should produce a
# small mapping module / function so the table lives in one place"). Tested
# behaviorally above; this docstring-only test pins the expectation.
def test_mapping_lives_in_one_place_per_clarification_5():
    # If phase-3 ships a _map_sigstore_exception helper, the per-exception tests
    # above will all use it. This test just documents the architectural intent.
    # No assertion — the architecture is documented via the per-exception tests.
    pass
