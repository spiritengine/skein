"""SigningUnavailable contract — the boundary exception that the offline-write
queue catches.

Identity rev 5 §"Boundary placement": signing.py raises SigningUnavailable when
Fulcio, Rekor, or OIDC is reachable-but-failing OR unreachable. The offline-write
queue catches this and enqueues the write. This is the ONLY exception type that
crosses the signing.py boundary for sign-path domain failures — every other
failure path returns a VerifyResult with a status.

(EmptySignatureBundle and MultiSignerBundle are caller programming errors, not
domain failures — covered in test_models.py / test_verify.py.)

A wrapper that lets raw FulcioClientError / RekorClientError / TUFError leak
breaks the sign_queue.py catch-and-defer logic. These tests pin the boundary.
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
# All sign-path failures translate to SigningUnavailable
# ---------------------------------------------------------------------------


# Enforces: any sigstore-python exception during sign() is caught and re-raised
# as SigningUnavailable. The wrapper is the ONE place these get translated;
# callers (routes.py, sign_queue.py) MUST NOT need sigstore-python types.
@pytest.mark.parametrize(
    "simulated_failure",
    [
        "fulcio_503",
        "fulcio_400_unknown_csr",
        "rekor_502",
        "rekor_timeout",
        "rekor_404",
        "oidc_unreachable",
        "oidc_token_rejected",
        "tuf_metadata_stale",
        "expired_cert",
        "network_down",
    ],
)
def test_sign_failure_classes_raise_signing_unavailable(
    crypto_factory,
    google_provider,
    canonical_bytes_simple,
    monkeypatch,
    simulated_failure,
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode=simulated_failure,
    )
    with pytest.raises(signing.SigningUnavailable):
        signing.sign(canonical_bytes_simple, google_provider)


# Enforces: SigningUnavailable.component identifies the failing system.
@pytest.mark.parametrize(
    "failure_mode,expected_component",
    [
        ("fulcio_503", "fulcio"),
        ("fulcio_400_unknown_csr", "fulcio"),
        ("rekor_502", "rekor"),
        ("rekor_timeout", "rekor"),
        ("oidc_unreachable", "oidc"),
        ("oidc_token_rejected", "oidc"),
    ],
)
def test_signing_unavailable_component_is_correct(
    crypto_factory,
    google_provider,
    canonical_bytes_simple,
    monkeypatch,
    failure_mode,
    expected_component,
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode=failure_mode,
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == expected_component


# Enforces: oracle actionable #6. SigningUnavailable.component has a stable
# operations vocabulary for the systems on call will triage.
@pytest.mark.parametrize(
    "component",
    [
        "tuf",
        "oidc",
        "fulcio",
        "rekor",
        "tsa",
        "network",
        "token_expired_mid_flow",
    ],
)
def test_signing_unavailable_component_vocabulary(component):
    err = signing.SigningUnavailable(f"{component} unavailable", component=component)
    assert err.component == component
    assert isinstance(err.reason, str)


# Enforces: oracle actionable #6. Named partial-failure cases map to structured
# SigningUnavailable components instead of collapsing to a generic network error.
@pytest.mark.parametrize(
    "failure_mode,expected_component,reason_fragment",
    [
        ("tuf_unavailable", "tuf", "tuf"),
        ("oidc_unreachable", "oidc", "oidc"),
        ("fulcio_503", "fulcio", "fulcio"),
        ("rekor_after_fulcio_503", "rekor", "rekor"),
        ("tsa_unavailable_rekor_v2", "tsa", "tsa"),
        ("oidc_token_expired_mid_flow", "token_expired_mid_flow", "expired"),
    ],
)
def test_sign_unavailable_partial_failure_matrix(
    crypto_factory,
    google_provider,
    canonical_bytes_simple,
    monkeypatch,
    failure_mode,
    expected_component,
    reason_fragment,
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode=failure_mode,
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)

    assert exc.value.component == expected_component
    assert reason_fragment in exc.value.reason.lower()
    assert exc.value.__cause__ is None or isinstance(exc.value.__cause__, Exception)


# Enforces: oracle actionable #6. If Fulcio issued a certificate but Rekor then
# refuses inclusion, sign() still raises with component="rekor" and does not
# return or expose the cert-bearing partial bundle.
def test_sign_unavailable_when_rekor_fails_after_fulcio_does_not_leak_cert(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    spy = crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode="rekor_after_fulcio_503",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)

    assert exc.value.component == "rekor"
    leaked = getattr(spy, "leaked_cert", None)
    assert leaked in (None, False)


# Enforces: oracle actionable #6. A token that passes preflight but is rejected
# by Fulcio as expired mid-flow is distinguishable from local expires_at gating.
def test_sign_unavailable_when_oidc_token_expires_mid_flow(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode="oidc_token_expired_mid_flow",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert exc.value.component == "token_expired_mid_flow"
    assert "expired" in exc.value.reason.lower()


# Enforces: SigningUnavailable.reason is a non-empty human-readable string.
def test_signing_unavailable_reason_is_non_empty(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode="fulcio_503",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    assert isinstance(exc.value.reason, str)
    assert len(exc.value.reason) > 0


# Enforces: SigningUnavailable wraps the original sigstore-python exception via
# __cause__ per clarification 5: "SigningUnavailable with a structured reason
# and the original exception as __cause__".
def test_signing_unavailable_preserves_cause(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode="fulcio_503",
    )
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, google_provider)
    # The wrapper SHOULD set __cause__ to the original sigstore-python exception
    # for debugging; not strictly required but strongly recommended.
    assert exc.value.__cause__ is None or isinstance(exc.value.__cause__, Exception)


# Enforces: raw sigstore-python / cryptography / requests exceptions DO NOT
# leak. The wrapper translates everything. Load-bearing assertion — if it
# fails, the queue logic is potentially miswired.
@pytest.mark.parametrize(
    "failure_mode",
    [
        "fulcio_503",
        "rekor_502",
        "oidc_unreachable",
        "tuf_metadata_stale",
        "expired_cert",
        "network_down",
    ],
)
def test_no_raw_exception_leaks_from_sign(
    crypto_factory,
    google_provider,
    canonical_bytes_simple,
    monkeypatch,
    failure_mode,
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode=failure_mode,
    )
    try:
        signing.sign(canonical_bytes_simple, google_provider)
    except signing.SigningUnavailable:
        pass
    except Exception as e:
        pytest.fail(
            f"raw {type(e).__module__}.{type(e).__name__} leaked from sign() "
            f"for failure_mode={failure_mode!r}; expected SigningUnavailable"
        )
    else:
        pytest.fail(f"sign() did not raise under failure_mode={failure_mode!r}")


# ---------------------------------------------------------------------------
# verify() does NOT raise SigningUnavailable
# ---------------------------------------------------------------------------


# Enforces: SigningUnavailable is a SIGN-path-only contract. verify() returns
# a VerifyResult; it does not raise SigningUnavailable.
def test_verify_does_not_raise_signing_unavailable(
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


# Enforces: oracle actionable #6. Verify maps network/proof failure classes to
# specific VerifyStatus values; a loose "VerifyResult returned" is insufficient.
@pytest.mark.parametrize(
    "verify_failure,expected_status",
    [
        ("rekor_unreachable", signing.VerifyStatus.INCLUSION_FAILED),
        ("rekor_timeout", signing.VerifyStatus.INCLUSION_FAILED),
        (
            "tuf_unavailable_no_cached_root",
            signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT,
        ),
        ("bundle_parse_error", signing.VerifyStatus.BUNDLE_MALFORMED),
    ],
)
def test_verify_status_specific_per_failure_class(
    crypto_factory,
    google_provider,
    canonical_bytes_simple,
    monkeypatch,
    verify_failure,
    expected_status,
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        failure_mode=verify_failure,
    )
    result = signing.sign(canonical_bytes_simple, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )

    vr = signing.verify(canonical_bytes_simple, sb)
    assert vr.status == expected_status


# Enforces: oracle actionable #6. Rekor proof re-verification outages are an
# inclusion failure when a trusted root is otherwise available.
def test_verify_returns_inclusion_failed_when_rekor_unreachable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(
        monkeypatch,
        failure_mode="rekor_unreachable",
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
# SigningUnavailable is catchable as Exception (sanity)
# ---------------------------------------------------------------------------


# Enforces: SigningUnavailable is a subclass of Exception (not BaseException).
# Generic `except Exception` MUST catch it. The queue uses this.
def test_signing_unavailable_caught_by_generic_exception(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode="fulcio_503",
    )
    try:
        signing.sign(canonical_bytes_simple, google_provider)
    except Exception as e:
        assert isinstance(e, signing.SigningUnavailable)
    else:
        pytest.fail("sign() did not raise")


# ---------------------------------------------------------------------------
# BaseException propagation (KeyboardInterrupt / SystemExit)
# ---------------------------------------------------------------------------


# Enforces: sign() catches Exception, NOT BaseException. KeyboardInterrupt must
# propagate so Ctrl-C during a slow Fulcio call aborts the process rather than
# being converted into SigningUnavailable. Oracle B-review O-A3.
def test_keyboard_interrupt_propagates_from_sign(
    google_provider, canonical_bytes_simple, monkeypatch
):
    def _raise_keyboard_interrupt():
        raise KeyboardInterrupt

    monkeypatch.setattr(
        signing,
        "_build_production_signing_context",
        _raise_keyboard_interrupt,
    )
    with pytest.raises(KeyboardInterrupt):
        signing.sign(canonical_bytes_simple, google_provider)


# Enforces: sign() catches Exception, NOT BaseException. SystemExit must
# propagate rather than being wrapped in SigningUnavailable. Oracle B-review O-A3.
def test_system_exit_propagates_from_sign(
    google_provider, canonical_bytes_simple, monkeypatch
):
    def _raise_system_exit():
        raise SystemExit(2)

    monkeypatch.setattr(
        signing,
        "_build_production_signing_context",
        _raise_system_exit,
    )
    with pytest.raises(SystemExit):
        signing.sign(canonical_bytes_simple, google_provider)


# ---------------------------------------------------------------------------
# Reachable-but-failing distinction
# ---------------------------------------------------------------------------


# Enforces: spec rev 5 explicitly names "Fulcio, Rekor, or OIDC reachable but
# the flow cannot complete" — this includes 4xx errors (token rejected, CSR
# rejected, identity not found). They are SigningUnavailable, not ValueError.
def test_reachable_but_4xx_raises_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode="fulcio_400_unknown_csr",
    )
    with pytest.raises(signing.SigningUnavailable):
        signing.sign(canonical_bytes_simple, google_provider)


# Enforces: unreachable (DNS / connection refused / timeout) ALSO raises
# SigningUnavailable. The queue cannot distinguish from a 5xx; both mean
# "try again later."
def test_unreachable_raises_signing_unavailable(
    crypto_factory, google_provider, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(
        monkeypatch,
        provider=google_provider,
        failure_mode="network_down",
    )
    with pytest.raises(signing.SigningUnavailable):
        signing.sign(canonical_bytes_simple, google_provider)


# ---------------------------------------------------------------------------
# Distinct from caller programming errors
# ---------------------------------------------------------------------------


# Enforces: SigningUnavailable is a domain failure; EmptySignatureBundle and
# MultiSignerBundle are caller programming errors. They are distinct classes
# (clarification 2: "caller programming errors, not domain failures").
def test_signing_unavailable_is_distinct_from_caller_errors():
    assert not issubclass(signing.EmptySignatureBundle, signing.SigningUnavailable)
    assert not issubclass(signing.MultiSignerBundle, signing.SigningUnavailable)
    assert not issubclass(signing.SigningUnavailable, signing.EmptySignatureBundle)
    assert not issubclass(signing.SigningUnavailable, signing.MultiSignerBundle)


# ---------------------------------------------------------------------------
# Sign-path identity extraction must fail closed (residuals N + O from
# F-closure finding-20260520-j4w4 / brief-20260520-x2nj).
#
# The verify path at signing.py:1245-1263 rejects a leaf cert whose issuer
# or subject extraction returns falsy with VerifyStatus.CERT_INVALID. The
# sign path was emitting SignResult.subject="" (N) or SignResult.issuer
# falling back to oidc_provider.issuer (O) — the token's *claimed* issuer
# rather than what is embedded in the cert. Same cert that makes verify()
# reject was producing a successful sign(): sign/verify semantic divergence.
# Both paths now fail closed at the cert-extraction boundary by raising
# SigningUnavailable with component="fulcio".
# ---------------------------------------------------------------------------


class TestSignPathSubjectExtractionFailsClosed:
    """N: empty or unextractable subject on the sign path must raise
    SigningUnavailable, not return SignResult with subject=''.

    Same shape as test_san_extraction.py::TestVerifySingleRejectsMissingIssuer
    on the verify side — patch the extraction helper to simulate a leaf cert
    whose SAN extraction policy yields a falsy value (missing-SAN, malformed
    IA5String, empty-SAN per finding-20260514-burb).
    """

    @pytest.mark.parametrize("falsy_value", [None, ""])
    def test_falsy_subject_raises_signing_unavailable(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
        falsy_value,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        monkeypatch.setattr(
            signing, "_extract_subject_from_cert", lambda cert: falsy_value
        )
        with pytest.raises(signing.SigningUnavailable) as exc:
            signing.sign(canonical_bytes_simple, google_provider)
        assert exc.value.component == "fulcio"
        assert exc.value.reason == "Fulcio issued cert with empty or malformed SAN"

    def test_normal_subject_still_signs(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(
            monkeypatch,
            provider=google_provider,
            identity="alice@example.com",
        )
        result = signing.sign(canonical_bytes_simple, google_provider)
        assert isinstance(result, signing.SignResult)
        assert result.subject == "alice@example.com"


class TestSignPathIssuerExtractionFailsClosed:
    """O: unextractable issuer on the sign path must raise
    SigningUnavailable, not silently fall back to oidc_provider.issuer
    (the token's *claimed* issuer rather than the cert-embedded one).

    Same shape as the V2/legacy OID extraction tests in test_san_extraction
    — patch _extract_issuer_from_cert to simulate a cert where both V2
    OID 1.3.6.1.4.1.57264.1.8 and legacy 1.3.6.1.4.1.57264.1.1 are absent,
    empty, or malformed (the shapes hardened by R4-1 / R4-2 / F-fix-#9 on
    the verify side).
    """

    @pytest.mark.parametrize("falsy_value", [None, ""])
    def test_falsy_issuer_raises_signing_unavailable(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
        falsy_value,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        monkeypatch.setattr(
            signing, "_extract_issuer_from_cert", lambda cert: falsy_value
        )
        with pytest.raises(signing.SigningUnavailable) as exc:
            signing.sign(canonical_bytes_simple, google_provider)
        assert exc.value.component == "fulcio"
        assert (
            exc.value.reason
            == "Fulcio issued cert without extractable OIDC issuer extension"
        )

    def test_falsy_issuer_does_not_fall_back_to_token_claim(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        # Pre-fix behavior: SignResult.issuer = oidc_provider.issuer when
        # cert extraction returned falsy. Downstream policy comparing against
        # an allowlist would see unverified data. After fix: sign() raises
        # and never returns a SignResult at all.
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        monkeypatch.setattr(signing, "_extract_issuer_from_cert", lambda cert: None)
        with pytest.raises(signing.SigningUnavailable):
            signing.sign(canonical_bytes_simple, google_provider)

    def test_normal_issuer_still_signs(
        self,
        crypto_factory,
        google_provider,
        canonical_bytes_simple,
        monkeypatch,
    ):
        crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
        result = signing.sign(canonical_bytes_simple, google_provider)
        assert isinstance(result, signing.SignResult)
        assert result.issuer == "https://accounts.google.com"
