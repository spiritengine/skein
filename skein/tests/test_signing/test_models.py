"""Pydantic model contract — every type on the signing surface.

VerifyStatus, VerifyResult, MultiVerifyResult, Evidence, RekorInclusionProof,
SignResult, SignatureBundle, OIDCProviderConfig, SigningUnavailable,
EmptySignatureBundle, MultiSignerBundle.

The models are part of the surface — every caller (routes.py, the instance
web read path, federation peer, CLI render layer) reads from them. A wrong-shaped enum value
or missing required field cascades across the read path. We test the shape
contract, not the values.
"""

from __future__ import annotations

import pytest

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from .conftest import signing  # noqa: E402


# ---------------------------------------------------------------------------
# VerifyStatus enum — exactly the 8 cases from Identity rev 5
# ---------------------------------------------------------------------------


# Enforces: VerifyStatus has exactly these 8 string-valued cases.
# Adding cases without spec amendment is a contract violation; removing one
# silently collapses failure modes (the rev-4 root cause Phase 1 surfaced).
def test_verifystatus_has_exactly_eight_cases():
    expected = {
        "VERIFIED",
        "CERT_INVALID",
        "INCLUSION_FAILED",
        "IDENTITY_MISMATCH",
        "TRUST_ROOT_STALE",
        "BUNDLE_MALFORMED",
        "OFFLINE_NO_TRUSTED_ROOT",
        "SIGNATURE_MISMATCH",
    }
    actual = {s.value for s in signing.VerifyStatus}
    assert actual == expected, f"missing {expected - actual}, extra {actual - expected}"


# Enforces: VerifyStatus is a str-Enum so federation can JSON-serialize directly.
def test_verifystatus_is_str_enum():
    assert issubclass(signing.VerifyStatus, str)
    assert signing.VerifyStatus.VERIFIED == "VERIFIED"


# Enforces: VerifyStatus values are distinct strings.
def test_verifystatus_values_distinct():
    values = [s.value for s in signing.VerifyStatus]
    assert len(values) == len(set(values))


# Enforces: every VerifyStatus member has a _SEVERITY_RANK entry. verify_multi's
# overall aggregation lookups would KeyError at runtime if a new member shipped
# without a rank. The module-level assert is the runtime guarantee; this test
# makes the contract visible in the suite.
def test_severity_rank_covers_all_verify_status():
    from skein.signing import _SEVERITY_RANK

    rank_keys = set(_SEVERITY_RANK.keys())
    status_members = set(signing.VerifyStatus)
    assert rank_keys == status_members, (
        f"missing {status_members - rank_keys}, extra {rank_keys - status_members}"
    )


# ---------------------------------------------------------------------------
# VerifyResult shape
# ---------------------------------------------------------------------------


# Enforces: VerifyResult has the four named fields locked by Identity rev 5.
def test_verify_result_has_required_fields():
    r = signing.VerifyResult(
        status=signing.VerifyStatus.VERIFIED,
        issuer="https://accounts.google.com",
        subject="alice@example.com",
        evidence=None,
    )
    assert r.status == signing.VerifyStatus.VERIFIED
    assert r.issuer == "https://accounts.google.com"
    assert r.subject == "alice@example.com"
    assert r.evidence is None


# Enforces: VerifyResult permits None for issuer/subject/evidence (failure
# cases like BUNDLE_MALFORMED may not have a parseable identity).
def test_verify_result_nulls_allowed_on_failure():
    r = signing.VerifyResult(
        status=signing.VerifyStatus.BUNDLE_MALFORMED,
        issuer=None,
        subject=None,
        evidence=None,
    )
    assert r.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: VerifyResult.status must be a VerifyStatus value, not arbitrary string.
def test_verify_result_status_must_be_enum():
    with pytest.raises(Exception):  # ValidationError
        signing.VerifyResult(
            status="NOT_A_REAL_STATUS",
            issuer=None,
            subject=None,
            evidence=None,
        )


# Enforces: VerifyResult is a Pydantic BaseModel (callers use model_dump etc).
def test_verify_result_is_pydantic_model():
    from pydantic import BaseModel

    assert issubclass(signing.VerifyResult, BaseModel)


# Enforces: VerifyResult is JSON-serializable (federation peers ship it over wire).
def test_verify_result_json_round_trips():
    r = signing.VerifyResult(
        status=signing.VerifyStatus.VERIFIED,
        issuer="https://accounts.google.com",
        subject="alice@example.com",
        evidence=None,
    )
    j = r.model_dump_json()
    r2 = signing.VerifyResult.model_validate_json(j)
    assert r == r2


# ---------------------------------------------------------------------------
# Evidence shape
# ---------------------------------------------------------------------------


# Enforces: Evidence carries validity_window, rekor_inclusion, cert_chain_summary.
# All optional because verify paths return Evidence on success only.
def test_evidence_shape():
    e = signing.Evidence(
        validity_window=(1_700_000_000_000_000, 1_700_000_600_000_000),
        rekor_inclusion=None,
        cert_chain_summary="Fulcio CA1 → leaf alice@example.com",
    )
    assert e.validity_window == (1_700_000_000_000_000, 1_700_000_600_000_000)
    assert e.cert_chain_summary.startswith("Fulcio")


# Enforces: validity_window microsecond timestamps per spec rev 5 §verify return.
def test_evidence_validity_window_is_microseconds():
    # 2026-01-01T00:00:00Z = 1_767_225_600_000_000 microseconds.
    e = signing.Evidence(
        validity_window=(1_767_225_600_000_000, 1_767_226_200_000_000),
        rekor_inclusion=None,
        cert_chain_summary=None,
    )
    start, end = e.validity_window
    # Sanity-check: 600 seconds (Fulcio cert ~10-minute window) in microseconds.
    assert end - start == 600_000_000


# Enforces: Evidence is a Pydantic model.
def test_evidence_is_pydantic_model():
    from pydantic import BaseModel

    assert issubclass(signing.Evidence, BaseModel)


# ---------------------------------------------------------------------------
# RekorInclusionProof — 7 fields per clarification 4 + addendum log_id
# ---------------------------------------------------------------------------


# Enforces: RekorInclusionProof exposes log_index, tree_size, root_hash, hashes,
# checkpoint, integrated_time, and log_id. finding-20260513-w5hq adds log_id so
# verifiers select the correct Rekor key instead of trying every historical key.
def test_rekor_inclusion_proof_has_seven_fields():
    proof = signing.RekorInclusionProof(
        log_index=42,
        tree_size=999,
        root_hash="base64-encoded-merkle-root",
        hashes=["sibling-hash-1", "sibling-hash-2"],
        checkpoint="rekor.sigstore.dev - 999\n<roothash>\n\n— rekor.sigstore.dev <sig>\n",
        integrated_time=1_715_443_200_000_000,
        log_id="base64-encoded-rekor-key-id",
    )
    assert proof.log_index == 42
    assert proof.tree_size == 999
    assert proof.root_hash == "base64-encoded-merkle-root"
    assert proof.hashes == ["sibling-hash-1", "sibling-hash-2"]
    assert "rekor.sigstore.dev" in proof.checkpoint
    assert proof.integrated_time == 1_715_443_200_000_000
    assert proof.log_id == "base64-encoded-rekor-key-id"


# Enforces: every field is required; missing any one is a validation error.
@pytest.mark.parametrize(
    "missing_field",
    [
        "log_index",
        "tree_size",
        "root_hash",
        "hashes",
        "checkpoint",
        "integrated_time",
        "log_id",
    ],
)
def test_rekor_inclusion_proof_fields_are_required(missing_field):
    payload = dict(
        log_index=42,
        tree_size=999,
        root_hash="rh",
        hashes=[],
        checkpoint="cp",
        integrated_time=1_715_443_200_000_000,
        log_id="rekor-key-id",
    )
    del payload[missing_field]
    with pytest.raises(Exception):
        signing.RekorInclusionProof(**payload)


def test_rekor_inclusion_proof_log_id_rejects_empty():
    with pytest.raises(Exception):
        signing.RekorInclusionProof(
            log_index=42,
            tree_size=999,
            root_hash="rh",
            hashes=[],
            checkpoint="cp",
            integrated_time=1_715_443_200_000_000,
            log_id="",
        )


def test_rekor_inclusion_proof_log_id_rejects_over_max_length():
    with pytest.raises(Exception):
        signing.RekorInclusionProof(
            log_index=42,
            tree_size=999,
            root_hash="rh",
            hashes=[],
            checkpoint="cp",
            integrated_time=1_715_443_200_000_000,
            log_id="A" * 513,
        )


def test_rekor_inclusion_proof_log_id_accepts_typical_base64_key_id():
    proof = signing.RekorInclusionProof(
        log_index=42,
        tree_size=999,
        root_hash="rh",
        hashes=[],
        checkpoint="cp",
        integrated_time=1_715_443_200_000_000,
        log_id="mQv7r2XxQAE8m4Vxy03Yf0PTrf1LX6Hux4nQ2s6h0Wk=",
    )
    assert proof.log_id


# Enforces: an independent verifier can use these seven fields to re-run Merkle
# inclusion without re-querying Rekor. The proof carries (root_hash, hashes,
# tree_size) for the Merkle path, (log_index) to know which leaf, (checkpoint)
# as the signed witness binding root_hash to tree_size, (integrated_time) for
# the validity-window check against the cert, and (log_id) to choose the Rekor key.
def test_rekor_inclusion_proof_carries_everything_for_independent_verify(
    crypto_factory,
):
    checkpoint = crypto_factory.make_checkpoint(
        tree_size=12345679,
        root_hash="AAECAwQFBgcICQ==",
        log_id="rekor-key-id",
    )
    # Phase-3 amendment: tree_size must be > log_index per the locked
    # _log_index_inside_tree validator. The original values (log_index=12345678
    # vs tree_size=100000) violated this. Use a tree_size that comfortably
    # exceeds log_index.
    proof = signing.RekorInclusionProof(
        log_index=12345678,
        tree_size=12345679,
        root_hash="AAECAwQFBgcICQ==",  # base64
        hashes=["sibling0", "sibling1", "sibling2"],
        checkpoint=checkpoint,
        integrated_time=1_715_443_200_000_000,
        log_id="rekor-key-id",
    )
    # Merkle path: leaf -> siblings -> root_hash
    assert len(proof.hashes) > 0
    # Witness binding the (tree_size, root_hash) tuple cryptographically.
    parsed = crypto_factory.parse_checkpoint_signed_note(proof.checkpoint)
    assert parsed.tree_size == proof.tree_size
    assert parsed.root_hash == proof.root_hash


# Enforces: integrated_time is microseconds UTC (consistent with validity_window).
def test_rekor_inclusion_proof_integrated_time_is_microseconds():
    proof = signing.RekorInclusionProof(
        log_index=0,
        tree_size=1,
        root_hash="rh",
        hashes=[],
        checkpoint="cp",
        integrated_time=1_767_225_600_000_000,
        log_id="rekor-key-id",
    )
    # 2026-01-01T00:00:00Z in microseconds is > 1.5e15
    assert proof.integrated_time > 1_000_000_000_000_000


# Enforces: Evidence.rekor_inclusion holds a RekorInclusionProof when present.
def test_evidence_rekor_inclusion_is_rekor_inclusion_proof():
    proof = signing.RekorInclusionProof(
        log_index=42,
        tree_size=999,
        root_hash="rh",
        hashes=["s0", "s1"],
        checkpoint="cp",
        integrated_time=1_715_443_200_000_000,
        log_id="rekor-key-id",
    )
    e = signing.Evidence(
        validity_window=(1_700_000_000_000_000, 1_700_000_600_000_000),
        rekor_inclusion=proof,
        cert_chain_summary=None,
    )
    assert isinstance(e.rekor_inclusion, signing.RekorInclusionProof)
    assert e.rekor_inclusion.log_index == 42


# ---------------------------------------------------------------------------
# MultiVerifyResult shape
# ---------------------------------------------------------------------------


# Enforces: MultiVerifyResult.results is a list (preserves order from
# signature_bundle.bundles; order is significant for declare-continuity).
def test_multi_verify_result_preserves_order():
    r1 = signing.VerifyResult(
        status=signing.VerifyStatus.VERIFIED,
        issuer="https://accounts.google.com",
        subject="alice@example.com",
        evidence=None,
    )
    r2 = signing.VerifyResult(
        status=signing.VerifyStatus.VERIFIED,
        issuer="https://github.com/login/oauth",
        subject="alice-gh",
        evidence=None,
    )
    multi = signing.MultiVerifyResult(
        results=[r1, r2], overall=signing.VerifyStatus.VERIFIED
    )
    assert multi.results[0].subject == "alice@example.com"
    assert multi.results[1].subject == "alice-gh"


# Enforces: MultiVerifyResult.overall is VERIFIED iff all results are VERIFIED.
# Rev 5 normative semantics: "A folio is fully verified when overall == VERIFIED."
def test_multi_verify_result_overall_not_verified_when_any_fail():
    r_ok = signing.VerifyResult(
        status=signing.VerifyStatus.VERIFIED,
        issuer="https://accounts.google.com",
        subject="alice@example.com",
        evidence=None,
    )
    r_bad = signing.VerifyResult(
        status=signing.VerifyStatus.IDENTITY_MISMATCH,
        issuer="https://accounts.google.com",
        subject="mallory@example.com",
        evidence=None,
    )
    multi = signing.MultiVerifyResult(
        results=[r_ok, r_bad],
        overall=signing.VerifyStatus.IDENTITY_MISMATCH,
    )
    assert multi.overall != signing.VerifyStatus.VERIFIED


# Enforces: MultiVerifyResult.results must have at least one entry. Empty
# bundles cannot produce a MultiVerifyResult — they raise EmptySignatureBundle
# at SignatureBundle construction.
def test_multi_verify_result_at_least_one():
    with pytest.raises(Exception):
        signing.MultiVerifyResult(results=[], overall=signing.VerifyStatus.VERIFIED)


# Enforces: MultiVerifyResult is a Pydantic model.
def test_multi_verify_result_is_pydantic_model():
    from pydantic import BaseModel

    assert issubclass(signing.MultiVerifyResult, BaseModel)


# ---------------------------------------------------------------------------
# SignatureBundle shape
# ---------------------------------------------------------------------------


# Enforces: SignatureBundle has the five fields locked by Identity rev 5.
# bundles is the cryptographic source of truth; canonical_bytes is what every
# signer signed; canon_version is informational; identity_scheme drives verifier
# dispatch; trust_root_pin lets future verifiers locate era-correct trust roots.
def test_signature_bundle_shape(make_bundle):
    canonical = b"canonical-bytes-payload"
    sb = make_bundle(
        canonical_bytes=canonical,
        bundles=["<bundle-blob-1>"],
        identity_scheme="sigstore-public-v1",
        canon_version="knurl-1.0",
        trust_root_pin="sha256:abc123",
    )
    assert sb.identity_scheme == "sigstore-public-v1"
    assert sb.canon_version == "knurl-1.0"
    assert sb.canonical_bytes == canonical
    assert sb.trust_root_pin == "sha256:abc123"
    assert len(sb.bundles) == 1


# Enforces: SignatureBundle.bundles is mandatory and >= 1; construction with
# empty bundles raises EmptySignatureBundle per clarification 2.
def test_signature_bundle_empty_bundles_raises_empty_signature_bundle(make_bundle):
    with pytest.raises(signing.EmptySignatureBundle) as exc:
        make_bundle(canonical_bytes=b"x", bundles=[])
    assert "at least one signer" in str(exc.value)
    assert "got 0" in str(exc.value)


# Enforces: identity_scheme is a string discriminator. The verify dispatch is
# string equality; passing an enum or int would fail-closed silently.
def test_signature_bundle_identity_scheme_is_string(make_bundle):
    sb = make_bundle(canonical_bytes=b"x", identity_scheme="sigstore-public-v1")
    assert isinstance(sb.identity_scheme, str)


# Enforces: SignatureBundle.bundles entries are str. Per clarification 1, the
# wire form for one signer's bundle is canonical sigstore-bundle ProtoJSON
# (a plain string, no SigstoreBundleBlob alias).
def test_signature_bundle_bundles_entries_are_strings(make_bundle):
    sb = make_bundle(
        canonical_bytes=b"x", bundles=["<bundle-blob-1>", "<bundle-blob-2>"]
    )
    for entry in sb.bundles:
        assert isinstance(entry, str)


# Enforces: SignatureBundle.canonical_bytes preserves bytes byte-exactly (no
# .decode/.encode round-trip).
def test_signature_bundle_canonical_bytes_is_bytes(make_bundle):
    canonical = bytes(range(256))
    sb = make_bundle(canonical_bytes=canonical)
    assert isinstance(sb.canonical_bytes, bytes)
    assert sb.canonical_bytes == canonical


# Enforces: SignatureBundle round-trips through Pydantic JSON. Federation ships
# these between peers; the wire shape must be stable.
def test_signature_bundle_json_round_trips(make_bundle):
    sb = make_bundle(
        canonical_bytes=b"x",
        bundles=["<bundle-blob-1>", "<bundle-blob-2>"],
        identity_scheme="sigstore-public-v1",
        canon_version="knurl-1.0",
        trust_root_pin="sha256:def456",
    )
    j = sb.model_dump_json()
    sb2 = signing.SignatureBundle.model_validate_json(j)
    assert sb2 == sb


# Enforces: canon_version defaults to "knurl-1.0" (informational, not used by verify).
def test_signature_bundle_canon_version_default():
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["<blob>"],
        canonical_bytes=b"x",
    )
    assert sb.canon_version == "knurl-1.0"


# Enforces: trust_root_pin defaults to None (optional content-addressable ref).
def test_signature_bundle_trust_root_pin_default():
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["<blob>"],
        canonical_bytes=b"x",
    )
    assert sb.trust_root_pin is None


# Enforces: canonical_bytes accepts exactly MAX_CANONICAL_BYTES_LENGTH bytes.
def test_canonical_bytes_accepts_max_length():
    from skein.signing import MAX_CANONICAL_BYTES_LENGTH

    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=["<blob>"],
        canonical_bytes=b"\x00" * MAX_CANONICAL_BYTES_LENGTH,
    )
    assert len(sb.canonical_bytes) == MAX_CANONICAL_BYTES_LENGTH


# Enforces: canonical_bytes rejects inputs exceeding MAX_CANONICAL_BYTES_LENGTH.
def test_canonical_bytes_rejects_over_max_length():
    from pydantic import ValidationError

    from skein.signing import MAX_CANONICAL_BYTES_LENGTH

    with pytest.raises(ValidationError):
        signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=["<blob>"],
            canonical_bytes=b"\x00" * (MAX_CANONICAL_BYTES_LENGTH + 1),
        )


# ---------------------------------------------------------------------------
# SignResult shape (clarification 1)
# ---------------------------------------------------------------------------


# Enforces: SignResult is the sign() return type — a Pydantic model wrapping
# the upstream bundle JSON plus SKEIN's extracted convenience fields.
def test_sign_result_has_required_fields():
    ev = signing.Evidence(
        validity_window=(1_700_000_000_000_000, 1_700_000_600_000_000),
        rekor_inclusion=None,
        cert_chain_summary=None,
    )
    r = signing.SignResult(
        bundle_json='{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}',
        issuer="https://accounts.google.com",
        subject="alice@example.com",
        signing_timestamp=1_715_443_200_000_000,
        evidence=ev,
    )
    assert r.bundle_json.startswith("{")
    assert r.issuer == "https://accounts.google.com"
    assert r.subject == "alice@example.com"
    assert r.signing_timestamp == 1_715_443_200_000_000
    assert r.evidence == ev


# Enforces: SignResult.bundle_json is a plain str (canonical sigstore-bundle
# ProtoJSON). Per clarification 1: "The wire form for one signer's bundle is
# plain str (the canonical sigstore-bundle ProtoJSON). No separate top-level
# type name."
def test_sign_result_bundle_json_is_str():
    ev = signing.Evidence(
        validity_window=(1, 2),
        rekor_inclusion=None,
        cert_chain_summary=None,
    )
    r = signing.SignResult(
        bundle_json='{"mediaType":"application/vnd.dev.sigstore.bundle.v0.3+json"}',
        issuer="https://accounts.google.com",
        subject="alice@example.com",
        signing_timestamp=1_715_443_200_000_000,
        evidence=ev,
    )
    assert isinstance(r.bundle_json, str)


# Enforces: SignResult is a Pydantic model (callers serialize for queue storage).
def test_sign_result_is_pydantic_model():
    from pydantic import BaseModel

    assert issubclass(signing.SignResult, BaseModel)


# Enforces: SignResult names parallel VerifyResult / MultiVerifyResult per
# clarification 1: "Naming parallels VerifyResult and MultiVerifyResult."
def test_sign_result_naming_parallels_verify_result():
    # No SingleSignerBundle alias should exist (clarification 1: retired).
    assert not hasattr(signing, "SingleSignerBundle"), (
        "SingleSignerBundle was retired per clarification 1; use SignResult"
    )


# Enforces: SignResult.signing_timestamp is microseconds UTC from the Rekor
# inclusion / RFC3161 — not client clock. Sized so a 2026 timestamp fits.
def test_sign_result_signing_timestamp_is_microseconds():
    ev = signing.Evidence(
        validity_window=(1, 2),
        rekor_inclusion=None,
        cert_chain_summary=None,
    )
    r = signing.SignResult(
        bundle_json="{}",
        issuer="https://accounts.google.com",
        subject="alice@example.com",
        signing_timestamp=1_767_225_600_000_000,  # 2026-01-01 UTC
        evidence=ev,
    )
    assert r.signing_timestamp > 1_000_000_000_000_000


# Enforces: no SigstoreBundleBlob alias exists — clarification 1 drops it in
# favor of plain str with docstrings.
def test_no_sigstore_bundle_blob_alias():
    assert not hasattr(signing, "SigstoreBundleBlob"), (
        "SigstoreBundleBlob alias was retired per clarification 1; "
        "SignatureBundle.bundles entries are plain str."
    )


# ---------------------------------------------------------------------------
# OIDCProviderConfig shape — 4 fields per clarification 3
# ---------------------------------------------------------------------------


# Enforces: OIDCProviderConfig has issuer + token (required); provider_id and
# expires_at (optional). Per clarification 3.
def test_oidc_provider_config_full_shape():
    cfg = signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token="oauth-jwt-here",
        provider_id="google",
        expires_at=1_767_225_600_000_000,
    )
    assert cfg.issuer == "https://accounts.google.com"
    assert cfg.token == "oauth-jwt-here"
    assert cfg.provider_id == "google"
    assert cfg.expires_at == 1_767_225_600_000_000


# Enforces: issuer and token are required (clarification 3).
def test_oidc_provider_config_issuer_required():
    with pytest.raises(Exception):
        signing.OIDCProviderConfig(token="jwt")  # missing issuer


def test_oidc_provider_config_token_required():
    with pytest.raises(Exception):
        signing.OIDCProviderConfig(
            issuer="https://accounts.google.com"
        )  # missing token


# Enforces: provider_id is optional (default None).
def test_oidc_provider_config_provider_id_optional():
    cfg = signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token="jwt",
    )
    assert cfg.provider_id is None


# Enforces: expires_at is optional (default None; not all flows surface expiry
# cleanly per clarification 3).
def test_oidc_provider_config_expires_at_optional():
    cfg = signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token="jwt",
    )
    assert cfg.expires_at is None


# Enforces: both v0 providers can be expressed with this shape.
def test_oidc_provider_config_accepts_v0_providers():
    g = signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token="g-jwt",
        provider_id="google",
    )
    h = signing.OIDCProviderConfig(
        issuer="https://github.com/login/oauth",
        token="gh-jwt",
        provider_id="github",
    )
    assert g.provider_id != h.provider_id
    assert g.issuer != h.issuer


# Enforces: expires_at is microseconds UTC.
def test_oidc_provider_config_expires_at_is_microseconds():
    cfg = signing.OIDCProviderConfig(
        issuer="https://accounts.google.com",
        token="jwt",
        expires_at=1_767_225_600_000_000,  # 2026-01-01 UTC
    )
    assert cfg.expires_at > 1_000_000_000_000_000


# ---------------------------------------------------------------------------
# SigningUnavailable exception
# ---------------------------------------------------------------------------


# Enforces: SigningUnavailable is an Exception subclass — the offline-write
# queue catches it as `except SigningUnavailable`.
def test_signing_unavailable_is_exception():
    assert issubclass(signing.SigningUnavailable, Exception)


# Enforces: SigningUnavailable carries a reason string accessible via .reason
# AND via str(). The queue's retry-decision logic uses both.
def test_signing_unavailable_carries_reason():
    e = signing.SigningUnavailable("fulcio unavailable: 503")
    assert e.reason == "fulcio unavailable: 503"
    assert "fulcio" in str(e).lower()


# Enforces: SigningUnavailable can carry a structured component identifier so
# dashboards can render "fulcio" / "rekor" / "oidc" narrowly.
def test_signing_unavailable_carries_component():
    e = signing.SigningUnavailable(
        "fulcio unavailable: 503",
        component="fulcio",
    )
    assert e.component == "fulcio"


# Enforces: SigningUnavailable can wrap a cause exception via __cause__ so the
# original sigstore-python exception remains accessible (clarification 5).
def test_signing_unavailable_wraps_cause():
    original = RuntimeError("connection refused: fulcio.sigstore.dev:443")
    e = signing.SigningUnavailable(
        "fulcio unavailable",
        component="fulcio",
        cause=original,
    )
    assert e.__cause__ is original


# Enforces: SigningUnavailable is catchable as generic Exception.
def test_signing_unavailable_caught_by_exception():
    try:
        raise signing.SigningUnavailable("test", component="fulcio")
    except Exception as e:
        assert isinstance(e, signing.SigningUnavailable)


# ---------------------------------------------------------------------------
# EmptySignatureBundle exception (clarification 2)
# ---------------------------------------------------------------------------


# Enforces: EmptySignatureBundle exists as a SKEIN-specific exception.
def test_empty_signature_bundle_exists():
    assert hasattr(signing, "EmptySignatureBundle")
    assert issubclass(signing.EmptySignatureBundle, Exception)


# Enforces: EmptySignatureBundle is raised by SignatureBundle construction
# with bundles=[]. Per clarification 2, the message is:
# "SignatureBundle must have at least one signer; got 0."
def test_empty_signature_bundle_message():
    with pytest.raises(signing.EmptySignatureBundle) as exc:
        signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[],
            canonical_bytes=b"x",
        )
    assert str(exc.value) == "SignatureBundle must have at least one signer; got 0."


# Enforces: EmptySignatureBundle is a caller programming error — distinct from
# SigningUnavailable (domain failure). Per clarification 2.
def test_empty_signature_bundle_is_not_signing_unavailable():
    assert not issubclass(signing.EmptySignatureBundle, signing.SigningUnavailable)


# ---------------------------------------------------------------------------
# MultiSignerBundle exception (clarification 2)
# ---------------------------------------------------------------------------


# Enforces: MultiSignerBundle exists as a SKEIN-specific exception.
def test_multi_signer_bundle_exists():
    assert hasattr(signing, "MultiSignerBundle")
    assert issubclass(signing.MultiSignerBundle, Exception)


# Enforces: MultiSignerBundle is a caller programming error — distinct from
# SigningUnavailable. Per clarification 2: "caller programming errors, not
# domain failures."
def test_multi_signer_bundle_is_not_signing_unavailable():
    assert not issubclass(signing.MultiSignerBundle, signing.SigningUnavailable)


# Enforces: MultiSignerBundle message format per clarification 2:
# "verify() requires exactly one signer; got N. Use verify_multi() for multi-signer bundles."
# Tested via verify() behavior in test_verify.py; this test just confirms the
# class is instantiable with N substituted.
def test_multi_signer_bundle_can_be_raised():
    msg = (
        "verify() requires exactly one signer; got 2. "
        "Use verify_multi() for multi-signer bundles."
    )
    with pytest.raises(signing.MultiSignerBundle) as exc:
        raise signing.MultiSignerBundle(msg)
    assert "verify_multi()" in str(exc.value)
    assert "got 2" in str(exc.value)
