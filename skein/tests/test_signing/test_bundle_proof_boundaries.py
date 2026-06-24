"""SignatureBundle and Rekor proof boundary contract.

Oracle finding-20260512-sr0w's bundle/proof nit called out max signer count,
duplicate signer semantics, and inclusion-proof consistency edges.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

pytest.importorskip(
    "skein.signing",
    reason="skein.signing is the phase-3 deliverable; contract collects but skips until then.",
)

from .conftest import HAS_FUNCTIONS, signing  # noqa: E402

MAX_SIGNERS = 256


# Enforces: finding-20260512-sr0w proof-boundary nit. Contract choice: the folio
# wire format allows up to 256 signer bundles. This is high enough for practical
# continuity/delegation use and bounded enough to avoid unbounded verifier work.
def test_signature_bundle_max_signers_documented(canonical_bytes_simple):
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[f'{{"testBundle":{i}}}' for i in range(MAX_SIGNERS)],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    assert len(sb.bundles) == MAX_SIGNERS


# Enforces: the 256-signer cap is hard; callers must not construct bundles that
# imply unbounded verify_multi() work.
def test_signature_bundle_rejects_more_than_max_signers(canonical_bytes_simple):
    with pytest.raises(Exception):
        signing.SignatureBundle(
            identity_scheme="sigstore-public-v1",
            bundles=[f'{{"testBundle":{i}}}' for i in range(MAX_SIGNERS + 1)],
            canonical_bytes=canonical_bytes_simple,
            canon_version="knurl-1.0",
        )


# Enforces: length-1 is canonical for the common single-signer folio case.
def test_signature_bundle_bundles_length_one_canonical(canonical_bytes_simple):
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=['{"testBundle":0}'],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )
    assert len(sb.bundles) == 1


phase3_functions = pytest.mark.skipif(
    not HAS_FUNCTIONS,
    reason="signing.sign/verify/verify_multi are Phase 3 deliverables",
)


# Enforces: duplicate signer identity policy. Contract choice: duplicate
# identities are allowed and verify independently; authorization layers may
# deduplicate them, but verify_multi() is an evidence checker, not a quorum
# calculator.
@phase3_functions
def test_verify_multi_with_duplicate_signer_identity(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    first = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    second = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[first, second],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )

    multi = signing.verify_multi(canonical_bytes_simple, sb)

    assert len(multi.results) == 2
    assert [r.subject for r in multi.results] == [
        "alice@example.com",
        "alice@example.com",
    ]
    assert all(r.status == signing.VerifyStatus.VERIFIED for r in multi.results)
    assert multi.overall == signing.VerifyStatus.VERIFIED


# Enforces: a non-trivial Rekor tree needs a non-empty audit path. tree_size=5
# with hashes=[] is a structural bundle defect: the proof cannot even be parsed
# as a valid inclusion proof, so the pinned status is BUNDLE_MALFORMED.
@phase3_functions
def test_rekor_proof_empty_hashes_when_tree_size_greater_than_one(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_rekor_tree_size(blob, 5)
    blob = crypto_factory.set_rekor_hashes(blob, [])
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )

    result = signing.verify(canonical_bytes_simple, sb)

    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: inclusionProof.hashes are base64-encoded Merkle hashes; malformed
# base64 is a bundle-shape error, not a successful empty proof.
@phase3_functions
def test_rekor_proof_malformed_base64_hashes(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_rekor_hashes(blob, ["not base64!!!"])
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )

    result = signing.verify(canonical_bytes_simple, sb)

    assert result.status == signing.VerifyStatus.BUNDLE_MALFORMED


# Enforces: checkpoint root and inclusionProof.root_hash bind the same Rekor tree
# root. A valid checkpoint for root X cannot authorize a proof claiming root Y.
@phase3_functions
def test_rekor_proof_checkpoint_root_mismatch_with_proof_root(
    crypto_factory, canonical_bytes_simple, monkeypatch
):
    crypto_factory.install_verify_monkeypatch(monkeypatch)
    blob = crypto_factory.make_bundle_blob(
        canonical_bytes=canonical_bytes_simple,
        identity="alice@example.com",
        issuer="https://accounts.google.com",
    )
    blob = crypto_factory.set_rekor_root_hash(blob, "cm9vdC1oYXNoLW1pc21hdGNo")
    sb = signing.SignatureBundle(
        identity_scheme="sigstore-public-v1",
        bundles=[blob],
        canonical_bytes=canonical_bytes_simple,
        canon_version="knurl-1.0",
    )

    result = signing.verify(canonical_bytes_simple, sb)

    assert result.status == signing.VerifyStatus.INCLUSION_FAILED


# ---------------------------------------------------------------------------
# _build_rekor_inclusion_proof — empty key_id contract
# ---------------------------------------------------------------------------
#
# When the wire inclusion proof carries a missing/empty Rekor log_id key_id,
# the builder must honor its "return None means no inclusion proof" contract
# (already used for missing proof at line 689 and for log_index>=tree_size at
# line 704). The earlier `or "unknown"` fallback fabricated a literal "unknown"
# string that satisfies RekorInclusionProof.log_id's min_length=1 validator
# and survived all the way to Evidence consumers, masking the missing-data
# signal.


_DEFAULT_CHECKPOINT_ENVELOPE = (
    "rekor.sigstore.dev\n10\n<root>\n\n— rekor.sigstore.dev <sig>\n"
)


def _fake_bundle_with_inclusion(
    *,
    key_id: bytes,
    log_index: int = 5,
    tree_size: int = 10,
    integrated_time_s: int = 1_715_443_200,
    checkpoint_envelope: str | None = _DEFAULT_CHECKPOINT_ENVELOPE,
) -> SimpleNamespace:
    """Build a duck-typed Bundle covering the attributes _build_rekor_inclusion_proof reads.

    checkpoint_envelope:
        - non-empty string: wrapped in a checkpoint SimpleNamespace (normal path).
        - "" (empty string): wrapped in a checkpoint SimpleNamespace with empty envelope
          (wire path delivered a checkpoint object whose envelope is missing).
        - None: proof.checkpoint itself is None (wire path delivered no checkpoint object).
    """
    if checkpoint_envelope is None:
        checkpoint = None
    else:
        checkpoint = SimpleNamespace(envelope=checkpoint_envelope)
    proof = SimpleNamespace(
        log_index=log_index,
        tree_size=tree_size,
        root_hash=b"root-hash-bytes",
        hashes=[b"sibling-0", b"sibling-1"],
        checkpoint=checkpoint,
    )
    inner = SimpleNamespace(
        inclusion_proof=proof,
        integrated_time=integrated_time_s,
        log_id=SimpleNamespace(key_id=key_id),
    )
    log_entry = SimpleNamespace(_inner=inner)
    return SimpleNamespace(log_entry=log_entry)


def test_build_rekor_inclusion_proof_returns_none_on_empty_key_id():
    bundle = _fake_bundle_with_inclusion(key_id=b"")
    assert signing._build_rekor_inclusion_proof(bundle) is None


def test_build_rekor_inclusion_proof_b64_encodes_log_id_when_key_id_present():
    key_id = b"\x01\x02\x03\x04rekor-key"
    bundle = _fake_bundle_with_inclusion(key_id=key_id)
    result = signing._build_rekor_inclusion_proof(bundle)
    assert result is not None
    assert result.log_id == base64.b64encode(key_id).decode("ascii")


# ---------------------------------------------------------------------------
# _build_rekor_inclusion_proof — empty checkpoint envelope contract (Shard P,
# residual from F-closure finding-20260520-j4w4)
# ---------------------------------------------------------------------------
#
# When the wire inclusion proof carries a missing/empty checkpoint envelope, the
# builder previously fabricated the literal string "rekor.sigstore.dev\n0\n\n\n"
# as a fallback. That fabricated string is a signed-note whose tree-size line is
# "0", which contradicts RekorInclusionProof.tree_size (the struct field, which
# reflects the real proof.tree_size). Consumers that parse the checkpoint string
# read tree_size=0 while consumers that read the struct field read the real
# number — silent inconsistency. The fix passes the empty string through so
# absence is observable rather than masked.


def test_build_rekor_inclusion_proof_passes_empty_envelope_through():
    """Wire delivers checkpoint object with empty envelope: surface "" instead
    of fabricating a tree_size=0 signed-note that contradicts the struct field.
    """
    key_id = b"\x01\x02\x03\x04rekor-key"
    bundle = _fake_bundle_with_inclusion(key_id=key_id, checkpoint_envelope="")
    result = signing._build_rekor_inclusion_proof(bundle)
    assert result is not None
    assert result.checkpoint == ""


def test_build_rekor_inclusion_proof_passes_missing_checkpoint_through():
    """Wire delivers no checkpoint object at all: same contract as empty envelope —
    surface "" instead of fabricating tree_size=0."""
    key_id = b"\x01\x02\x03\x04rekor-key"
    bundle = _fake_bundle_with_inclusion(key_id=key_id, checkpoint_envelope=None)
    result = signing._build_rekor_inclusion_proof(bundle)
    assert result is not None
    assert result.checkpoint == ""


def test_build_rekor_inclusion_proof_struct_tree_size_matches_parsed_envelope(
    crypto_factory,
):
    """When a checkpoint is present, parsing it must yield a tree_size that
    matches RekorInclusionProof.tree_size — the bug was silent disagreement
    between the struct field (real tree_size) and the embedded signed-note line
    (fabricated "0")."""
    key_id = b"\x01\x02\x03\x04rekor-key"
    real_tree_size = 10
    envelope = (
        f"rekor.sigstore.dev\n{real_tree_size}\n<root>\n\n— rekor.sigstore.dev <sig>\n"
    )
    bundle = _fake_bundle_with_inclusion(
        key_id=key_id, checkpoint_envelope=envelope, tree_size=real_tree_size
    )
    result = signing._build_rekor_inclusion_proof(bundle)
    assert result is not None
    parsed = crypto_factory.parse_checkpoint_signed_note(result.checkpoint)
    assert parsed.tree_size == result.tree_size


def test_build_rekor_inclusion_proof_passes_real_envelope_verbatim():
    """Non-empty wire envelope must flow through unmodified (sanity)."""
    key_id = b"\x01\x02\x03\x04rekor-key"
    envelope = "rekor.sigstore.dev\n10\n<root>\n\n— rekor.sigstore.dev <sig>\n"
    bundle = _fake_bundle_with_inclusion(key_id=key_id, checkpoint_envelope=envelope)
    result = signing._build_rekor_inclusion_proof(bundle)
    assert result is not None
    assert result.checkpoint == envelope


# ---------------------------------------------------------------------------
# _build_rekor_inclusion_proof — timestamp-floor substitution warning
# (j4w4 round-7 oracle A3)
# ---------------------------------------------------------------------------
#
# When Rekor's integrated_time is 0 or otherwise lands below
# MIN_MICROSECOND_TIMESTAMP, the builder substitutes the floor value. Before
# the A3 fix, that substitution was silent and the resulting
# RekorInclusionProof.integrated_time would report the sentinel-like floor
# with no signal that the wire value was anomalous. The fix logs a WARNING
# with a grep-able prefix so the substitution is observable.


def test_build_rekor_inclusion_proof_warns_on_zero_integrated_time(caplog):
    key_id = b"\x01\x02\x03\x04rekor-key"
    bundle = _fake_bundle_with_inclusion(key_id=key_id, integrated_time_s=0)
    with caplog.at_level("WARNING"):
        result = signing._build_rekor_inclusion_proof(bundle)
    assert result is not None
    assert result.integrated_time == signing.MIN_MICROSECOND_TIMESTAMP
    floor_records = [
        r
        for r in caplog.records
        if "signing.rekor_integrated_time_below_floor" in r.getMessage()
    ]
    assert floor_records, (
        "expected at least one WARNING log with "
        "'signing.rekor_integrated_time_below_floor' prefix; "
        f"got: {[r.getMessage() for r in caplog.records]}"
    )
    assert any(r.levelname == "WARNING" for r in floor_records)
    # The log must include the original below-floor value for triage.
    assert any("0" in r.getMessage() for r in floor_records)


def test_build_rekor_inclusion_proof_warns_on_below_floor_integrated_time(caplog):
    """Any non-zero below-floor value also warns — not only the 0 sentinel."""
    key_id = b"\x01\x02\x03\x04rekor-key"
    # 1 second since epoch → 1_000_000 us, well below MIN_MICROSECOND_TIMESTAMP.
    bundle = _fake_bundle_with_inclusion(key_id=key_id, integrated_time_s=1)
    with caplog.at_level("WARNING"):
        result = signing._build_rekor_inclusion_proof(bundle)
    assert result is not None
    assert result.integrated_time == signing.MIN_MICROSECOND_TIMESTAMP
    assert any(
        "signing.rekor_integrated_time_below_floor" in r.getMessage()
        and r.levelname == "WARNING"
        for r in caplog.records
    )


def test_build_rekor_inclusion_proof_does_not_warn_on_normal_integrated_time(
    caplog,
):
    """No warning when integrated_time is well above the floor (sanity)."""
    key_id = b"\x01\x02\x03\x04rekor-key"
    bundle = _fake_bundle_with_inclusion(key_id=key_id, integrated_time_s=1_715_443_200)
    with caplog.at_level("WARNING"):
        result = signing._build_rekor_inclusion_proof(bundle)
    assert result is not None
    assert not any(
        "signing.rekor_integrated_time_below_floor" in r.getMessage()
        for r in caplog.records
    )
