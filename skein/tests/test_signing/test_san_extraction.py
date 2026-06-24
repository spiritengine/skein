"""SAN extraction tests for verify() identity extraction policy."""

from __future__ import annotations

import pytest

pytest.importorskip("skein.signing")

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


def _sb(canonical: bytes, blob: str) -> signing.SignatureBundle:
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )


def test_verify_extracts_rfc822name_san(crypto_factory, monkeypatch):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-rfc822"
    value = "alice@example.com"
    blob = crypto_factory.make_bundle_blob_with_san(
        san_type="rfc822Name",
        value=value,
        canonical_bytes=canonical,
        identity=value,
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == value


def test_verify_extracts_uri_san(crypto_factory, monkeypatch):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-uri"
    value = "https://example.com/identity/alice"
    blob = crypto_factory.make_bundle_blob_with_san(
        san_type="uniformResourceIdentifier",
        value=value,
        canonical_bytes=canonical,
        identity=value,
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == value


def test_verify_extracts_othername_oid_57264_1_24(crypto_factory, monkeypatch):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-othername"
    value = "repo:owner/name:ref:refs/heads/main"
    blob = crypto_factory.make_bundle_blob_with_san(
        san_type="otherName_oid_57264_1_24",
        value=value,
        canonical_bytes=canonical,
        identity=value,
        issuer="https://token.actions.githubusercontent.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == value


def test_verify_multiple_sans_extraction_policy(crypto_factory, monkeypatch):
    # Policy pin per finding-20260514-burb (closes brief-20260514-cw13):
    # multi-SAN preference order is rfc822Name → uniformResourceIdentifier →
    # otherName OID 1.3.6.1.4.1.57264.1.24. This test covers the rfc822Name-vs-URI
    # edge; the URI-vs-otherName and three-way edges are covered by the two
    # tests below.
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-multi"
    blob = crypto_factory.make_bundle_blob_with_multiple_sans(
        sans=[
            ("uniformResourceIdentifier", "https://example.com/u/alice"),
            ("rfc822Name", "alice@example.com"),
        ],
        canonical_bytes=canonical,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == "alice@example.com"


def test_verify_multi_sans_uri_outranks_othername_oid(crypto_factory, monkeypatch):
    # Enforces: uniformResourceIdentifier outranks otherName OID
    # 1.3.6.1.4.1.57264.1.24. Second preference edge of the policy ratified
    # by finding-20260514-burb. Fact-pattern is non-hypothetical: Fulcio CI
    # OIDC issues URI + otherName multi-SAN certs.
    #
    # otherName value mirrors the Fulcio CI signer-identity shape used by
    # the single-SAN test at test_verify_extracts_othername_oid_57264_1_24
    # so the fixture matches real Fulcio wire content.
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-uri-vs-othername"
    uri_value = "https://github.com/owner/repo/.github/workflows/ci.yml@refs/heads/main"
    othername_value = "repo:owner/repo:ref:refs/heads/main"
    blob = crypto_factory.make_bundle_blob_with_multiple_sans(
        sans=[
            ("otherName_oid_57264_1_24", othername_value),
            ("uniformResourceIdentifier", uri_value),
        ],
        canonical_bytes=canonical,
        identity=uri_value,
        issuer="https://token.actions.githubusercontent.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == uri_value, (
        f"URI must outrank otherName per finding-20260514-burb; got {vr.subject!r}"
    )


def test_verify_multi_sans_three_way_rfc822_wins(crypto_factory, monkeypatch):
    # Enforces: when all three SAN types coexist on the same cert,
    # rfc822Name wins (top of the preference order). Closes the transitivity
    # gap on the SAN policy from finding-20260514-burb — a non-transitive
    # implementation that prefers rfc822 > URI and URI > otherName but
    # surfaces otherName when all three are present would pass the pairwise
    # tests but fail here.
    #
    # otherName value mirrors the Fulcio CI signer-identity shape.
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-three-way"
    rfc822 = "alice@example.com"
    uri = "https://example.com/u/alice"
    othername = "repo:alice/site:ref:refs/heads/main"
    blob = crypto_factory.make_bundle_blob_with_multiple_sans(
        sans=[
            ("otherName_oid_57264_1_24", othername),
            ("uniformResourceIdentifier", uri),
            ("rfc822Name", rfc822),
        ],
        canonical_bytes=canonical,
        identity=rfc822,
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.subject == rfc822, (
        f"rfc822Name must win in three-way coexistence per finding-20260514-burb; "
        f"got {vr.subject!r}"
    )


def test_verify_missing_san_returns_cert_invalid(crypto_factory, monkeypatch):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-missing"
    blob = crypto_factory.make_bundle_blob_with_missing_san(
        canonical_bytes=canonical,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.CERT_INVALID


def test_verify_malformed_ia5string_san_returns_cert_invalid(
    crypto_factory, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"san-malformed"
    blob = crypto_factory.make_bundle_blob_with_malformed_ia5string_san(
        canonical_bytes=canonical,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    vr = signing.verify(canonical, _sb(canonical, blob))
    assert vr.status == signing.VerifyStatus.CERT_INVALID


class TestExtractSubjectEmptySanRejected:
    """Empty SAN must not propagate through _extract_subject_from_cert
    as the empty string.

    _decode_der_utf8(bytes([0x0c, 0x00])) is a syntactically valid DER
    encoding of a zero-length UTF8String — it returns ''. Before the
    fix, _extract_subject_from_cert returned that '' as the subject and
    verify()'s `if subject is None` guard let it through; the
    VerifyResult surfaced VERIFIED with subject=''. Empty subject is
    not a usable identity — any caller comparing result.subject against
    an expected signer would be misled. The fix rejects empty raw at
    the extraction layer (returning None so verify() maps to
    CERT_INVALID) and uses a truthiness check at the verify() guard.

    These tests stub cert objects directly rather than going through
    the cryptography library's normal cert builder. The library refuses
    to construct an empty x509.RFC822Name (raises ValueError), but it
    DOES accept an empty x509.UniformResourceIdentifier, so both the
    URI and OtherName-UTF8String paths can deliver an empty raw to
    _extract_subject_from_cert. Both are covered below.
    """

    def _stub_cert_with_san(self, san_objects):
        from unittest.mock import Mock

        san_ext_value = Mock()
        san_ext_value.__iter__ = lambda self: iter(san_objects)
        san_ext = Mock()
        san_ext.value = san_ext_value
        cert = Mock()
        cert.extensions.get_extension_for_class.return_value = san_ext
        return cert

    def test_empty_othername_utf8string_returns_none(self):
        from cryptography import x509

        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        # 0x0c 0x00 = DER UTF8String, length 0 -> decodes to ''
        empty_other = x509.OtherName(type_id=other_oid, value=bytes([0x0C, 0x00]))
        cert = self._stub_cert_with_san([empty_other])
        assert signing._extract_subject_from_cert(cert) is None

    def test_whitespace_only_othername_returns_none(self):
        from cryptography import x509

        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        # 0x0c 0x01 0x20 = DER UTF8String containing a single space.
        ws_other = x509.OtherName(type_id=other_oid, value=bytes([0x0C, 0x01, 0x20]))
        cert = self._stub_cert_with_san([ws_other])
        # `raw != raw.strip()` catches this — pre-existing path; pin the
        # behavior so the empty-string fix doesn't accidentally weaken it.
        assert signing._extract_subject_from_cert(cert) is None

    def test_valid_othername_unchanged(self):
        from cryptography import x509

        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        # 0x0c 0x05 "alice"
        valid_other = x509.OtherName(
            type_id=other_oid,
            value=bytes([0x0C, 0x05]) + b"alice",
        )
        cert = self._stub_cert_with_san([valid_other])
        assert signing._extract_subject_from_cert(cert) == "alice"

    def test_empty_uri_san_returns_none(self):
        # x509.UniformResourceIdentifier('') constructs without error
        # (unlike RFC822Name, which rejects empty at construction). An
        # empty URI SAN therefore reaches _extract_subject_from_cert as
        # raw=''. The same `if not raw: return None` guard that catches
        # the OtherName empty case must catch this too.
        from cryptography import x509

        empty_uri = x509.UniformResourceIdentifier("")
        cert = self._stub_cert_with_san([empty_uri])
        assert signing._extract_subject_from_cert(cert) is None

    def test_othername_with_wrong_der_tag_returns_none(self):
        # Prior implementation fell back to `name.value.decode("utf-8")`
        # when _decode_der_utf8 rejected the value. b"\x16\x05alice"
        # (IA5String tag 0x16 + length 5 + "alice") would round-trip as
        # the identity string "\x16\x05alice" — leading control chars
        # slipping past the NUL / strip / NFC guards because \x16 (SYN)
        # is not in str.strip()'s default whitespace set.
        from cryptography import x509

        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        ia5_other = x509.OtherName(type_id=other_oid, value=b"\x16\x05alice")
        cert = self._stub_cert_with_san([ia5_other])
        assert signing._extract_subject_from_cert(cert) is None

    def test_othername_with_garbage_bytes_returns_none(self):
        # Truly arbitrary bytes that happen to be valid UTF-8 but are
        # not a DER UTF8String. Prior implementation would have returned
        # this garbage as the subject identity.
        from cryptography import x509

        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        garbage_other = x509.OtherName(
            type_id=other_oid, value=b"\x16\x0cadmin@corp.com"
        )
        cert = self._stub_cert_with_san([garbage_other])
        assert signing._extract_subject_from_cert(cert) is None


class TestExtractIssuerV2DerStrict:
    """Issuer V2 (OID 1.3.6.1.4.1.57264.1.8) extraction must use strict DER.

    Prior implementation had a raw-UTF-8 fallback that admitted any byte
    sequence that happened to be valid UTF-8 — including IA5String-tagged
    values where the leading tag/length bytes become control characters
    embedded in the returned issuer string. The Issuer V2 path also
    skips the NUL/strip/NFC post-extraction guards entirely (it returns
    the decoded value directly), so the V2 raw fallback was an even
    sharper attack surface than the subject path.
    """

    def _stub_cert_with_issuer(self, oid_str, issuer_bytes):
        from unittest.mock import Mock

        ext = Mock()
        ext.oid.dotted_string = oid_str
        ext.value.value = issuer_bytes
        cert = Mock()
        cert.extensions = [ext]
        return cert

    def test_wrong_tag_v2_returns_none(self):
        # IA5String tag where DER UTF8String was expected.
        cert = self._stub_cert_with_issuer(
            "1.3.6.1.4.1.57264.1.8",
            b"\x16\x05alice",
        )
        assert signing._extract_issuer_from_cert(cert) is None

    def test_garbage_v2_returns_none(self):
        # Arbitrary valid UTF-8 bytes that are not DER UTF8String. Prior
        # implementation returned these as the issuer string.
        cert = self._stub_cert_with_issuer(
            "1.3.6.1.4.1.57264.1.8",
            b"\x16\x1bhttps://accounts.google.com",
        )
        assert signing._extract_issuer_from_cert(cert) is None

    def test_valid_v2_returns_decoded(self):
        # Regression: a real Fulcio-shaped DER UTF8String issuer round-trips.
        issuer = "https://accounts.google.com"
        body = issuer.encode("utf-8")
        # Short-form DER: 0x0C, length, body.
        der = bytes([0x0C, len(body)]) + body
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.8", der)
        assert signing._extract_issuer_from_cert(cert) == issuer

    def test_legacy_path_still_uses_raw_utf8(self):
        # The legacy issuer OID (1.3.6.1.4.1.57264.1.1) IS spec'd as raw
        # UTF-8 bytes (no DER wrapping); preserve that path.
        cert = self._stub_cert_with_issuer(
            "1.3.6.1.4.1.57264.1.1",
            b"https://accounts.google.com",
        )
        assert signing._extract_issuer_from_cert(cert) == "https://accounts.google.com"


class TestExtractIssuerSubjectParityGuards:
    """Issuer extraction has the same post-extraction guards as subject.

    finding-20260514-burb's policy on _extract_subject_from_cert was missing
    on the issuer path: NUL bytes, leading/trailing whitespace, and empty
    strings flowed through unfiltered for both Issuer V2 (DER UTF8String) and
    legacy (raw UTF-8 bytes) paths. NUL in particular enabled a startswith-
    prefix split bypass — a downstream
    ``issuer.startswith("https://accounts.google.com")`` returns True for
    ``"https://accounts.google.com\\x00bad"`` even though the cert's issuer
    string is not the trusted issuer. This class enforces parity.
    """

    def _stub_cert_with_issuer(self, oid_str, issuer_bytes):
        from unittest.mock import Mock

        ext = Mock()
        ext.oid.dotted_string = oid_str
        ext.value.value = issuer_bytes
        cert = Mock()
        cert.extensions = [ext]
        return cert

    def test_v2_nul_byte_rejected(self):
        # The V2 DER UTF8String parse admits NUL bytes (X.690 §8.1 allows any
        # UTF-8 octet sequence in UTF8String content; NUL is valid UTF-8).
        # Post-extraction NUL guard mirrors subject's burb policy.
        body = b"https://accounts.google.com\x00bad"
        der = bytes([0x0C, len(body)]) + body
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.8", der)
        assert signing._extract_issuer_from_cert(cert) is None

    def test_legacy_nul_byte_rejected(self):
        # The legacy issuer OID is raw UTF-8 bytes; .decode("utf-8") succeeds
        # on NUL-bearing input. Post-extraction NUL guard applies here too.
        cert = self._stub_cert_with_issuer(
            "1.3.6.1.4.1.57264.1.1",
            b"https://accounts.google.com\x00evil.com",
        )
        assert signing._extract_issuer_from_cert(cert) is None

    def test_v2_leading_whitespace_rejected(self):
        body = b" https://accounts.google.com"
        der = bytes([0x0C, len(body)]) + body
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.8", der)
        assert signing._extract_issuer_from_cert(cert) is None

    def test_v2_trailing_whitespace_rejected(self):
        body = b"https://accounts.google.com "
        der = bytes([0x0C, len(body)]) + body
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.8", der)
        assert signing._extract_issuer_from_cert(cert) is None

    def test_legacy_leading_whitespace_rejected(self):
        cert = self._stub_cert_with_issuer(
            "1.3.6.1.4.1.57264.1.1",
            b" https://accounts.google.com",
        )
        assert signing._extract_issuer_from_cert(cert) is None

    def test_v2_empty_string_rejected(self):
        # 0x0C 0x00 is a syntactically valid zero-length DER UTF8String, but
        # an empty issuer is not a usable identity.
        cert = self._stub_cert_with_issuer(
            "1.3.6.1.4.1.57264.1.8",
            bytes([0x0C, 0x00]),
        )
        assert signing._extract_issuer_from_cert(cert) is None

    def test_legacy_empty_bytes_rejected(self):
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.1", b"")
        assert signing._extract_issuer_from_cert(cert) is None

    def test_v2_nfc_normalized(self):
        # Decomposed precomposed-equivalent characters get normalized to NFC,
        # matching subject normalization behavior.
        body = "café".encode("utf-8")  # precomposed é (U+00E9)
        decomposed = "café".encode("utf-8")  # e + combining acute
        der = bytes([0x0C, len(decomposed)]) + decomposed
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.8", der)
        result = signing._extract_issuer_from_cert(cert)
        assert result is not None
        import unicodedata

        assert result == unicodedata.normalize("NFC", "café")
        assert result.encode("utf-8") == body

    def test_v2_valid_issuer_still_returns(self):
        # Regression: the happy path still works after the new guards.
        issuer = "https://accounts.google.com"
        body = issuer.encode("utf-8")
        der = bytes([0x0C, len(body)]) + body
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.8", der)
        assert signing._extract_issuer_from_cert(cert) == issuer


class TestVerifySingleRejectsMissingIssuer:
    """R4-1: _verify_single must not return VERIFIED with issuer=None.

    Fix is symmetric to the subject guard introduced by burb. A leaf cert
    whose issuer extensions are absent or malformed enough to fail
    _extract_issuer_from_cert is malformed for the sigstore-public-v1
    profile; surface as CERT_INVALID rather than a VERIFIED result whose
    issuer is None.
    """

    def test_verify_with_malformed_issuer_returns_cert_invalid(
        self,
        crypto_factory,
        monkeypatch,
    ):
        crypto_factory.install_verify_monkeypatch(monkeypatch)
        canonical = b"missing-issuer-probe"
        value = "alice@example.com"
        # Use a bundle that VERIFIES through the verifier, but on the cert
        # introspection side returns issuer=None. We patch
        # _extract_issuer_from_cert to simulate a leaf whose V2 OID parsed
        # garbage and whose legacy OID is absent — the same condition the
        # repro hits with a real IA5String-tagged V2 extension.
        blob = crypto_factory.make_bundle_blob_with_san(
            san_type="rfc822Name",
            value=value,
            canonical_bytes=canonical,
            identity=value,
            issuer="https://accounts.google.com",
        )
        monkeypatch.setattr(signing, "_extract_issuer_from_cert", lambda cert: None)
        vr = signing.verify(canonical, _sb(canonical, blob))
        assert vr.status == signing.VerifyStatus.CERT_INVALID
        assert vr.issuer is None
        assert vr.subject == value


class TestInvisibleIdentityCharsRejected:
    """R4-3 + Shard FEFF: default-ignorable / visually-invisible chars
    (zero-width, BOM, word-joiner, bidi directional formatting, variation
    selectors, HANGUL fillers) must not survive identity normalization on
    either subject or issuer paths.

    NFC normalization does not touch these characters, and they have no
    visible glyph (or, for RLO/LRO, they reorder surrounding text),
    so they can be smuggled into an identity to produce a string that
    is visually identical to a trusted identity but compares non-equal
    against it.

    Coverage: the whole Cf (Format) and Cc (Control) categories, plus an
    explicit allowlist of default-ignorable chars that fall outside those
    categories (variation selectors are Mn; HANGUL fillers are Lo) and
    would slip past a category-only check.

      U+200B-U+200F : ZWSP, ZWNJ, ZWJ, LRM, RLM           (Cf)
      U+202A-U+202E : LRE, RLE, PDF, LRO, RLO             (Cf, legacy bidi)
      U+2066-U+2069 : LRI, RLI, FSI, PDI                  (Cf, modern bidi)
      U+FEFF        : ZERO WIDTH NO-BREAK SPACE / BOM     (Cf, Shard FEFF)
      U+2060        : WORD JOINER                         (Cf, Shard FEFF)
      U+061C        : ARABIC LETTER MARK                  (Cf)
      U+180E        : MONGOLIAN VOWEL SEPARATOR           (Cf)
      U+FE00-U+FE0F : Variation Selectors VS-1..VS-16     (Mn, FEFF r1 follow-up)
      U+E0100       : Variation Selector VS-17            (Mn, supplementary)
      U+180B        : Mongolian Free Variation Selector 1 (Mn)
      U+115F        : HANGUL CHOSEONG FILLER              (Lo)
      U+3164        : HANGUL FILLER                       (Lo)
    """

    INVISIBLE_CHARS = [
        ("U+200B ZWSP", "​"),
        ("U+200C ZWNJ", "‌"),
        ("U+200D ZWJ", "‍"),
        ("U+200E LRM", "‎"),
        ("U+200F RLM", "‏"),
        ("U+202A LRE", "‪"),
        ("U+202B RLE", "‫"),
        ("U+202C PDF", "‬"),
        ("U+202D LRO", "‭"),
        ("U+202E RLO", "‮"),
        ("U+2066 LRI", "⁦"),
        ("U+2067 RLI", "⁧"),
        ("U+2068 FSI", "⁨"),
        ("U+2069 PDI", "⁩"),
        # Shard FEFF / j4w4 round 7 + oracle pass: empirically-confirmed
        # gaps in the ad-hoc enumerated set. Both are Cf-category, visually
        # invisible, and survive NFC.
        ("U+FEFF BOM", "﻿"),
        ("U+2060 WJ", "⁠"),
        # Additional Cf-category chars added in recent Unicode versions —
        # caught by the category-based check, would slip past an enumerated
        # ad-hoc set. Pin so a regression to an ad-hoc set fails here.
        ("U+061C ALM", "؜"),
        ("U+180E MVS", "᠎"),
        # Shard FEFF r1 fell follow-up: variation selectors are Mn, not
        # Cf — a category-only check (Cf ∪ Cc) misses them. Pin three
        # representatives spanning the standard, supplementary, and
        # Mongolian ranges so a regression to a category-only predicate
        # surfaces here.
        ("U+FE00 VS-1", chr(0xFE00)),
        ("U+FE0F VS-16", chr(0xFE0F)),
        ("U+E0100 VS-17", chr(0xE0100)),
        ("U+180B Mongolian FVS-1", chr(0x180B)),
        # HANGUL fillers are Lo (Other_Letter), not Cf or Cc — same gap
        # in a category-only check. They render as zero-width placeholders.
        ("U+115F HANGUL CHOSEONG FILLER", chr(0x115F)),
        ("U+3164 HANGUL FILLER", chr(0x3164)),
    ]

    def _stub_cert_with_san(self, san_objects):
        from unittest.mock import Mock

        san_ext_value = Mock()
        san_ext_value.__iter__ = lambda self: iter(san_objects)
        san_ext = Mock()
        san_ext.value = san_ext_value
        cert = Mock()
        cert.extensions.get_extension_for_class.return_value = san_ext
        return cert

    def _stub_cert_with_issuer(self, oid_str, issuer_bytes):
        from unittest.mock import Mock

        ext = Mock()
        ext.oid.dotted_string = oid_str
        ext.value.value = issuer_bytes
        cert = Mock()
        cert.extensions = [ext]
        return cert

    @pytest.mark.parametrize("label,ch", INVISIBLE_CHARS)
    def test_subject_othername_invisible_char_rejected(self, label, ch):
        from cryptography import x509

        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        val = f"alice{ch}@example.com"
        body = val.encode("utf-8")
        der = bytes([0x0C, len(body)]) + body
        other = x509.OtherName(type_id=other_oid, value=der)
        cert = self._stub_cert_with_san([other])
        assert signing._extract_subject_from_cert(cert) is None, label

    @pytest.mark.parametrize("label,ch", INVISIBLE_CHARS)
    def test_subject_rfc822_invisible_char_rejected(self, label, ch):
        # RFC822Name SAN preference comes first. Confirm the guard fires
        # there too — not only the OtherName path.
        from cryptography import x509

        try:
            email = x509.RFC822Name(f"alice{ch}@example.com")
        except (ValueError, TypeError):
            pytest.skip(f"cryptography rejects RFC822Name with {label}")
        cert = self._stub_cert_with_san([email])
        assert signing._extract_subject_from_cert(cert) is None, label

    @pytest.mark.parametrize("label,ch", INVISIBLE_CHARS)
    def test_subject_uri_invisible_char_rejected(self, label, ch):
        # cryptography.x509.UniformResourceIdentifier(...) rejects non-ASCII
        # at construction, so we stub the SAN entry directly to exercise the
        # extraction function's URI branch. A maliciously-encoded cert could
        # still produce a URI SAN with these chars on the wire.
        from cryptography import x509
        from unittest.mock import Mock

        uri = Mock(spec=x509.UniformResourceIdentifier)
        uri.value = f"https://example.com/alice{ch}"
        cert = self._stub_cert_with_san([uri])
        assert signing._extract_subject_from_cert(cert) is None, label

    @pytest.mark.parametrize("label,ch", INVISIBLE_CHARS)
    def test_issuer_v2_invisible_char_rejected(self, label, ch):
        val = f"https://accounts.{ch}google.com"
        body = val.encode("utf-8")
        der = bytes([0x0C, len(body)]) + body
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.8", der)
        assert signing._extract_issuer_from_cert(cert) is None, label

    @pytest.mark.parametrize("label,ch", INVISIBLE_CHARS)
    def test_issuer_legacy_invisible_char_rejected(self, label, ch):
        val = f"https://accounts.{ch}google.com"
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.1", val.encode("utf-8"))
        assert signing._extract_issuer_from_cert(cert) is None, label

    def test_subject_valid_identity_still_returns(self):
        # Regression: identities with no invisible chars still extract.
        from cryptography import x509

        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        val = "alice@example.com"
        body = val.encode("utf-8")
        der = bytes([0x0C, len(body)]) + body
        other = x509.OtherName(type_id=other_oid, value=der)
        cert = self._stub_cert_with_san([other])
        assert signing._extract_subject_from_cert(cert) == val

    def test_issuer_valid_identity_still_returns(self):
        # Regression: real Fulcio-shaped issuer still passes the new guard.
        val = "https://accounts.google.com"
        body = val.encode("utf-8")
        der = bytes([0x0C, len(body)]) + body
        cert = self._stub_cert_with_issuer("1.3.6.1.4.1.57264.1.8", der)
        assert signing._extract_issuer_from_cert(cert) == val

    def test_non_invisible_unicode_still_normalized(self):
        # Regression: NFC normalization still happens for non-invisible
        # combining chars (decomposed é → precomposed é).
        from cryptography import x509
        import unicodedata

        other_oid = x509.ObjectIdentifier("1.3.6.1.4.1.57264.1.24")
        decomposed = "café@example.com".encode("utf-8")
        der = bytes([0x0C, len(decomposed)]) + decomposed
        other = x509.OtherName(type_id=other_oid, value=der)
        cert = self._stub_cert_with_san([other])
        result = signing._extract_subject_from_cert(cert)
        assert result == unicodedata.normalize("NFC", "café@example.com")


class TestDecodeDerUtf8Strict:
    """White-box on _decode_der_utf8: strict DER UTF8String parse.

    The function powers issuer/subject extraction from Fulcio cert extensions
    (OID 1.3.6.1.4.1.57264.1.8 issuer V2 and OID 1.3.6.1.4.1.57264.1.24
    signer-identity OtherName). Pre-tightening, it accepted BER
    indefinite-length encoding (data[1] == 0x80, returned empty string) and
    silently truncated bodies whose claimed length exceeded the buffer
    (returned partial bytes). Both shapes are now rejected per X.690.
    """

    def test_happy_path_short_form(self):
        assert signing._decode_der_utf8(bytes([0x0C, 0x05]) + b"hello") == "hello"
        assert signing._decode_der_utf8(bytes([0x0C, 0x00])) == ""

    def test_happy_path_long_form_128(self):
        # Length 128 — smallest legal long-form encoding.
        data = bytes([0x0C, 0x81, 0x80]) + (b"X" * 128)
        assert signing._decode_der_utf8(data) == "X" * 128

    def test_wrong_tag_rejected(self):
        assert signing._decode_der_utf8(bytes([0x0D, 0x01, 0x41])) is None
        assert signing._decode_der_utf8(b"") is None
        assert signing._decode_der_utf8(b"\x0c") is None

    def test_ber_indefinite_length_rejected(self):
        # 0x0C 0x80 -> BER indefinite-length, illegal in DER (X.690 §10.1).
        # Was decoded as empty string under lenient parser.
        assert signing._decode_der_utf8(bytes([0x0C, 0x80])) is None

    def test_silent_truncation_rejected(self):
        # Claims length 10, supplies only 5. Was decoded as partial 'hello'.
        assert signing._decode_der_utf8(bytes([0x0C, 0x0A]) + b"hello") is None
        # Claims length 200, supplies 50.
        assert signing._decode_der_utf8(bytes([0x0C, 0x81, 0xC8]) + b"X" * 50) is None

    def test_non_minimal_long_form_rejected(self):
        # Length < 128 must use short form (X.690 §10.1).
        assert signing._decode_der_utf8(bytes([0x0C, 0x81, 0x7F]) + b"X" * 127) is None
        assert signing._decode_der_utf8(bytes([0x0C, 0x81, 0x05]) + b"hello") is None

    def test_leading_zero_in_long_form_length_rejected(self):
        # Leading zero octet would not be minimal — X.690 §10.1 forbids it.
        assert (
            signing._decode_der_utf8(bytes([0x0C, 0x82, 0x00, 0x80]) + b"X" * 128)
            is None
        )

    def test_trailing_garbage_rejected(self):
        # The encoded value must consume exactly the input — any trailing
        # bytes mean the input is not a single well-formed UTF8String.
        assert (
            signing._decode_der_utf8(bytes([0x0C, 0x05]) + b"hello" + b"EXTRA") is None
        )

    def test_insufficient_length_octets_rejected(self):
        # 0x82 says "2 length octets follow", but only 1 is present.
        assert signing._decode_der_utf8(bytes([0x0C, 0x82, 0x00])) is None

    def test_invalid_utf8_body_rejected(self):
        # 0xFF is never a valid UTF-8 start byte.
        assert signing._decode_der_utf8(bytes([0x0C, 0x01, 0xFF])) is None
