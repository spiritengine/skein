"""Canonicalization conformance vectors + the hash/sign boundary test (slice 2).

knurl is the reference implementation of the canonical serialization, but it is an
unpinned external dependency: a knurl change could silently alter SKEIN's hashes
with no version bump (zr29 CRITICAL #2). So the CONTRACT is these frozen vectors —
``(fields -> canonical_bytes -> content_hash)`` triples — not the code. If knurl
drifts, these break loudly; the vectors define ``skein.folio.canon/v1`` per
brief-20260603-tgg8 §4 (and the knurl 0.3.0 amendments in brief-20260603-n22c).

The ``ascii_basic`` triple is the verbatim worked example from tgg8 §4.
"""

from __future__ import annotations

import hashlib

import pytest

from skein import canon
from skein.identity import compute_folio_hash

# Frozen contract. Regenerate ONLY with a deliberate, reviewed canon-version bump.
VECTORS = {
    "ascii_basic": dict(
        fields={
            "type": "finding",
            "title": "Hello",
            "content": "# body",
            "created_at": "2026-01-01T00:00:00+00:00",
            "created_by": "alice",
        },
        canonical_bytes=(
            b'{"content":"# body","created_at":"2026-01-01T00:00:00+00:00",'
            b'"created_by":"alice","title":"Hello","type":"finding"}'
        ),
        content_hash="sha256::f778b633bd4f6633528cc7d25c966ff6f6ec5e0812e115b30783e54ed0d531d4",
    ),
    "nfc_nonascii": dict(
        fields={
            "type": "finding",
            "title": "café",
            "content": "über",
            "created_at": "2026-01-01T00:00:00+00:00",
            "created_by": "résumé",
        },
        canonical_bytes=(
            b'{"content":"\xc3\xbcber","created_at":"2026-01-01T00:00:00+00:00",'
            b'"created_by":"r\xc3\xa9sum\xc3\xa9","title":"caf\xc3\xa9","type":"finding"}'
        ),
        content_hash="sha256::a7018772acecf11ae4cf040a060140580dc92e186c597bad37a72bf7860adab2",
    ),
    # Same characters as nfc_nonascii but typed decomposed (combining marks); NFC
    # must collapse them to the identical bytes and hash.
    "nfd_decomposed": dict(
        fields={
            "type": "finding",
            "title": "café",
            "content": "über",
            "created_at": "2026-01-01T00:00:00+00:00",
            "created_by": "résumé",
        },
        canonical_bytes=(
            b'{"content":"\xc3\xbcber","created_at":"2026-01-01T00:00:00+00:00",'
            b'"created_by":"r\xc3\xa9sum\xc3\xa9","title":"caf\xc3\xa9","type":"finding"}'
        ),
        content_hash="sha256::a7018772acecf11ae4cf040a060140580dc92e186c597bad37a72bf7860adab2",
    ),
    "subsecond_ts": dict(
        fields={
            "type": "note",
            "title": "t",
            "content": "c",
            "created_at": "2026-05-29T14:47:52.123456+00:00",
            "created_by": "a",
        },
        canonical_bytes=(
            b'{"content":"c","created_at":"2026-05-29T14:47:52.123456+00:00",'
            b'"created_by":"a","title":"t","type":"note"}'
        ),
        content_hash="sha256::b277b28b1e291d7c04e8e42f5d902f53a523d05adfc9cac045863452739f49d4",
    ),
    # A sub-6-digit fractional second (".1") pads to 6 digits. Before Python 3.11
    # fromisoformat rejected ".1" outright; the parser normalizes it so the
    # canonical bytes — and this frozen hash — are identical across 3.10-3.12.
    "subsecond_short_ts": dict(
        fields={
            "type": "note",
            "title": "t",
            "content": "c",
            "created_at": "2026-05-29T14:47:52.1+00:00",
            "created_by": "a",
        },
        canonical_bytes=(
            b'{"content":"c","created_at":"2026-05-29T14:47:52.100000+00:00",'
            b'"created_by":"a","title":"t","type":"note"}'
        ),
        content_hash="sha256::bc65ee7ef2982d0f1b195da687c34d0a232dde4354aa34c2cd70e244316c31f0",
    ),
    # A >6-digit fraction TRUNCATES to microseconds (CPython's own behavior, which
    # the fix's [:6] slice matches) — ".1234567" -> ".123456", the SAME canonical
    # bytes/hash as the 6-digit vector above. Rounding would yield ".123457" and a
    # different hash, so this vector pins truncation against a future edit.
    "subsecond_long_ts": dict(
        fields={
            "type": "note",
            "title": "t",
            "content": "c",
            "created_at": "2026-05-29T14:47:52.1234567+00:00",
            "created_by": "a",
        },
        canonical_bytes=(
            b'{"content":"c","created_at":"2026-05-29T14:47:52.123456+00:00",'
            b'"created_by":"a","title":"t","type":"note"}'
        ),
        content_hash="sha256::b277b28b1e291d7c04e8e42f5d902f53a523d05adfc9cac045863452739f49d4",
    ),
    # 'Z' normalizes to '+00:00' and whole seconds drop the fractional part.
    "wholesecond_z": dict(
        fields={
            "type": "note",
            "title": "t",
            "content": "c",
            "created_at": "2026-05-29T14:47:52Z",
            "created_by": "a",
        },
        canonical_bytes=(
            b'{"content":"c","created_at":"2026-05-29T14:47:52+00:00",'
            b'"created_by":"a","title":"t","type":"note"}'
        ),
        content_hash="sha256::d30a57771ace4683ed6af6adf8445464dd4917e5a5f6d7d013f7679d3da59f9c",
    ),
    # A non-UTC offset normalizes to the same UTC instant as wholesecond_z.
    "offset_ts": dict(
        fields={
            "type": "note",
            "title": "t",
            "content": "c",
            "created_at": "2026-05-29T19:47:52+05:00",
            "created_by": "a",
        },
        canonical_bytes=(
            b'{"content":"c","created_at":"2026-05-29T14:47:52+00:00",'
            b'"created_by":"a","title":"t","type":"note"}'
        ),
        content_hash="sha256::d30a57771ace4683ed6af6adf8445464dd4917e5a5f6d7d013f7679d3da59f9c",
    ),
    "missing_fields": dict(
        fields={
            "type": "note",
            "title": None,
            "content": None,
            "created_at": None,
            "created_by": None,
        },
        canonical_bytes=(
            b'{"content":null,"created_at":null,"created_by":null,' b'"title":null,"type":"note"}'
        ),
        content_hash="sha256::b7d7a444ed3c6efc74c6c07421c4ee8bb83b8289f5a0536373b643d3e9d9684f",
    ),
    "escaping": dict(
        fields={
            "type": "finding",
            "title": 'a"q\\b',
            "content": "line1\nline2\ttab",
            "created_at": "2026-01-01T00:00:00+00:00",
            "created_by": "a",
        },
        canonical_bytes=(
            b'{"content":"line1\\nline2\\ttab","created_at":"2026-01-01T00:00:00+00:00",'
            b'"created_by":"a","title":"a\\"q\\\\b","type":"finding"}'
        ),
        content_hash="sha256::c46ee4bc62c9da3607d824356ce2fbfabed8674818052b1521db599e341ce8ed",
    ),
    # A literal NUL in a field serializes to the JSON escape \\u0000 — NO raw 0x00
    # byte in the output. This is the property the domain-separation NUL relies on
    # (profile || 0x00 || canonical_bytes is unambiguous only if canonical_bytes
    # can't contain a raw NUL); pinned here so a knurl change that stopped escaping
    # control characters breaks loudly rather than silently un-separating the
    # preimage (load-bearing once a second profile is registered).
    "nul_escaped": dict(
        fields={
            "type": "note",
            "title": "t",
            "content": "a\x00b",
            "created_at": "2026-01-01T00:00:00+00:00",
            "created_by": "x",
        },
        canonical_bytes=(
            b'{"content":"a\\u0000b","created_at":"2026-01-01T00:00:00+00:00",'
            b'"created_by":"x","title":"t","type":"note"}'
        ),
        content_hash="sha256::a2abaef72b11900618f417d44925d4b183c0e16df03dc9ac56805fd203808d59",
    ),
}


@pytest.mark.parametrize("name", list(VECTORS))
def test_canonical_bytes_match_frozen_vector(name):
    vec = VECTORS[name]
    assert canon.folio_canonical_bytes(vec["fields"]) == vec["canonical_bytes"]


@pytest.mark.parametrize("name", list(VECTORS))
def test_content_hash_matches_frozen_vector(name):
    vec = VECTORS[name]
    assert compute_folio_hash(vec["fields"]) == vec["content_hash"]


@pytest.mark.parametrize("name", list(VECTORS))
def test_canonical_bytes_never_contain_a_raw_nul(name):
    # The domain-separation preimage is profile || 0x00 || canonical_bytes; that
    # split is unambiguous only because canonical bytes can never hold a raw NUL.
    assert b"\x00" not in VECTORS[name]["canonical_bytes"]


# --- the properties the vectors encode (stated, not just implied) -----------


def test_nfc_and_nfd_collapse_to_one_identity():
    assert compute_folio_hash(VECTORS["nfc_nonascii"]["fields"]) == compute_folio_hash(
        VECTORS["nfd_decomposed"]["fields"]
    )


def test_z_and_offset_normalize_to_utc():
    h = VECTORS["wholesecond_z"]["content_hash"]
    assert compute_folio_hash(VECTORS["wholesecond_z"]["fields"]) == h
    assert compute_folio_hash(VECTORS["offset_ts"]["fields"]) == h  # +05:00 -> UTC


# --- the hash/sign boundary test (zr29 CRITICAL #1) -------------------------


@pytest.mark.parametrize("name", list(VECTORS))
def test_hash_path_and_sign_path_operate_on_identical_bytes(name):
    """The bytes the signer signs (``folio_canonical_bytes``) hash, byte-for-byte,
    to the digest inside ``compute_folio_hash`` — so hash and signature can never
    silently disagree on what was covered."""
    fields = VECTORS[name]["fields"]
    signed_bytes = canon.folio_canonical_bytes(fields)
    hashed_digest = compute_folio_hash(fields).split("::", 1)[1]
    assert hashlib.sha256(signed_bytes).hexdigest() == hashed_digest
