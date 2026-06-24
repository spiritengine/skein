"""OIDC issuer allowlist contract (finding-20260513-w5hq section 2)."""

from __future__ import annotations

import pytest

pytest.importorskip("skein.signing")

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


def test_sign_rejects_issuer_not_in_v0_allowlist(
    crypto_factory,
    make_oidc_provider,
    canonical_bytes_simple,
    monkeypatch,
):
    provider = make_oidc_provider(
        issuer="https://login.microsoftonline.com/common/v2.0",
        provider_id="azure",
    )
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, provider)
    assert exc.value.component == "oidc"
    assert "allowlist" in exc.value.reason.lower()


def test_sign_rejects_github_actions_oidc_issuer(
    crypto_factory,
    make_oidc_provider,
    canonical_bytes_simple,
    monkeypatch,
):
    provider = make_oidc_provider(
        issuer="https://token.actions.githubusercontent.com",
        provider_id="github-actions",
    )
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    with pytest.raises(signing.SigningUnavailable) as exc:
        signing.sign(canonical_bytes_simple, provider)
    assert exc.value.component == "oidc"
    assert "allowlist" in exc.value.reason.lower()


def test_sign_accepts_google_personal_oauth(
    crypto_factory,
    make_oidc_provider,
    canonical_bytes_simple,
    monkeypatch,
):
    provider = make_oidc_provider(
        issuer="https://accounts.google.com", provider_id="google"
    )
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    result = signing.sign(canonical_bytes_simple, provider)
    assert isinstance(result, signing.SignResult)


def test_sign_accepts_github_personal_oauth(
    crypto_factory,
    make_oidc_provider,
    canonical_bytes_simple,
    monkeypatch,
):
    provider = make_oidc_provider(
        issuer="https://github.com/login/oauth", provider_id="github"
    )
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    result = signing.sign(canonical_bytes_simple, provider)
    assert isinstance(result, signing.SignResult)


# w5hq §2 amendment (2026-05-19): the Sigstore Dex brokers are admitted as
# identity intermediaries for the personal-OAuth allowlist. Both staging
# (oauth2.sigstage.dev) and prod (oauth2.sigstore.dev) Dex are accepted; Dex's
# connector config constrains the underlying IdP to the original allowlist's
# set (Google / GitHub-personal / Microsoft). Empirical premise: the K-A9
# interactive run on 2026-05-19 confirmed sigstore-python's standard
# human-flow Issuer returns a Dex-issued token (iss=oauth2.sigstage.dev/auth,
# federated_issuer=accounts.google.com) — see brief-20260519-5aa5.
def test_sign_accepts_sigstore_staging_dex_broker(
    crypto_factory,
    make_oidc_provider,
    canonical_bytes_simple,
    monkeypatch,
):
    provider = make_oidc_provider(
        issuer="https://oauth2.sigstage.dev/auth",
        provider_id="sigstore-staging-dex",
    )
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    result = signing.sign(canonical_bytes_simple, provider)
    assert isinstance(result, signing.SignResult)


def test_sign_accepts_sigstore_prod_dex_broker(
    crypto_factory,
    make_oidc_provider,
    canonical_bytes_simple,
    monkeypatch,
):
    provider = make_oidc_provider(
        issuer="https://oauth2.sigstore.dev/auth",
        provider_id="sigstore-prod-dex",
    )
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=provider)
    result = signing.sign(canonical_bytes_simple, provider)
    assert isinstance(result, signing.SignResult)
