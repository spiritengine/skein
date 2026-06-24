"""Regression: cert issuer (not OIDC token issuer) drives the authz binding.

Lesson: a human Sigstore login federates through the Dex broker
(https://oauth2.sigstore.dev/auth) for the OIDC ceremony, but Fulcio mints a
cert whose issuer extension carries the UPSTREAM provider — for a Google login
that is https://accounts.google.com, empirically confirmed 2026-06-20 by
decoding real stored certs (OIDs 1.3.6.1.4.1.57264.1.8 and .1.1).

`skein whoami` prints the OIDC *token* issuer (the broker). That is NOT
what can_write() keys on. Bootstrapping a binding from whoami's issuer will
never match a signed publish; the binding must use the cert issuer (the
upstream federated provider). Read it off a real cert or let the redeem
ceremony auto-bind.

Non-vacuity: test_google_cert_accepted_through_ingest passes only when the
fake signer's default cert issuer is https://accounts.google.com. If that
default drifts (e.g. reverts to the broker), the binding misses and ingest
rejects with "unbound signer", failing the test.
"""
from __future__ import annotations

import base64
import json
import pytest

from skein import signing
from skein.signing import _test_factory

from skein import publish as pub_mod
from skein import sign as sign_mod
from skein.ingress import ingest
from skein.station import Station


_GOOGLE_ISSUER = "https://accounts.google.com"
_BROKER_ISSUER = "https://oauth2.sigstore.dev/auth"


def _unsigned_jwt(issuer: str = _GOOGLE_ISSUER, aud: str = "sigstore") -> str:
    def b64(d: dict) -> str:
        raw = json.dumps(d, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64({"alg": "none", "typ": "JWT"})
    payload = b64({"sub": "alice@example.com", "iss": issuer, "aud": aud})
    return f"{header}.{payload}."


@pytest.fixture
def google_provider():
    return signing.OIDCProviderConfig(
        issuer=_GOOGLE_ISSUER,
        token=_unsigned_jwt(issuer=_GOOGLE_ISSUER),
        provider_id="google",
    )


@pytest.fixture
def client(tmp_path):
    s = Station(tmp_path / "client" / ".skein")
    s.create_site("specs", purpose="Public specs", created_by="t")
    s.post("finding", "specs", "Design Overview", "body", created_by="t")
    yield s
    s.close()


@pytest.fixture
def instance(tmp_path):
    s = Station(tmp_path / "instance" / ".skein")
    yield s
    s.close()


def test_google_cert_accepted_through_ingest(client, instance, google_provider, monkeypatch):
    """Bind under accounts.google.com; sign with the fake signer's default issuer
    (no explicit provider, so _build_bundle falls back to accounts.google.com).
    Ingest must ACCEPT the publish under require_signed.

    Non-vacuity: if the fake signer's default changes away from accounts.google.com,
    the cert issuer no longer matches the binding and ingest returns "unbound signer",
    failing this test.
    """
    _test_factory.install_sign_monkeypatch(monkeypatch, provider=None)
    _test_factory.install_verify_monkeypatch(monkeypatch)

    signer = sign_mod.make_oidc_signer(google_provider)

    # Discover (issuer, subject) embedded in the fake cert — must be accounts.google.com.
    probe = sign_mod.sign_manifest(["sha256::" + "0" * 64], signer)
    assert probe["issuer"] == _GOOGLE_ISSUER, (
        f"fake signer default issuer is {probe['issuer']!r}, expected {_GOOGLE_ISSUER!r}; "
        "this means the fake signer's fallback changed — update the binding or fix the signer"
    )

    instance.store.add_binding(probe["issuer"], probe["subject"], role="author")

    monkeypatch.setattr(
        pub_mod,
        "post_batch",
        lambda url, batch, timeout=30.0: ingest(instance, batch, require_signed=True),
    )

    result = pub_mod.publish(
        client, "http://instance.example", site="specs", signer=signer
    )

    assert result["signed"] is True
    ack = result["ack"]
    assert len(ack["rejected"]) == 0, f"unexpected rejections: {ack['rejected']}"
    assert len(ack["accepted"]) >= 1


def test_broker_cert_rejected_when_only_google_bound(client, instance, google_provider, monkeypatch):
    """Bind under accounts.google.com but produce a cert with the broker issuer
    (https://oauth2.sigstore.dev/auth). Ingest must REJECT with "unbound signer".

    This documents that the cert issuer — the upstream federated provider, not the
    OIDC token issuer / broker — is what the binding must match. Signing through the
    broker does NOT produce a broker-issuer cert; it produces a Google-issuer cert.
    Operators who mistakenly bootstrap from the broker issuer will see this rejection.
    """
    # Override the cert issuer to the broker explicitly (models a misconfigured binding).
    _test_factory.install_sign_monkeypatch(monkeypatch, issuer=_BROKER_ISSUER, provider=None)
    _test_factory.install_verify_monkeypatch(monkeypatch)

    signer = sign_mod.make_oidc_signer(google_provider)

    # Bind ONLY under the Google issuer — the real cert would carry.
    instance.store.add_binding(_GOOGLE_ISSUER, "alice@example.com", role="author")

    monkeypatch.setattr(
        pub_mod,
        "post_batch",
        lambda url, batch, timeout=30.0: ingest(instance, batch, require_signed=True),
    )

    result = pub_mod.publish(
        client, "http://instance.example", site="specs", signer=signer
    )

    assert result["signed"] is True
    ack = result["ack"]
    assert len(ack["accepted"]) == 0, f"unexpected accepts: {ack['accepted']}"
    assert all(r["reason"] == "unbound signer" for r in ack["rejected"]), ack["rejected"]
