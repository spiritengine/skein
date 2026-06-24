"""sign() OIDC aud validation contract.

finding-20260513-tx8r is normative: v0 Google and GitHub personal-OAuth tokens
must carry aud="sigstore" before sign() sends them to Fulcio.
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


def _fulcio_call_count(crypto_factory, spy) -> int:
    if spy is not None and hasattr(spy, "fulcio_call_count"):
        return spy.fulcio_call_count
    return getattr(crypto_factory, "fulcio_call_count", 0)


# Enforces: finding-20260513-tx8r. sign() rejects a JWT whose string aud is not
# "sigstore" and reports the actual value before Fulcio sees the token.
def test_sign_rejects_token_with_wrong_aud_string(
    crypto_factory, make_oidc_provider, canonical_bytes_simple, monkeypatch
):
    provider = make_oidc_provider(aud="https://api.github.com")
    spy = crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)

    with pytest.raises(
        signing.SigningUnavailable,
        match=r"OIDC token aud is https://api\.github\.com, expected sigstore",
    ):
        signing.sign(canonical_bytes_simple, provider)

    assert _fulcio_call_count(crypto_factory, spy) == 0


# Enforces: finding-20260513-tx8r. The required v0 aud value is the literal
# string "sigstore"; this is valid for both supported providers.
def test_sign_accepts_token_with_aud_sigstore_string(
    crypto_factory, make_oidc_provider, canonical_bytes_simple, monkeypatch
):
    provider = make_oidc_provider(aud="sigstore")
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)

    result = signing.sign(canonical_bytes_simple, provider)

    assert isinstance(result, signing.SignResult)


# Enforces: finding-20260513-tx8r edge case. OIDC permits multi-audience tokens;
# sign() accepts an aud array when one member is "sigstore".
def test_sign_accepts_token_with_aud_array_containing_sigstore(
    crypto_factory, make_oidc_provider, canonical_bytes_simple, monkeypatch
):
    provider = make_oidc_provider(aud=["sigstore", "other"])
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)

    result = signing.sign(canonical_bytes_simple, provider)

    assert isinstance(result, signing.SignResult)


# Enforces: finding-20260513-tx8r. A multi-audience token that omits "sigstore"
# is a wrong-audience token and must not be sent to Fulcio.
def test_sign_rejects_token_with_aud_array_not_containing_sigstore(
    crypto_factory, make_oidc_provider, canonical_bytes_simple, monkeypatch
):
    provider = make_oidc_provider(aud=["other", "yet-another"])
    spy = crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)

    with pytest.raises(
        signing.SigningUnavailable,
        match=r"OIDC token aud is .+, expected sigstore",
    ):
        signing.sign(canonical_bytes_simple, provider)

    assert _fulcio_call_count(crypto_factory, spy) == 0


# Enforces: finding-20260513-tx8r. Missing aud is a distinct local validation
# error so callers do not get an opaque Fulcio rejection.
def test_sign_rejects_token_missing_aud_claim(
    crypto_factory, make_oidc_provider, canonical_bytes_simple, monkeypatch
):
    provider = make_oidc_provider(include_aud=False)
    spy = crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)

    with pytest.raises(
        signing.SigningUnavailable, match="OIDC token missing aud claim"
    ):
        signing.sign(canonical_bytes_simple, provider)

    assert _fulcio_call_count(crypto_factory, spy) == 0


# Enforces: finding-20260513-tx8r defense-in-depth behavior. This duplicates the
# explicit call-count assertion so future refactors cannot accidentally move aud
# validation after Fulcio client setup.
def test_sign_does_not_call_fulcio_on_wrong_aud_token(
    crypto_factory, make_oidc_provider, canonical_bytes_simple, monkeypatch
):
    provider = make_oidc_provider(aud="https://api.github.com")
    spy = crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)

    with pytest.raises(signing.SigningUnavailable):
        signing.sign(canonical_bytes_simple, provider)

    assert _fulcio_call_count(crypto_factory, spy) == 0


# Enforces: finding-20260513-tx8r fail-closed branch (A8). An allowlisted issuer
# paired with a token that is not JWT-shaped cannot have its aud validated, so
# sign() rejects it before Fulcio rather than silently passing it through (the
# Phase-2 behavior finding-20260513-tx8r replaced). A revert to passthrough —
# which would pass every other aud test — fails this one.
def test_sign_rejects_non_jwt_token_from_allowlisted_issuer(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    provider = signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token="test-google-token",  # opaque, not header.payload.sig shaped
        provider_id="google",
    )
    spy = crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)

    with pytest.raises(
        signing.SigningUnavailable,
        match=r"OIDC token is not JWT-shaped, cannot validate aud",
    ) as exc:
        signing.sign(canonical_bytes_simple, provider)

    assert exc.value.component == "oidc"
    assert _fulcio_call_count(crypto_factory, spy) == 0
