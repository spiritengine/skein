"""Wire encoding contract for folio-level SignatureBundle JSON."""

from __future__ import annotations

import base64
import json

import pytest
from pydantic import ValidationError

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from .conftest import signing  # noqa: E402


def _bundle(canonical_bytes: bytes) -> signing.SignatureBundle:
    return signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["<placeholder-sigstore-bundle-protojson>"],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )


def _json_payload(canonical_bytes: str) -> str:
    return json.dumps(
        {
            "identity_scheme": "sigstore-public-v1",
            "bundles": ["<placeholder-sigstore-bundle-protojson>"],
            "canonical_bytes": canonical_bytes,
            "canon_version": "knurl-1.0",
        }
    )


# Enforces: finding-20260512-sr0w blocker #1. Folio JSON carries
# signature_bundle.canonical_bytes as standard base64, not UTF-8 bytes or ints.
def test_signature_bundle_emits_base64_for_canonical_bytes():
    canonical = b"\xfb\xff\x00canonical\xfe"
    sb = _bundle(canonical)
    payload = json.loads(sb.model_dump_json())
    assert payload["canonical_bytes"] == base64.b64encode(canonical).decode("ascii")
    assert payload["canonical_bytes"] != canonical.decode("latin1")
    assert not isinstance(payload["canonical_bytes"], list)


# Enforces: finding-20260512-sr0w blocker #1. JSON parsing decodes the
# canonical_bytes base64 field back to the exact bytes that were signed.
def test_signature_bundle_accepts_base64_canonical_bytes_on_parse():
    canonical = b"\x00signed bytes\xff"
    encoded = base64.b64encode(canonical).decode("ascii")
    sb = signing.SignatureBundle.model_validate_json(_json_payload(encoded))
    assert sb.canonical_bytes == canonical


# Enforces: finding-20260512-sr0w blocker #1. Non-base64 strings must fail
# closed instead of being treated as UTF-8 payload bytes.
def test_signature_bundle_rejects_non_base64_canonical_bytes():
    with pytest.raises(ValidationError):
        signing.SignatureBundle.model_validate_json(_json_payload("not base64!!!"))


# Enforces: finding-20260512-sr0w blocker #1. Every byte value survives the
# folio JSON boundary byte-for-byte.
def test_signature_bundle_round_trip_arbitrary_bytes():
    canonical = bytes(range(256))
    sb = _bundle(canonical)
    sb2 = signing.SignatureBundle.model_validate_json(sb.model_dump_json())
    assert sb2.canonical_bytes == canonical


@pytest.mark.parametrize(
    "canonical",
    [
        b"\xff\xfeh\x00e\x00l\x00l\x00o\x00",  # UTF-16LE with BOM
        b"prefix\x00middle\x00suffix",  # embedded NULs
        b"\xff\xfe\xfa\xfb",  # invalid UTF-8
        "\u200b\u200d\u200f\u202ertl".encode("utf-8"),  # zero-width + RTL
    ],
    ids=[
        "utf16_with_bom",
        "embedded_null",
        "invalid_utf8",
        "zero_width_rtl_unicode_payload",
    ],
)
def test_signature_bundle_round_trip_byte_edge_cases(canonical):
    sb = _bundle(canonical)
    sb2 = signing.SignatureBundle.model_validate_json(sb.model_dump_json())
    assert sb2.canonical_bytes == canonical


def test_signature_bundle_json_canonical_serialization():
    canonical = b"federation-canonical-json"
    sb = _bundle(canonical)
    j1 = sb.model_dump_json()
    sb2 = signing.SignatureBundle.model_validate_json(j1)
    j2 = sb2.model_dump_json()
    assert j1 == j2


def test_signature_bundle_json_whitespace_variant_canonicalizes():
    raw = '{  "identity_scheme":"sigstore-public-v1", "bundles":[ "<placeholder-sigstore-bundle-protojson>" ], "canonical_bytes":"YQ==", "canon_version":"knurl-1.0" }'
    parsed = signing.SignatureBundle.model_validate_json(raw)
    assert parsed.model_dump_json() == _bundle(b"a").model_dump_json()


def test_signature_bundle_json_key_order_variant_canonicalizes():
    raw = '{"canon_version":"knurl-1.0","canonical_bytes":"YQ==","bundles":["<placeholder-sigstore-bundle-protojson>"],"identity_scheme":"sigstore-public-v1"}'
    parsed = signing.SignatureBundle.model_validate_json(raw)
    assert parsed.model_dump_json() == _bundle(b"a").model_dump_json()
