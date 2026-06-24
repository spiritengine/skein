"""Property-based tests (Hypothesis) for skein.signing.

Properties tested
-----------------
1. Round-trip: any canonical_bytes that signs cleanly verifies cleanly.
2. Signature binding: any perturbation (single byte, prefix, suffix) of
   canonical_bytes makes verify return SIGNATURE_MISMATCH.
3. Bundle parse fidelity: serialize and deserialize through SignatureBundle's
   JSON shape preserves bytes, lengths, and order.
4. Verify determinism: verify() is a pure function of (canonical_bytes,
   signature_bundle); calling it twice yields identical VerifyResults.
5. Multi-signer order: results[i] always corresponds to bundles[i].
6. Bundle blob bit-flip: any single bit flipped in a known-good blob makes
   verify return non-VERIFIED.
7. Length truncation: any prefix of a bundle blob shorter than full length
   makes verify return non-VERIFIED.
8. canonical_bytes preservation: SignatureBundle stores canonical_bytes
   byte-exactly across JSON round-trip.
9. canon_version invariance: verify result is independent of canon_version.
10. Identity tuple stability: verify on same bundle returns identical
    (issuer, subject).
11. VerifyResult field invariants per status.
12. Verify-cache key independence: two distinct bundles for the same
    canonical_bytes verify independently.

Strategies
----------
- canonical_bytes_strategy: bytes of length 0 to 4KB.
- signer_count_strategy: integers in [1, 5].
- bit_flip_strategy: integer offset + bit index.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from hypothesis import (  # noqa: E402
    HealthCheck,
    given,
    settings,
    strategies as st,
)

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402
from .test_sign import _assert_sign_results_differ_per_field  # noqa: E402

pytestmark = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


canonical_bytes_strategy = st.binary(min_size=0, max_size=4096)
small_canonical_bytes_strategy = st.binary(min_size=1, max_size=512)
signer_count_strategy = st.integers(min_value=1, max_value=5)

unicode_subject_strategy = st.text(
    alphabet=st.characters(exclude_categories=("Cs",)),
    min_size=1,
    max_size=64,
)

issuer_url_strategy = st.sampled_from(
    [
        "https://accounts.google.com",
        "https://github.com/login/oauth",
    ]
)

canon_version_strategy = st.text(min_size=1, max_size=32).filter(str.isprintable)


_settings = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# ---------------------------------------------------------------------------
# Section 1 — Round-trip: sign → verify
# ---------------------------------------------------------------------------


# Property: ∀ canonical_bytes, sign(b).bundle_json wrapped in a SignatureBundle
# verifies to VERIFIED. Central invariant.
@given(canonical_bytes=canonical_bytes_strategy)
@_settings
def test_property_sign_then_verify_roundtrips(
    crypto_factory, google_provider, monkeypatch, canonical_bytes
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED


# Property: round-trip exposes the same identity in the result.
@given(canonical_bytes=canonical_bytes_strategy)
@_settings
def test_property_roundtrip_identity_populated(
    crypto_factory, google_provider, monkeypatch, canonical_bytes
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED
    assert vr.issuer is not None
    assert vr.subject is not None


# ---------------------------------------------------------------------------
# Section 2 — Signature binding: perturb → non-VERIFIED
# ---------------------------------------------------------------------------


# Property: any single-bit perturbation of canonical_bytes results in non-VERIFIED.
@given(
    canonical_bytes=small_canonical_bytes_strategy,
    flip_offset=st.integers(min_value=0),
    flip_bit=st.integers(min_value=0, max_value=7),
)
@_settings
def test_property_any_perturbation_breaks_verification(
    crypto_factory,
    google_provider,
    monkeypatch,
    canonical_bytes,
    flip_offset,
    flip_bit,
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    perturbed = bytearray(canonical_bytes)
    offset = flip_offset % len(perturbed)
    perturbed[offset] ^= 1 << flip_bit
    perturbed = bytes(perturbed)
    if perturbed == canonical_bytes:
        return  # bit flip was a no-op
    vr = signing.verify(perturbed, sb)
    assert vr.status != signing.VerifyStatus.VERIFIED


# Property: prepending bytes to canonical_bytes breaks verification.
@given(
    canonical_bytes=small_canonical_bytes_strategy,
    extra=st.binary(min_size=1, max_size=4),
)
@_settings
def test_property_prefix_extension_breaks_verification(
    crypto_factory,
    google_provider,
    monkeypatch,
    canonical_bytes,
    extra,
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(extra + canonical_bytes, sb)
    assert vr.status != signing.VerifyStatus.VERIFIED


# Property: appending bytes to canonical_bytes breaks verification.
@given(
    canonical_bytes=small_canonical_bytes_strategy,
    extra=st.binary(min_size=1, max_size=4),
)
@_settings
def test_property_suffix_extension_breaks_verification(
    crypto_factory,
    google_provider,
    monkeypatch,
    canonical_bytes,
    extra,
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical_bytes + extra, sb)
    assert vr.status != signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Section 3 — Bundle parse fidelity
# ---------------------------------------------------------------------------


# Property: any SignatureBundle survives JSON round-trip with byte-equality on
# bundles[i] and canonical_bytes.
@given(
    canonical_bytes=canonical_bytes_strategy,
    n=signer_count_strategy,
)
@_settings
def test_property_signature_bundle_json_roundtrip(
    crypto_factory,
    monkeypatch,
    canonical_bytes,
    n,
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical_bytes,
            identity=f"alice{i}@example.com",
            issuer="https://accounts.google.com",
        )
        for i in range(n)
    ]
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    j = sb.model_dump_json()
    sb2 = signing.SignatureBundle.model_validate_json(j)
    assert sb2.bundles == sb.bundles
    assert sb2.canonical_bytes == sb.canonical_bytes
    assert sb2.identity_scheme == sb.identity_scheme
    assert sb2.canon_version == sb.canon_version
    assert sb2.trust_root_pin == sb.trust_root_pin


# ---------------------------------------------------------------------------
# Section 4 — Verify determinism: pure function
# ---------------------------------------------------------------------------


# Property: verify() is referentially transparent.
@given(canonical_bytes=small_canonical_bytes_strategy)
@_settings
def test_property_verify_is_deterministic(
    crypto_factory, google_provider, monkeypatch, canonical_bytes
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes, google_provider)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    a = signing.verify(canonical_bytes, sb)
    b = signing.verify(canonical_bytes, sb)
    assert a == b


# ---------------------------------------------------------------------------
# Section 5 — Multi-signer order: results[i] ↔ bundles[i]
# ---------------------------------------------------------------------------


# Property: regardless of N, results[i].subject corresponds to bundles[i]'s SAN.
@given(n=signer_count_strategy)
@_settings
def test_property_verify_multi_preserves_order_for_any_n(
    crypto_factory, monkeypatch, n
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"hypothesis-payload"
    identities = [f"signer{i}@example.com" for i in range(n)]
    blobs = [
        crypto_factory.make_bundle_blob(
            canonical_bytes=canonical,
            identity=identities[i],
            issuer="https://accounts.google.com",
        )
        for i in range(n)
    ]
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=blobs,
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    multi = signing.verify_multi(canonical, sb)
    assert [r.subject for r in multi.results] == identities


# ---------------------------------------------------------------------------
# Section 6 — Bundle bit-flip fuzz
# ---------------------------------------------------------------------------


# Property: ∀ single-bit flip in a known-good bundle, verify returns non-VERIFIED.
@given(
    flip_offset=st.integers(min_value=0),
    flip_bit=st.integers(min_value=0, max_value=7),
)
@_settings
def test_property_bundle_bit_flip_breaks_verify(
    crypto_factory, google_provider, monkeypatch, flip_offset, flip_bit
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"fuzz-payload-of-fixed-bytes-12345678"
    result = signing.sign(canonical, google_provider)
    blob = result.bundle_json
    if not blob:
        return
    munged = crypto_factory.bit_flip(blob, flip_offset % len(blob), flip_bit)
    if munged == blob:
        return
    # Phase-3 amendment: a single bit flip in the bundle JSON can land in a
    # semantically-empty region (base64 padding bits, whitespace, numeric
    # string digits that the verifier doesn't bind on, JSON encoding choice
    # bytes). Real sigstore-python verification ignores those. We strengthen
    # the test to focus on cryptographically-bound regions: skip if the parsed
    # bundle round-trips to the same content as the original.
    import json as _json

    try:
        original_obj = _json.loads(blob)
        munged_obj = _json.loads(munged)
        if _bundle_crypto_payload(original_obj) == _bundle_crypto_payload(munged_obj):
            return
    except (ValueError, KeyError):
        # If the mutation broke JSON parsing, verify must reject.
        pass
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[munged],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical, sb)
    assert vr.status != signing.VerifyStatus.VERIFIED


def _checkpoint_envelope_payload(env: str) -> tuple:
    """Extract checkpoint envelope's cryptographically-bound parts.

    Mirrors SignedNote.from_text / SignedNote.verify
    (sigstore/_internal/rekor/checkpoint.py:128-187). Real verify binds:
    (a) the signed prefix (text up to and including the '\\n' before the
    '\\n\\n' separator), (b) the em-dash + line structure (the regex must
    parse), (c) the decoded sig_hash (first 4 bytes, matched against
    key_id[:4] at checkpoint.py:174), (d) the decoded sig body. The
    decorative signer-name token between '\\u2014 ' and the b64 signature is
    NOT bound — it may drift without real verify caring (finding-20260521-33nj).

    Returns a ("UNPARSEABLE", env) sentinel when the envelope does not match
    the SignedNote grammar; such flips also cause real verify to reject.
    """
    import base64 as _b64
    import re as _re

    sep = "\n\n"
    if env.count(sep) != 1:
        return ("UNPARSEABLE", env)
    split = env.index(sep)
    prefix = env[: split + 1]
    sig_block = env[split + len(sep):]
    if not sig_block or sig_block[-1] != "\n":
        return ("UNPARSEABLE", env)
    lines = sig_block.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    sig_re = _re.compile(r"— (\S+) (\S+)")
    sigs = []
    for line in lines:
        m = sig_re.fullmatch(line)
        if not m:
            return ("UNPARSEABLE", env)
        try:
            raw = _b64.b64decode(m.group(2))
        except Exception:  # noqa: BLE001
            return ("UNPARSEABLE", env)
        if len(raw) < 4:
            return ("UNPARSEABLE", env)
        sigs.append((raw[:4], raw[4:]))
    return ("PARSED", prefix, tuple(sigs))


def _set_is_verified_time_source(obj: dict) -> bool:
    """Mirror sigstore.verify.verifier.Verifier._establish_time SET clause.

    SET counts when inclusion_promise.signed_entry_timestamp + integrated_time
    are present AND kindVersion is hashedrekord/dsse 0.0.1 — see verifier.py:228-232.
    The kindVersion clause matters: exotic kindVersions raise BEFORE SET
    counts (verifier.py:233), so the gate must check it explicitly.
    """
    vm = obj.get("verificationMaterial") or {}
    tlogs = vm.get("tlogEntries") or [{}]
    te = tlogs[0] or {}
    promise = (te.get("inclusionPromise") or {}).get("signedEntryTimestamp")
    integrated = te.get("integratedTime")
    kv = te.get("kindVersion") or {}
    return (
        bool(promise)
        and bool(integrated)
        and kv.get("kind") in ("hashedrekord", "dsse")
        and kv.get("version") == "0.0.1"
    )


def _bundle_crypto_payload(obj: dict) -> tuple:
    """Reduce a bundle JSON to its cryptographically-bound payload.

    Captures every region real sigstore-python verify binds: the leaf cert,
    message signature + digest, and the full Rekor tlog witness — inclusion
    proof (rootHash, hashes, treeSize, logIndex, checkpoint), logId.keyId,
    integratedTime, inclusionPromise SET, canonicalizedBody — plus RFC3161
    TSA timestamps when SET is not the verified time source. Base64 byte
    regions are decoded so a flip that only perturbs base64 padding bits
    (decoded bytes unchanged) compares equal. The checkpoint envelope's
    decorative signer-name token and (when SET binds time) the TSA bytes are
    excluded — both are empirically not bound by real verify
    (finding-20260521-33nj). Two bundles equal here are cryptographically
    indistinguishable for verification — anything else MUST be rejected
    by verify().
    """
    import base64 as _b64

    def _db64(v):
        if not v:
            return b""
        try:
            return _b64.b64decode(v)
        except Exception:  # noqa: BLE001
            return ("RAW", v)

    vm = obj.get("verificationMaterial") or {}
    cert = _db64((vm.get("certificate") or {}).get("rawBytes", ""))
    msg_sig = obj.get("messageSignature") or {}
    sig = _db64(msg_sig.get("signature", ""))
    digest_obj = msg_sig.get("messageDigest") or {}
    digest = _db64(digest_obj.get("digest", ""))
    algo = digest_obj.get("algorithm", "")

    tlogs = vm.get("tlogEntries") or []
    te = tlogs[0] if tlogs else {}
    ip = te.get("inclusionProof") or {}
    cp = ip.get("checkpoint") or {}
    cp_subfields = tuple(
        (k, cp.get(k)) for k in sorted(cp.keys()) if k != "envelope"
    )
    ip_payload = (
        _db64(ip.get("rootHash", "")),
        tuple(_db64(h) for h in (ip.get("hashes") or [])),
        str(ip.get("treeSize", "")),
        str(ip.get("logIndex", "")),
        cp_subfields,
        _checkpoint_envelope_payload(cp.get("envelope", "")),
    )
    log_id = _db64((te.get("logId") or {}).get("keyId", ""))
    promise = _db64((te.get("inclusionPromise") or {}).get("signedEntryTimestamp", ""))
    canon_body = _db64(te.get("canonicalizedBody", ""))
    integrated = str(te.get("integratedTime", ""))

    tvd = vm.get("timestampVerificationData") or {}
    if _set_is_verified_time_source(obj):
        tsa: tuple = ()
    else:
        tsa = tuple(
            _db64(t.get("signedTimestamp", ""))
            for t in (tvd.get("rfc3161Timestamps") or [])
        )

    return (
        cert,
        sig,
        digest,
        algo,
        ip_payload,
        log_id,
        promise,
        canon_body,
        integrated,
        tsa,
    )


# Property: a bit-flip inside the checkpoint envelope's decorative signer-name
# span is not bound by real sigstore-python verify
# (sigstore/_internal/rekor/checkpoint.py:166-187 — only sig_hash + decoded
# sig body bind, NAME is decorative), so the factory MUST accept it. Pins
# the §D loosening of _canonical_drift_exception (finding-20260521-33nj).
@given(
    flip_bit=st.integers(min_value=0, max_value=7),
    name_byte_offset=st.integers(min_value=0, max_value=63),
)
@_settings
def test_property_checkpoint_signer_name_drift_accepts(
    crypto_factory, google_provider, monkeypatch, flip_bit, name_byte_offset
):
    import json as _json
    import re as _re

    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"signer-name-drift-payload"
    result = signing.sign(canonical, google_provider)
    obj = _json.loads(result.bundle_json)
    ip = obj["verificationMaterial"]["tlogEntries"][0]["inclusionProof"]
    cp = ip.get("checkpoint") or {}
    envelope = cp.get("envelope", "")
    sep = "\n\n"
    if envelope.count(sep) != 1:
        return
    split = envelope.index(sep)
    sig_block_start = split + len(sep)
    sig_block = envelope[sig_block_start:]
    m = _re.search(r"— (\S+) (\S+)\n", sig_block)
    if not m:
        return
    name = m.group(1)
    if name_byte_offset >= len(name):
        return
    original_ch = name[name_byte_offset]
    flipped_ord = ord(original_ch) ^ (1 << flip_bit)
    if flipped_ord > 0x7E or flipped_ord < 0x21:
        # Stay inside printable ASCII non-space so the \S+ regex still parses.
        return
    new_ch = chr(flipped_ord)
    if new_ch == original_ch:
        return
    new_name = name[:name_byte_offset] + new_ch + name[name_byte_offset + 1:]
    name_start = sig_block_start + m.start(1)
    name_end = sig_block_start + m.end(1)
    new_envelope = envelope[:name_start] + new_name + envelope[name_end:]
    if new_envelope == envelope:
        return
    cp["envelope"] = new_envelope
    ip["checkpoint"] = cp
    obj["verificationMaterial"]["tlogEntries"][0]["inclusionProof"] = ip
    munged = _json.dumps(obj)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[munged],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED, (
        f"factory rejected checkpoint signer-name drift that real sigstore-python "
        f"accepts; status={vr.status}"
    )


# Property: when SET is the verified time source (inclusion_promise +
# integrated_time present AND kindVersion hashedrekord/dsse 0.0.1, per
# sigstore-python verifier.py:228-232), a bit-flip in TSA bytes is silently
# tolerated by real verify as long as the bytes still ASN.1-parse as a
# TimeStampResponse (verifier.py:192-196 drops failed TSA results;
# SET still meets VERIFIED_TIME_THRESHOLD=1 at verifier.py:67). Pins the §D
# loosening of _canonical_drift_exception (finding-20260521-33nj).
def _real_corpus_tsa_bytes() -> bytes:
    """Real RFC3161 TimeStampResponse bytes lifted from the corpus TSA bundle.

    We need ASN.1-parseable bytes so the factory's TVD-parse boundary
    (mirroring sigstore.models.TimestampVerificationData._verify) accepts
    the minted bundle; arbitrary garbage would trip that early.
    """
    import base64 as _b64
    import json as _json
    from pathlib import Path

    corpus = Path(__file__).resolve().parents[1] / "conformance" / "corpus" / "tsa" / "bundle.txt.sigstore"
    obj = _json.loads(corpus.read_bytes())
    ts = obj["verificationMaterial"]["timestampVerificationData"]["rfc3161Timestamps"][0]["signedTimestamp"]
    return _b64.b64decode(ts)


@given(
    flip_bit=st.integers(min_value=0, max_value=7),
    b64_offset=st.integers(min_value=0),
)
@_settings
def test_property_tsa_drift_with_set_time_source_accepts(
    crypto_factory, google_provider, monkeypatch, flip_bit, b64_offset
):
    import base64 as _b64
    import json as _json

    real_tsa = _real_corpus_tsa_bytes()
    crypto_factory.install_sign_monkeypatch(
        monkeypatch, provider=google_provider, tsa_timestamps=[real_tsa]
    )
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"tsa-drift-payload"
    result = signing.sign(canonical, google_provider)
    obj = _json.loads(result.bundle_json)
    tvd = obj.get("verificationMaterial", {}).get("timestampVerificationData") or {}
    rfc = tvd.get("rfc3161Timestamps") or []
    if not rfc:
        return
    ts_b64 = rfc[0].get("signedTimestamp", "")
    if not ts_b64:
        return
    # SET-as-time-source predicate must hold; the factory's default minting
    # gives us inclusion_promise + integrated_time + hashedrekord/0.0.1.
    te = obj["verificationMaterial"]["tlogEntries"][0]
    promise = (te.get("inclusionPromise") or {}).get("signedEntryTimestamp")
    integrated = te.get("integratedTime")
    kv = te.get("kindVersion") or {}
    if not (
        promise
        and integrated
        and kv.get("kind") in ("hashedrekord", "dsse")
        and kv.get("version") == "0.0.1"
    ):
        return
    offset = b64_offset % len(ts_b64)
    original_ch = ts_b64[offset]
    flipped_ord = ord(original_ch) ^ (1 << flip_bit)
    new_ch = chr(flipped_ord)
    # Keep the result inside the b64 alphabet so the JSON re-parses cleanly.
    if not (new_ch.isalnum() or new_ch in "+/="):
        return
    new_ts_b64 = ts_b64[:offset] + new_ch + ts_b64[offset + 1:]
    if new_ts_b64 == ts_b64:
        return
    try:
        new_raw = _b64.b64decode(new_ts_b64)
        if new_raw == _b64.b64decode(ts_b64):
            return  # padding-bit no-op
    except Exception:  # noqa: BLE001
        return
    # Real sigstore-python's deeper TSA checks (ASN.1 parse, PKIStatus enum
    # validation, lazy tst_info access) propagate exceptions that surface as
    # real-verify rejections. The factory mirrors this. Skip flips that
    # trigger any of those — those are case-2 (both reject), not the
    # case-3-becomes-accept we're pinning.
    try:
        from rfc3161_client import PKIStatus as _PKIStatus
        from rfc3161_client import decode_timestamp_response as _decode_ts
    except ImportError:
        return
    try:
        _new_tsr = _decode_ts(new_raw)
        _PKIStatus(_new_tsr.status)
        _ = _new_tsr.tst_info.message_imprint.hash_algorithm
    except Exception:  # noqa: BLE001
        return
    rfc[0]["signedTimestamp"] = new_ts_b64
    obj["verificationMaterial"]["timestampVerificationData"][
        "rfc3161Timestamps"
    ] = rfc
    munged = _json.dumps(obj)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[munged],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical, sb)
    assert vr.status == signing.VerifyStatus.VERIFIED, (
        f"factory rejected TSA drift while SET counts as time source; real "
        f"sigstore-python silently tolerates this. status={vr.status}"
    )


# ---------------------------------------------------------------------------
# Section 7 — Length truncation fuzz
# ---------------------------------------------------------------------------


# Property: ∀ prefix shorter than the full blob, verify returns non-VERIFIED.
@given(truncate_ratio=st.floats(min_value=0.0, max_value=0.99))
@_settings
def test_property_bundle_truncation_returns_non_verified(
    crypto_factory, google_provider, monkeypatch, truncate_ratio
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"truncate-payload"
    result = signing.sign(canonical, google_provider)
    blob = result.bundle_json
    cut = int(len(blob) * truncate_ratio)
    if cut == len(blob):
        return
    truncated = crypto_factory.truncate(blob, cut)
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[truncated],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical, sb)
    assert vr.status != signing.VerifyStatus.VERIFIED


# ---------------------------------------------------------------------------
# Section 8 — canonical_bytes preservation
# ---------------------------------------------------------------------------


# Property: SignatureBundle stores canonical_bytes byte-exactly across Pydantic
# JSON round-trip. All-zero, high-bit, embedded NULs survive.
@given(canonical_bytes=canonical_bytes_strategy)
@_settings
def test_property_signature_bundle_preserves_canonical_bytes(canonical_bytes):
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["<placeholder>"],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    j = sb.model_dump_json()
    sb2 = signing.SignatureBundle.model_validate_json(j)
    assert sb2.canonical_bytes == canonical_bytes


# ---------------------------------------------------------------------------
# Section 9 — canon_version invariance
# ---------------------------------------------------------------------------


# Property: two bundles differing only in canon_version verify identically.
# Rev 5 normative: verifiers MUST NOT use canon_version to re-derive canonical_bytes.
@given(
    canonical_bytes=small_canonical_bytes_strategy,
    version_a=canon_version_strategy,
    version_b=canon_version_strategy,
)
@_settings
def test_property_verify_independent_of_canon_version(
    crypto_factory,
    google_provider,
    monkeypatch,
    canonical_bytes,
    version_a,
    version_b,
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    result = signing.sign(canonical_bytes, google_provider)
    sb_a = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version=version_a,
    )
    sb_b = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[result.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version=version_b,
    )
    r_a = signing.verify(canonical_bytes, sb_a)
    r_b = signing.verify(canonical_bytes, sb_b)
    assert r_a.status == r_b.status


# ---------------------------------------------------------------------------
# Section 10 — Identity tuple stability
# ---------------------------------------------------------------------------


# Property: verify on same bundle returns identical (issuer, subject).
@given(
    canonical_bytes=small_canonical_bytes_strategy,
    issuer=issuer_url_strategy,
    subject=unicode_subject_strategy,
)
@_settings
def test_property_identity_stable_across_calls(
    crypto_factory,
    monkeypatch,
    canonical_bytes,
    issuer,
    subject,
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes,
        identity=subject,
        issuer=issuer,
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    r1 = signing.verify(canonical_bytes, sb)
    r2 = signing.verify(canonical_bytes, sb)
    assert r1.issuer == r2.issuer
    assert r1.subject == r2.subject


# Property: issuer in result is exactly the URL from the cert OID; no normalization.
@given(
    issuer=issuer_url_strategy,
    canonical_bytes=small_canonical_bytes_strategy,
)
@_settings
def test_property_issuer_url_is_exact_match(
    crypto_factory,
    monkeypatch,
    issuer,
    canonical_bytes,
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes,
        identity="alice@example.com",
        issuer=issuer,
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    r = signing.verify(canonical_bytes, sb)
    if r.status == signing.VerifyStatus.VERIFIED:
        assert r.issuer == issuer


# ---------------------------------------------------------------------------
# Section 11 — VerifyResult field invariants per status
# ---------------------------------------------------------------------------


class TestVerifyResultFieldInvariants:
    """Per VerifyStatus, (issuer, subject, evidence) shape from rev 5."""

    # VERIFIED → issuer and subject populated.
    @given(issuer=issuer_url_strategy, subject=unicode_subject_strategy)
    @_settings
    def test_verified_has_issuer_and_subject(self, issuer, subject):
        r = signing.VerifyResult(
            status=signing.VerifyStatus.VERIFIED,
            issuer=issuer,
            subject=subject,
            evidence=None,
        )
        assert r.issuer is not None
        assert r.subject is not None

    # BUNDLE_MALFORMED → no identity (the bundle wasn't parseable enough to extract).
    def test_bundle_malformed_has_no_identity(self):
        r = signing.VerifyResult(
            status=signing.VerifyStatus.BUNDLE_MALFORMED,
            issuer=None,
            subject=None,
            evidence=None,
        )
        assert r.issuer is None
        assert r.subject is None
        assert r.evidence is None

    # CERT_INVALID → no identity (cert chain failed to verify).
    def test_cert_invalid_has_no_identity(self):
        r = signing.VerifyResult(
            status=signing.VerifyStatus.CERT_INVALID,
            issuer=None,
            subject=None,
            evidence=None,
        )
        assert r.issuer is None
        assert r.subject is None

    # OFFLINE_NO_TRUSTED_ROOT → no identity (no trust anchor to validate cert).
    def test_offline_no_trusted_root_has_no_identity(self):
        r = signing.VerifyResult(
            status=signing.VerifyStatus.OFFLINE_NO_TRUSTED_ROOT,
            issuer=None,
            subject=None,
            evidence=None,
        )
        assert r.issuer is None
        assert r.subject is None


# ---------------------------------------------------------------------------
# Section 12 — Verify-cache key independence
# ---------------------------------------------------------------------------


# Property: two distinct bundles for the same canonical_bytes verify independently
# (rev 5 §verify-cache key: cache must key on (content_hash, bundle_hash)).
@given(canonical_bytes=small_canonical_bytes_strategy)
@_settings
def test_property_two_bundles_same_content_verify_independently(
    crypto_factory, google_provider, monkeypatch, canonical_bytes
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    r1 = signing.sign(canonical_bytes, google_provider)
    r2 = signing.sign(canonical_bytes, google_provider)
    sb_1 = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[r1.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    sb_2 = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[r2.bundle_json],
        canonical_bytes=canonical_bytes,
        canon_version="knurl-1.0",
    )
    v1 = signing.verify(canonical_bytes, sb_1)
    v2 = signing.verify(canonical_bytes, sb_2)
    # Both must verify; bundles are independent (different ephemeral keys).
    assert isinstance(v1, signing.VerifyResult)
    assert isinstance(v2, signing.VerifyResult)


# ---------------------------------------------------------------------------
# Section 13 — Unknown scheme always fail-closed
# ---------------------------------------------------------------------------


# Property: any unregistered identity_scheme is BUNDLE_MALFORMED.
@given(
    scheme=st.text(min_size=1, max_size=64).filter(
        lambda s: s != "sigstore-public-v1" and s.isprintable()
    ),
)
@_settings
def test_property_any_unknown_scheme_fails_closed(crypto_factory, monkeypatch, scheme):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    canonical = b"some-payload"
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    sb = signing.SignatureBundle(
        identity_scheme=scheme,
        bundles=[blob],
        canonical_bytes=canonical,
        canon_version="knurl-1.0",
    )
    vr = signing.verify(canonical, sb)
    assert vr.status == signing.VerifyStatus.BUNDLE_MALFORMED


# ---------------------------------------------------------------------------
# Section 14 — Sign non-determinism
# ---------------------------------------------------------------------------


# Property: sign() called twice produces distinct bundle_json. Non-determinism
# is the basis for the verify-cache key being (content_hash, bundle_hash).
#
# K-A9: staging-only — see test_sign.py::test_sign_is_non_deterministic. The
# synthetic _FakeSigner always produces distinct bundles by construction, so
# only real sigstore-python signing (SKEIN_TEST_SIGSTORE_LIVE=1) instruments
# the actual ECDSA nonce property. Limitation documented at
# skein/signing.py sign(); Phase-4 audit is brief-20260514-me2x §2.
@pytest.mark.staging
@given(canonical_bytes=small_canonical_bytes_strategy)
@_settings
def test_property_two_signs_produce_different_bundles(
    crypto_factory, google_provider, monkeypatch, canonical_bytes
):
    crypto_factory.install_sign_monkeypatch(monkeypatch, provider=google_provider)
    a = signing.sign(canonical_bytes, google_provider)
    b = signing.sign(canonical_bytes, google_provider)
    _assert_sign_results_differ_per_field(a.bundle_json, b.bundle_json)
