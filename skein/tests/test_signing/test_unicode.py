"""Unicode hardening contracts for signing identities and canonical bytes.

Oracle finding-20260512-sr0w called out that the earlier Unicode tests were too
forgiving: identity properties filtered to NFC, adversarial tests mostly
asserted "no crash", and canonical payload coverage did not pin invisible or
directional characters. These tests define the v0 behavior explicitly.
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


GOOGLE_ISSUER = "https://accounts.google.com"


def _bundle(canonical: bytes, blob: str) -> signing.SignatureBundle:
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )


def _verify_identity(crypto_factory, monkeypatch, *, subject: str, issuer: str):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"unicode-identity-contract"
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical,
        identity=subject,
        issuer=issuer,
    )
    return signing.verify(canonical, _bundle(canonical, blob))


# Enforces: oracle actionable #1. SAN subjects are compared and exposed in NFC
# form, so logically identical NFC/NFD email subjects do not diverge.
def test_verify_identity_nfc_vs_nfd_subjects(crypto_factory, monkeypatch):
    nfc = "josé@example.com"
    nfd = "jose\u0301@example.com"

    nfc_result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject=nfc,
        issuer=GOOGLE_ISSUER,
    )
    nfd_result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject=nfd,
        issuer=GOOGLE_ISSUER,
    )

    assert nfc_result.status == signing.VerifyStatus.VERIFIED
    assert nfc_result.subject == nfc
    assert nfd_result.status == signing.VerifyStatus.VERIFIED
    assert nfd_result.subject == nfc


# Enforces R4-3 (F-pass round 4): zero-width joiners in extracted SAN
# subjects are not safe to surface \u2014 they are visually invisible and produce
# strings that look identical to a trusted identity but compare non-equal.
# Fail closed at extraction rather than preserving them in a VERIFIED result.
# Supersedes the earlier "preserve to prevent stripping collision" policy:
# rejection is strictly stronger than preservation against substitution.
def test_verify_identity_zero_width_joiner_in_subject(crypto_factory, monkeypatch):
    subject = "alice\u200dops@example.com"
    result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject=subject,
        issuer=GOOGLE_ISSUER,
    )
    assert result.status != signing.VerifyStatus.VERIFIED


# Enforces R4-3 (F-pass round 4): RTL/bidi-control marks in extracted SAN
# subjects are rejected at the extraction layer. The mark visually reorders
# surrounding glyphs without changing byte-level comparison, so a cert with
# one of these in the identity field is unsafe to surface as VERIFIED.
def test_verify_identity_rtl_marks_in_subject(crypto_factory, monkeypatch):
    subject = "alice\u202e@example.com"
    result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject=subject,
        issuer=GOOGLE_ISSUER,
    )
    assert result.status != signing.VerifyStatus.VERIFIED


# Enforces Shard FEFF (j4w4 round 7 + oracle pass): U+FEFF (BOM /
# ZERO WIDTH NO-BREAK SPACE) is visually invisible, survives NFC, and was
# absent from the historical ad-hoc invisible-char set. A SAN whose URI
# is "https://accounts.google.com﻿x" looks identical to a whitelisted
# issuer but compares non-equal, defeating downstream string-equality
# policy checks (e.g., issuer-allowlist membership / identity pinning).
# Reject at extraction.
def test_verify_identity_bom_in_issuer_visual_equivalence(crypto_factory, monkeypatch):
    smuggled_issuer = "https://accounts.google.com﻿x"
    result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com",
        issuer=smuggled_issuer,
    )
    assert result.status != signing.VerifyStatus.VERIFIED


# Enforces Shard FEFF: U+2060 (WORD JOINER) has the same shape as the BOM
# attack — invisible, NFC-stable, not in the legacy enumerated set.
def test_verify_identity_word_joiner_in_subject(crypto_factory, monkeypatch):
    subject = "alice⁠ops@example.com"
    result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject=subject,
        issuer=GOOGLE_ISSUER,
    )
    assert result.status != signing.VerifyStatus.VERIFIED


# Enforces: oracle actionable #1. X.509 SAN subjects with trailing whitespace
# fail closed instead of being trimmed into a different identity.
def test_verify_identity_trailing_whitespace_subject(crypto_factory, monkeypatch):
    result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com ",
        issuer=GOOGLE_ISSUER,
    )
    assert result.status != signing.VerifyStatus.VERIFIED


# Enforces: oracle actionable #1. Issuer URLs are byte-string identities in the
# v0 profile; punycode and Unicode host spellings do not compare equal.
def test_verify_identity_punycode_vs_unicode_issuer(crypto_factory, monkeypatch):
    punycode_issuer = "https://accounts.xn--bcher-kva.example"
    unicode_issuer = "https://accounts.bücher.example"

    punycode_result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com",
        issuer=punycode_issuer,
    )
    unicode_result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com",
        issuer=unicode_issuer,
    )

    assert punycode_result.status == signing.VerifyStatus.VERIFIED
    assert punycode_result.issuer == punycode_issuer
    assert unicode_result.status == signing.VerifyStatus.VERIFIED
    assert unicode_result.issuer == unicode_issuer
    assert punycode_result.issuer != unicode_result.issuer


# Enforces: oracle actionable #1. Confusable issuer domains remain distinct;
# sigstore.io and sigstore.com are different issuer URLs.
def test_verify_identity_confusable_issuer_io_vs_com(crypto_factory, monkeypatch):
    io_result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com",
        issuer="https://oauth.sigstore.io",
    )
    com_result = _verify_identity(
        crypto_factory,
        monkeypatch,
        subject="alice@example.com",
        issuer="https://oauth.sigstore.com",
    )

    assert io_result.status == signing.VerifyStatus.VERIFIED
    assert io_result.issuer == "https://oauth.sigstore.io"
    assert com_result.status == signing.VerifyStatus.VERIFIED
    assert com_result.issuer == "https://oauth.sigstore.com"
    assert io_result.issuer != com_result.issuer


@pytest.mark.parametrize(
    "canonical",
    [
        "zero\u200dwidth\u200cchars".encode("utf-8"),
        "rtl\u202eoverride\u202cmarks".encode("utf-8"),
        "emoji-\U0001f469\u200d\U0001f4bb-\U0001f680".encode("utf-8"),
        "combining-e\u0301-a\u0308".encode("utf-8"),
    ],
)
def test_canonical_bytes_round_trip_unicode_edges(
    crypto_factory, google_provider, monkeypatch, canonical
):
    # Enforces: oracle actionable #1. SignatureBundle JSON keeps UTF-8
    # canonical bytes byte-exact, including invisible and multi-byte codepoints.
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical, google_provider)
    sb = _bundle(canonical, result.bundle_json)

    assert (
        signing.SignatureBundle.model_validate_json(
            sb.model_dump_json()
        ).canonical_bytes
        == canonical
    )
    assert signing.verify(canonical, sb).status == signing.VerifyStatus.VERIFIED


# Enforces: oracle actionable #1. The signature binds the Unicode-heavy payload
# bytes themselves, not a decoded or normalized string form.
def test_sign_verify_round_trip_unicode_heavy_canonical_bytes(
    crypto_factory, google_provider, canonical_bytes_unicode_heavy, monkeypatch
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes_unicode_heavy, google_provider)
    sb = _bundle(canonical_bytes_unicode_heavy, result.bundle_json)

    assert (
        signing.verify(canonical_bytes_unicode_heavy, sb).status
        == signing.VerifyStatus.VERIFIED
    )
    assert signing.verify(canonical_bytes_unicode_heavy + b"!", sb).status == (
        signing.VerifyStatus.SIGNATURE_MISMATCH
    )
